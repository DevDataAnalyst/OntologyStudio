"""
Learned knowledge ("ontology snippets"): facts mined from the SQL people actually run.

Every query that touches the ontology's tables is parsed (sqlglot) into snippets:
  join        Orders.customer_id = Customers.customer_id
  filter      Orders.shipped_date > Orders.required_date       (WHERE conjuncts)
  expression  SUM(OrderDetails.unit_price * OrderDetails.quantity) AS revenue   (aggregates)

Each snippet keeps its evidence (uses, distinct authors, where it ran, comments, last seen) and
gets an authority score in [0, 1] — a transparent, OntoRank-style blend of:
  popularity  how often it is used                      log-scaled
  reach       how many different people use it          log-scaled
  author      how authoritative its authors are         volume + shared work, plus trusted-author boosts
  sharing     share of uses in shared assets            dashboards, Genie spaces, jobs, alerts
  documented  someone explained it in a SQL comment
  recency     decays with a 90-day half life

Sources: system.query.history (Unity Catalog, with executed_by and query_source), an uploaded
.sql / .csv query log, and Ask questions that got a thumbs up.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

SHARED_SOURCES = {"dashboard", "genie", "job", "alert"}
SOURCE_LABELS = {"dashboard": "Dashboard", "genie": "Genie space", "job": "Job", "alert": "Alert",
                 "notebook": "Notebook", "sql_editor": "SQL editor", "file": "Query log file",
                 "ask": "Ask (👍)", "other": "Other"}
WEIGHTS = {"popularity": 0.30, "reach": 0.20, "author": 0.15, "sharing": 0.15, "documented": 0.10,
           "recency": 0.10}
HALF_LIFE_DAYS = 90


# --------------------------------------------------------------------------- #
# Reading query logs
# --------------------------------------------------------------------------- #
def classify_source(qsrc: str | dict | None, client: str | None = None) -> tuple[str, str | None]:
    """(source kind, source id) from system.query.history query_source / client_application."""
    q = qsrc
    if isinstance(q, str):
        try:
            q = json.loads(q)
        except Exception:  # noqa: BLE001
            q = {}
    q = q or {}
    for key, kind in (("dashboard_id", "dashboard"), ("legacy_dashboard_id", "dashboard"),
                      ("genie_space_id", "genie"), ("alert_id", "alert"), ("job_info", "job"),
                      ("notebook_id", "notebook"), ("sql_query_id", "sql_editor")):
        v = q.get(key)
        if v:
            return kind, (json.dumps(v) if isinstance(v, dict) else str(v))
    c = (client or "").lower()
    for word, kind in (("dashboard", "dashboard"), ("genie", "genie"), ("notebook", "notebook"),
                       ("sql editor", "sql_editor"), ("job", "job")):
        if word in c:
            return kind, None
    return "other", None


def read_query_history(run, full_names: list[str], days: int = 90, limit: int = 5000) -> dict:
    """{"records": [...], "warnings": [...]} from system.query.history for the given tables."""
    shorts = sorted({fq.split(".")[-1].lower() for fq in full_names})
    pattern = "(" + "|".join(re.escape(s) for s in shorts) + ")"
    base = ("FROM system.query.history "
            "WHERE start_time >= current_timestamp() - make_dt_interval(:d) "
            "AND execution_status = 'FINISHED' AND lower(statement_text) LIKE '%select%' "
            f"AND lower(statement_text) RLIKE :pat ORDER BY start_time DESC LIMIT {int(limit)}")
    params = {"d": int(days), "pat": pattern}
    out = {"records": [], "warnings": []}
    try:
        df = run("SELECT statement_text, executed_by, CAST(start_time AS STRING) AS start_time, "
                 "to_json(query_source) AS qsrc, client_application " + base, params)
    except Exception:  # noqa: BLE001  (older schemas without query_source)
        try:
            df = run("SELECT statement_text, executed_by, CAST(start_time AS STRING) AS start_time, "
                     "NULL AS qsrc, client_application " + base, params)
        except Exception as e:  # noqa: BLE001
            out["warnings"].append(f"Query history unavailable ({str(e)[:160]}). "
                                   "Needs SELECT on system.query.history.")
            return out
    texts = [t for t in df["statement_text"].tolist() if t]
    if texts and all(str(t).strip().lower() == "<redacted>" for t in texts[:20]):
        out["warnings"].append("Query text is redacted. Only account admins and members of the "
                               "databricks_pii_access group can read it.")
        return out
    for r in df.itertuples(index=False):
        kind, sid = classify_source(r.qsrc, r.client_application)
        out["records"].append({"text": r.statement_text, "user": r.executed_by, "ts": r.start_time,
                               "source": kind, "source_id": sid})
    return out


def records_from_file(name: str, data: bytes) -> list[dict]:
    """A .sql file (statements split on ';') or a .csv with text/statement_text and optional
    user/executed_by, source, ts/start_time, source_id columns."""
    text = data.decode("utf-8-sig", errors="replace")
    if name.lower().endswith(".csv"):
        import io

        import pandas as pd
        df = pd.read_csv(io.StringIO(text))
        cols = {c.lower(): c for c in df.columns}
        tcol = cols.get("statement_text") or cols.get("text") or cols.get("sql") or cols.get("query")
        if not tcol:
            raise ValueError("The CSV needs a statement_text (or text / sql / query) column.")
        get = lambda r, *ks: next((r[cols[k]] for k in ks if k in cols and isinstance(r[cols[k]], str)), None)  # noqa: E731
        out = []
        for _, r in df.iterrows():
            src = (get(r, "source") or "file").lower().replace(" ", "_")
            out.append({"text": r[tcol], "user": get(r, "executed_by", "user", "author") or "unknown",
                        "ts": get(r, "start_time", "ts", "timestamp"), "source": src,
                        "source_id": get(r, "source_id", "asset")})
        return out
    stmts, cur = [], []
    for line in text.splitlines():
        cur.append(line)
        if line.rstrip().endswith(";"):
            stmts.append("\n".join(cur))
            cur = []
    if "".join(cur).strip():
        stmts.append("\n".join(cur))
    return [{"text": s, "user": "file", "ts": None, "source": "file", "source_id": name}
            for s in stmts if re.search(r"\bselect\b", s, re.I)]


# --------------------------------------------------------------------------- #
# Mining
# --------------------------------------------------------------------------- #
def _comments(text: str) -> list[str]:
    out = [m.strip() for m in re.findall(r"--\s*(.+)$", text, re.M)]
    out += [re.sub(r"\s+", " ", m).strip() for m in re.findall(r"/\*(.*?)\*/", text, re.S)]
    return [c for c in out if len(c) > 3 and not c.lower().startswith(("noqa", "type:"))]


def _lookups(onto: dict):
    tables, cols = {}, {}
    for cid, c in onto.get("classes", {}).items():
        for k in (c.get("source_table"), c.get("source_fqn"), cid):
            if k:
                tables[str(k).lower()] = cid
        cols[cid] = {}
        for pid, p in c.get("data_properties", {}).items():
            cols[cid][str(p.get("source_column") or pid).lower()] = p.get("source_column") or pid
        for col in c.get("source_columns") or []:
            cols[cid].setdefault(str(col).lower(), col)
    return tables, cols


def _key(kind: str, text: str) -> str:
    return "snp_" + hashlib.sha1(f"{kind}|{text.lower()}".encode()).hexdigest()[:10]


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s[:26], fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
    return None


APP_MARKER = "ontology-studio"
_APP_SIGNATURES = re.compile(
    r"AS\s+`?(__n|d\d+|nn\d+|e\d+)`?\s*(,|FROM)"                      # profiling (older app versions)
    r"|SUM\(CASE WHEN b\.v IS NOT NULL THEN 1 ELSE 0 END\)"              # join value-overlap checks
    r"|information_schema\.|system\.(query|access|billing)\.", re.I)


def is_app_query(text: str) -> bool:
    """The studio's own profiling / discovery / metadata SQL: not knowledge about the business."""
    return APP_MARKER in text or bool(_APP_SIGNATURES.search(text))


def mine(records: list[dict], onto: dict, dialect: str = "databricks") -> list[dict]:
    """Parse query records into aggregated snippets (without scores)."""
    import sqlglot
    from sqlglot import exp
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

    tables, cols = _lookups(onto)
    agg: dict[str, dict] = {}

    def resolve(tbl: exp.Table) -> str | None:
        parts = [p for p in (tbl.catalog, tbl.db, tbl.name) if p]
        for k in (".".join(parts), tbl.name):
            cid = tables.get(k.lower())
            if cid:
                return cid
        return None

    def add(kind, text, rec, classes, columns, name=None, bare=None, comment=None):
        k = _key(kind, text)
        s = agg.get(k)
        if s is None:
            s = agg[k] = {"id": k, "kind": kind, "text": text, "classes": sorted(classes),
                          "columns": sorted(columns), "uses": 0, "users": Counter(), "sources": Counter(),
                          "assets": set(), "names": Counter(), "comments": [], "first_seen": None,
                          "last_seen": None, "bare": bare}
        s["uses"] += 1
        s["users"][rec.get("user") or "unknown"] += 1
        s["sources"][rec.get("source") or "other"] += 1
        if rec.get("source_id"):
            s["assets"].add(f'{rec.get("source")}:{rec["source_id"]}')
        if name:
            s["names"][name] += 1
        if comment and comment not in s["comments"] and len(s["comments"]) < 3:
            s["comments"].append(comment)
        ts = rec.get("ts")
        if ts:
            ts = str(ts)
            s["first_seen"] = min(s["first_seen"] or ts, ts)
            s["last_seen"] = max(s["last_seen"] or ts, ts)

    for rec in records:
        text = str(rec.get("text") or "")
        if not text.strip():
            continue
        if is_app_query(text):
            continue
        comment = (_comments(text) or [None])[0]
        try:
            trees = sqlglot.parse(text, read=dialect)
        except Exception:  # noqa: BLE001
            continue
        for tree in trees:
            if tree is None:
                continue
            for select in tree.find_all(exp.Select):
                aliases: dict[str, str] = {}
                for tbl in select.find_all(exp.Table):
                    cid = resolve(tbl)
                    if cid:
                        aliases[(tbl.alias_or_name or tbl.name).lower()] = cid
                        aliases.setdefault(tbl.name.lower(), cid)
                if not aliases:
                    continue
                only = next(iter(set(aliases.values()))) if len(set(aliases.values())) == 1 else None

                def owner(col: exp.Column) -> str | None:
                    if col.table:
                        return aliases.get(col.table.lower())
                    if only and col.name.lower() in cols.get(only, {}):
                        return only
                    hits = [c for c in set(aliases.values()) if col.name.lower() in cols.get(c, {})]
                    return hits[0] if len(hits) == 1 else None

                def normalize(node) -> tuple[str, str, set, set] | None:
                    node = node.copy()
                    used_classes, used_cols, ok = set(), set(), True
                    for col in list(node.find_all(exp.Column)):
                        cid = owner(col)
                        if cid is None:
                            ok = False
                            break
                        real = cols[cid].get(col.name.lower(), col.name)
                        used_classes.add(cid)
                        used_cols.add(f"{cid}.{real}")
                        col.set("table", exp.to_identifier(cid))
                        col.set("this", exp.to_identifier(real))
                    if not ok or not used_classes:
                        return None
                    qualified = node.sql(dialect=dialect)
                    bare_node = node.copy()
                    for col in bare_node.find_all(exp.Column):
                        col.set("table", None)
                    return qualified, bare_node.sql(dialect=dialect), used_classes, used_cols

                # joins
                for join in select.args.get("joins") or []:
                    on = join.args.get("on")
                    if on is None:
                        continue
                    for eq in on.find_all(exp.EQ):
                        if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
                            a, b = owner(eq.left), owner(eq.right)
                            if a and b and a != b:
                                ca = cols[a].get(eq.left.name.lower(), eq.left.name)
                                cb = cols[b].get(eq.right.name.lower(), eq.right.name)
                                (x, xc), (y, yc) = sorted([(a, ca), (b, cb)])
                                add("join", f"{x}.{xc} = {y}.{yc}", rec, {x, y}, {f"{x}.{xc}", f"{y}.{yc}"},
                                    comment=comment)
                # filters
                where = select.args.get("where")
                if where is not None:
                    cond = where.this
                    parts = list(cond.flatten()) if isinstance(cond, exp.And) else [cond]
                    for part in parts:
                        if isinstance(part, exp.EQ) and isinstance(part.left, exp.Column) \
                                and isinstance(part.right, exp.Column):
                            continue                    # implicit join, not a business filter
                        if part.find(exp.Select):
                            continue
                        n = normalize(part)
                        if n:
                            add("filter", n[0], rec, n[2], n[3], bare=n[1] if len(n[2]) == 1 else None,
                                comment=comment)
                # aggregate expressions with a name
                for proj in select.expressions:
                    if isinstance(proj, exp.Alias) and proj.this.find(exp.AggFunc):
                        if proj.this.find(exp.Window):
                            continue
                        n = normalize(proj.this)
                        if n:
                            add("expression", n[0], rec, n[2], n[3], name=proj.alias.lower(),
                                bare=n[1] if len(n[2]) == 1 else None, comment=comment)
    out = []
    for s in agg.values():
        s["users"] = dict(s["users"].most_common())
        s["sources"] = dict(s["sources"].most_common())
        s["assets"] = sorted(s["assets"])[:20]
        s["name"] = s["names"].most_common(1)[0][0] if s["names"] else None
        s["names"] = dict(s["names"].most_common(5))
        out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Authority (OntoRank-style)
# --------------------------------------------------------------------------- #
def author_scores(snips: list[dict], trusted: dict[str, float] | None = None) -> dict[str, float]:
    """Per author: volume of work on these tables, how much of it is shared, and how many of
    their snippets other people also use; plus an admin boost for trusted authors."""
    volume, shared, adopted = Counter(), Counter(), Counter()
    for s in snips:
        users = s.get("users") or {}
        share = sum(n for k, n in (s.get("sources") or {}).items() if k in SHARED_SOURCES) / max(1, s["uses"])
        for u, n in users.items():
            volume[u] += n
            shared[u] += n * share
            if len(users) > 1:
                adopted[u] += 1
    if not volume:
        return {}
    vmax = max(volume.values())
    amax = max(adopted.values()) if adopted else 0
    out = {}
    for u in volume:
        v = math.log1p(volume[u]) / math.log1p(vmax)
        sh = shared[u] / volume[u]
        ad = adopted[u] / amax if amax else 0.0
        out[u] = 0.5 * v + 0.25 * sh + 0.25 * ad
    for u, boost in (trusted or {}).items():
        if u in out:
            out[u] = min(1.0, out[u] + float(boost))
    return out


def score(snips: list[dict], trusted: dict[str, float] | None = None,
          today: datetime | None = None) -> list[dict]:
    """Adds `authority` and `authority_parts` to each snippet (in place) and sorts by authority."""
    if not snips:
        return snips
    today = today or datetime.now(timezone.utc)
    authors = author_scores(snips, trusted)
    umax = max(s["uses"] for s in snips)
    rmax = max(len(s.get("users") or {}) for s in snips)
    for s in snips:
        users = s.get("users") or {}
        last = _parse_ts(s.get("last_seen"))
        age = (today - last).days if last else None
        parts = {
            "popularity": math.log1p(s["uses"]) / math.log1p(umax) if umax else 0.0,
            "reach": math.log1p(len(users)) / math.log1p(rmax) if rmax else 0.0,
            "author": (sum(authors.get(u, 0) * n for u, n in users.items()) / max(1, sum(users.values()))),
            "sharing": sum(n for k, n in (s.get("sources") or {}).items() if k in SHARED_SOURCES) / max(1, s["uses"]),
            "documented": 1.0 if s.get("comments") else 0.0,
            "recency": 0.5 ** (max(age, 0) / HALF_LIFE_DAYS) if age is not None else 0.5,
        }
        s["authority_parts"] = {k: round(v, 3) for k, v in parts.items()}
        s["authority"] = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3)
    snips.sort(key=lambda s: -s["authority"])
    return snips


EVIDENCE_KEYS = ("uses", "users", "sources", "names", "assets", "comments", "first_seen", "last_seen")


def _totals(s: dict) -> None:
    """Snippet totals = sum of its evidence over ingestions (each ingestion counted once)."""
    ev = list((s.get("evidence") or {}).values())
    s["uses"] = sum(e.get("uses", 0) for e in ev)
    for k in ("users", "sources", "names"):
        c = Counter()
        for e in ev:
            c.update(e.get(k) or {})
        s[k] = dict(c.most_common())
    s["assets"] = sorted({a for e in ev for a in e.get("assets") or []})[:20]
    s["comments"] = list(dict.fromkeys(c for e in ev for c in e.get("comments") or []))[:3]
    s["first_seen"] = min((e["first_seen"] for e in ev if e.get("first_seen")), default=None)
    s["last_seen"] = max((e["last_seen"] for e in ev if e.get("last_seen")), default=None)
    s["name"] = next(iter(s["names"]), None) if s["names"] else s.get("name")


def merge_into(onto: dict, new: list[dict], ingest: str = "default") -> dict:
    """Merge mined snippets into onto['snippets']. Evidence is kept per ingestion key (e.g.
    "query_history", "file:<sha1>", "ask:<trace id>"): re-learning from the same source replaces its
    evidence instead of counting it twice. Review decisions (status, promoted_to) are kept.
    Domains come from the snippet's classes. Returns counts."""
    cur = {s["id"]: s for s in onto.setdefault("snippets", [])}
    added = updated = 0
    for s in cur.values():                       # the new run of this source replaces the old one
        if s.get("evidence") is None:            # snippets saved before evidence tracking
            s["evidence"] = {"legacy": {k: s.get(k) for k in EVIDENCE_KEYS}}
        s["evidence"].pop(ingest, None)
    for s in new:
        ev = {k: s.get(k) for k in EVIDENCE_KEYS}
        old = cur.get(s["id"])
        if old is None:
            s = {k: v for k, v in s.items() if k not in EVIDENCE_KEYS}
            s.update({"status": "learned", "evidence": {ingest: ev}})
            cur[s["id"]] = s
            added += 1
        else:
            old["evidence"][ingest] = ev
            for k in ("bare", "classes", "columns"):
                old[k] = s.get(k, old.get(k))
            updated += 1
    for sid in [k for k, s in cur.items() if not s["evidence"]]:
        if cur[sid].get("status") == "learned":    # no evidence left and never reviewed
            cur.pop(sid)
        else:
            cur[sid]["evidence"] = {"legacy": {"uses": 0}}
    snips = list(cur.values())
    classes = onto.get("classes", {})
    for s in snips:
        _totals(s)
        doms = []
        for cid in s.get("classes") or []:
            doms += [d for d in classes.get(cid, {}).get("domains") or [] if d not in doms]
        s["domains"] = doms
    onto["snippets"] = score(snips, onto.get("trusted_authors"))
    return {"added": added, "updated": updated, "total": len(snips)}


def rescore(onto: dict) -> None:
    onto["snippets"] = score(onto.get("snippets", []), onto.get("trusted_authors"))


def experts(onto: dict, cid: str | None = None, top: int = 5) -> list[dict]:
    """People whose work the ontology learned from, ranked by authority-weighted uses."""
    pts, uses, kinds = Counter(), Counter(), defaultdict(set)
    for s in onto.get("snippets", []):
        if cid and cid not in (s.get("classes") or []):
            continue
        for u, n in (s.get("users") or {}).items():
            if u in ("file", "unknown"):
                continue
            pts[u] += n * s.get("authority", 0)
            uses[u] += n
            kinds[u].update(s.get("classes") or [])
    return [{"person": u, "score": round(p, 2), "snippet_uses": uses[u], "works_on": ", ".join(sorted(kinds[u])[:6])}
            for u, p in pts.most_common(top)]


def ask_record(question: str, sql: str, user: str | None) -> dict:
    """A thumbs-up Ask answer, as a query record (the question becomes the comment)."""
    return {"text": f"-- {question}\n{sql}", "user": user or "ask-user",
            "ts": datetime.now(timezone.utc).isoformat(), "source": "ask", "source_id": None}


def top_for_context(onto: dict, min_authority: float = 0.35, limit: int = 25) -> list[dict]:
    """Snippets worth sending to the model with a question: the ones promoted or with authority."""
    out = []
    for s in onto.get("snippets", []):
        if s.get("status") == "dismissed":
            continue
        if s.get("authority", 0) >= min_authority or s.get("status") == "promoted":
            item = {"kind": s["kind"], "sql": s["text"], "authority": s.get("authority")}
            if s.get("name"):
                item["name"] = s["name"]
            if s.get("comments"):
                item["note"] = s["comments"][0]
            out.append(item)
        if len(out) >= limit:
            break
    return out
