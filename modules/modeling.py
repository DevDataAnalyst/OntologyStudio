"""
Modeling assistant: keep a model in step with its tables, show what is still unmodeled, review joins and
definitions, and keep a change log.

Adapted from auto-ontology-mcp (github.com/gogoymh/auto-ontology-mcp, Apache-2.0: service.scan_datasource,
get_modeling_worklist, list_relationships, set_relationship, suggest_sql_attributes, save_sql_attribute,
recent_changes and the review_ontology prompt), which ports NVIDIA Auto Ontology (Apache-2.0). See NOTICE.

  drift / apply_drift          re-read the model's Unity Catalog tables: new, dropped, retyped columns, profiles
  new_tables / add_tables      tables in the model's schemas it does not have yet (glob filters), with join suggestions
  worklist                     classes and columns still to describe, tables that gained columns
  unlinked_ids / observed_joins / multi_role / set_default   relationship review
  value_filters / save_rule    filter candidates from small value sets, saved as rules
  test_expression              run a metric or rule expression read-only before it is saved
  lint                         review-queue items (the MCP's review_ontology prompt)
  log_change                   who changed what and when, kept with the model
"""
from __future__ import annotations

import fnmatch
import re
from collections import Counter
from datetime import datetime, timezone

from sqlglot import exp

from . import sqlcheck
from .llm_enrich import lacks_description
from .ontology_builder import camel, label, xsd_type

ID_SUFFIX = re.compile(r"(?:^|_)(?:id|key|code|no|num|nbr|sk|fk)$", re.I)
ID_CAMEL = re.compile(r"[a-z0-9](?:Id|ID|Key|Code|No)$")
GRAIN = re.compile(r"\b(one row|each row|every row|per row|a row|rows? (?:is|are|of|for|per)|one record|each record"
                   r"|every record|records? (?:of|for)|one line|each line|per (?:order|customer|day|month|line))\b", re.I)
SELF_REF = re.compile(r"parent|manager|reports|boss|lead|supervisor", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cols(c: dict) -> list[str]:
    return list(c.get("source_columns") or [p.get("source_column") or pid
                                           for pid, p in (c.get("data_properties") or {}).items()])


def _key(c: dict) -> list[str]:
    props = c.get("data_properties") or {}
    return [str((props.get(pid) or {}).get("source_column") or pid) for pid in c.get("identifier") or []]


def _type_of(c: dict, col: str) -> str:
    for pid, p in (c.get("data_properties") or {}).items():
        if str(p.get("source_column") or pid).lower() == col.lower():
            return sqlcheck._coarse(p.get("sql_type"), p.get("range"))
    return ""


def is_id_like(col: str) -> bool:
    return bool(ID_SUFFIX.search(col) or ID_CAMEL.search(col))


# --------------------------------------------------------------------------- #
# Change log
# --------------------------------------------------------------------------- #
def log_change(onto: dict, what: str, by: str | None = None, keep: int = 500) -> None:
    log = onto.setdefault("changes", [])
    log.append({"at": _now(), "by": by or None, "what": str(what)[:300]})
    del log[:-keep]


# --------------------------------------------------------------------------- #
# Re-scan: drift between the model and its Unity Catalog tables
# --------------------------------------------------------------------------- #
def table_fqns(onto: dict) -> list[str]:
    return [c["source_fqn"] for c in onto.get("classes", {}).values() if str(c.get("source_fqn") or "").count(".") == 2]


def drift(onto: dict, ds) -> dict:
    """Compare the model with freshly extracted tables (uc.extract_objects): per class the new, dropped and
    retyped columns, plus classes whose table is gone."""
    fresh = {(t.full_name or "").lower(): t for t in ds.tables}
    out = {"changed": {}, "missing": [], "unchanged": 0}
    for cid, c in onto.get("classes", {}).items():
        fq = str(c.get("source_fqn") or "").lower()
        if fq.count(".") != 2:
            continue
        t = fresh.get(fq)
        if t is None:
            out["missing"].append(cid)
            continue
        have = {x.lower(): x for x in _cols(c)}
        now = {col.name.lower(): col for col in t.columns}
        retyped = []
        for pid, p in (c.get("data_properties") or {}).items():
            col = now.get(str(p.get("source_column") or pid).lower())
            if col and p.get("sql_type") and col.sql_type and str(p["sql_type"]).upper() != col.sql_type.upper():
                retyped.append({"column": col.name, "was": p["sql_type"], "now": col.sql_type})
        ch = {"new": [now[k].name for k in now if k not in have], "dropped": [have[k] for k in have if k not in now],
              "retyped": retyped}
        if any(ch.values()):
            out["changed"][cid] = ch
        else:
            out["unchanged"] += 1
    return out


def _prop(col) -> dict:
    p = {"label": label(col.name), "range": xsd_type(col.sql_type), "required": not col.nullable,
         "source_column": col.name, "sql_type": col.sql_type or None, "provenance": "rescan"}
    if col.comment:
        p["description"] = col.comment
    if col.distinct_values and p["range"] == "xsd:string" and len(col.distinct_values) <= 12:
        p["allowed_values"] = list(col.distinct_values)
    elif col.samples:
        p["samples"] = list(col.samples)[:5]
    if col.date_format:
        p["date_format"] = col.date_format
    return p


def apply_drift(onto: dict, ds, report: dict) -> list[str]:
    """Add new columns (marked for the worklist), drop vanished ones, update types, row counts and profiles.
    Descriptions, synonyms, roles, certification, joins and metrics are kept. Returns notes to show."""
    from . import semantics
    fresh, notes = {(t.full_name or "").lower(): t for t in ds.tables}, []
    tables = {str(c.get("source_table") or cid).lower(): cid for cid, c in onto["classes"].items()}
    for cid, c in onto["classes"].items():
        t = fresh.get(str(c.get("source_fqn") or "").lower())
        if t is None:
            continue
        props, ch = c.setdefault("data_properties", {}), report["changed"].get(cid, {})
        by_col = {str(p.get("source_column") or pid).lower(): pid for pid, p in props.items()}
        semantic = any(pid != (p.get("source_column") or pid) for pid, p in props.items())
        for col in t.columns:
            pid = by_col.get(col.name.lower())
            if pid:
                p = props[pid]
                if col.sql_type:
                    p["sql_type"], p["range"] = col.sql_type, xsd_type(col.sql_type)
                if col.distinct_values and p.get("allowed_values"):
                    p["allowed_values"] = list(col.distinct_values)
                elif col.samples and not p.get("allowed_values"):
                    p["samples"] = list(col.samples)[:5]
                if col.date_format:
                    p["date_format"] = col.date_format
            elif col.name in ch.get("new", []):
                new, i = camel(col.name) if semantic else col.name, 2
                base = new
                while new in props:
                    new, i = f"{base}{i}", i + 1
                props[new] = _prop(col)
        gone = {x.lower() for x in ch.get("dropped", [])}
        for pid in [pid for pid, p in props.items() if str(p.get("source_column") or pid).lower() in gone]:
            props.pop(pid)
        if any(k.lower() in gone for k in _key(c)):
            notes.append(f"{cid}: its key column was dropped; pick a new identifier in the inspector.")
        c["source_columns"] = [col.name for col in t.columns]
        if t.row_count is not None:
            c["instance_count"] = t.row_count
        if ch.get("new"):
            c["new_columns"] = sorted({*(c.get("new_columns") or []), *ch["new"]})
        for pid, p in onto["object_properties"].items():
            for ref in (p.get("join") or {}).get("from", []) + (p.get("join") or {}).get("to", []):
                tbl, _, col = ref.rpartition(".")
                if tables.get(tbl.lower()) == cid and col.lower() in gone:
                    notes.append(f"Relationship {pid} uses {ref}, which was dropped: repoint or delete it.")
    semantics.classify(onto)
    return list(dict.fromkeys(notes))


def _matches(fqn: str, patterns: str) -> bool:
    """MCP-style table filters: case-insensitive globs, % works like *, schema.table when the pattern has a
    dot, ! excludes (SALES_*, HR.EMP*, !*_LOG)."""
    pats = [p for p in re.split(r"[,\s]+", patterns or "") if p] or ["*"]
    inc, exc = [p for p in pats if not p.startswith("!")] or ["*"], [p[1:] for p in pats if p.startswith("!")]

    def hit(p: str) -> bool:
        p = p.lower().replace("%", "*")
        return fnmatch.fnmatchcase(".".join(fqn.lower().split(".")[-2:]) if "." in p else fqn.lower().split(".")[-1], p)
    return any(hit(p) for p in inc) and not any(hit(p) for p in exc)


def new_tables(run, onto: dict, patterns: str = "") -> list[str]:
    """Tables and views in the model's schemas that the model does not have yet, filtered by globs."""
    from . import uc
    have = {f.lower() for f in table_fqns(onto)}
    bridges = {str((p.get("join") or {}).get("via") or "").lower() for p in onto.get("object_properties", {}).values()}
    out = []
    for cat, sch in sorted({tuple(f.split(".")[:2]) for f in table_fqns(onto)}):
        for name in uc.list_tables(run, cat, sch)["table_name"]:
            fq = f"{cat}.{sch}.{name}"
            if fq.lower() not in have and name.lower() not in bridges and _matches(fq, patterns):
                out.append(fq)
    return out


def add_tables(run, onto: dict, fqns: list[str], progress=None) -> dict:
    """Read the given tables, add them as classes, and propose joins between them and the model (declared keys
    first, then name and value-overlap evidence) for review in the Network tab."""
    from . import discovery, semantics, uc
    from .ontology_builder import BuildOptions, _class_lookup, build_ontology, refresh_index
    from .schema_model import DatabaseSchema
    new = uc.extract_objects(run, fqns, profile=True, progress=progress)
    old = uc.extract_objects(run, table_fqns(onto), profile=False)
    for t in old.tables:                                   # the model's keys make its tables join targets
        cid = next((k for k, c in onto["classes"].items()
                    if str(c.get("source_fqn") or "").lower() == (t.full_name or "").lower()), None)
        if cid and _key(onto["classes"][cid]):
            t.primary_key = _key(onto["classes"][cid])
    semantic = any(cid != c.get("source_table") for cid, c in onto["classes"].items())
    built = build_ontology(new, BuildOptions(ontology_name=onto.get("ontology", {}).get("name", "model"),
                                             naming="semantic" if semantic else "raw", include_views=True))
    rename = {}
    for cid in built["classes"]:
        nid = cid
        while nid in onto["classes"]:
            nid += "_"
        rename[cid] = nid
    pmap = {}
    for pid in built["object_properties"]:
        npid, i = pid, 2
        while npid in onto["object_properties"] or npid in pmap.values():
            npid, i = f"{pid}{i}", i + 1
        pmap[pid] = npid
    for cid, c in built["classes"].items():
        onto["classes"][rename[cid]] = c
    for pid, p in built["object_properties"].items():
        p.update(domain=rename[p["domain"]], range=rename[p["range"]], inverse=pmap.get(p.get("inverse"), p.get("inverse")))
        onto["object_properties"][pmap[pid]] = p
    for vm in built.get("value_meanings") or []:
        onto.setdefault("value_meanings", []).append({**vm, "class": rename[vm["class"]]})
    # declared keys that cross between the new tables and the model, then discovered candidates
    combined = DatabaseSchema(name="rescan", source_type="unity_catalog", dialect="databricks",
                              tables=new.tables + old.tables)
    news = {(t.full_name or "").lower() for t in new.tables}
    cands = []
    for t in combined.tables:
        for fk in t.foreign_keys:
            ref = str(fk.ref_table).lower()
            if fk.name == "declared" and ((t.full_name or "").lower() in news) != (ref in news):
                cands.append({"from_table": t.full_name, "from_cols": fk.columns, "to_table": fk.ref_table,
                              "to_cols": fk.ref_columns, "confidence": 1.0, "evidence": ["declared foreign key"]})
    sug, _ = discovery.discover(combined, run_sql=run, table_ref=lambda t: uc.bq(*t.full_name.split(".")),
                                quote=lambda c: f"`{c}`", verify=True, threshold=1.01)   # all stay suggestions
    cands += [s for s in sug if s["from_table"].lower() in news or s["to_table"].lower() in news]
    look, added = _class_lookup(onto), 0
    have = {(s.get("domain"), tuple(s.get("from_cols") or []), s.get("range")) for s in onto.get("suggested_relationships") or []}
    for s in cands:
        d, r = look.get(str(s["from_table"]).lower()), look.get(str(s["to_table"]).lower())
        if d and r and (d, tuple(s["from_cols"]), r) not in have:
            have.add((d, tuple(s["from_cols"]), r))
            onto.setdefault("suggested_relationships", []).append(
                {**{k: s[k] for k in ("from_table", "from_cols", "to_table", "to_cols", "confidence", "evidence")},
                 "id": f"scan{len(onto.get('suggested_relationships') or [])}", "status": "proposed",
                 "domain": d, "range": r})
            added += 1
    semantics.classify(onto)
    refresh_index(onto)
    return {"classes": [rename[c] for c in built["classes"]], "suggestions": added}


# --------------------------------------------------------------------------- #
# Worklist
# --------------------------------------------------------------------------- #
def worklist(onto: dict) -> list[dict]:
    """Classes with modeling left to do, most urgent first: new columns from a re-scan, no description, a
    description that does not say what one row is, columns without a description."""
    rows = []
    for cid, c in onto.get("classes", {}).items():
        props = c.get("data_properties") or {}
        bare = [pid for pid, p in props.items()
                if lacks_description(p, pid, p.get("label"), p.get("source_column")) and not p.get("identifier")]
        desc = "" if lacks_description(c, cid, c.get("label"), c.get("source_table")) else c["description"]
        new, todo = c.get("new_columns") or [], []
        if new:
            todo.append(f"{len(new)} new column(s): {', '.join(new[:4])}")
        if not desc:
            todo.append("no description")
        elif not GRAIN.search(desc):
            todo.append("description does not say what one row is")
        if bare:
            todo.append(f"{len(bare)} of {len(props)} column(s) without a description")
        if todo:
            rows.append({"class": cid, "table": c.get("source_fqn") or c.get("source_table"), "to do": " · ".join(todo),
                         "_score": 5 * bool(new) + 2 * (not desc) + (not GRAIN.search(desc)) + len(bare) / max(1, len(props))})
    return [{k: v for k, v in r.items() if k != "_score"} for r in sorted(rows, key=lambda r: -r["_score"])]


# --------------------------------------------------------------------------- #
# Relationship review
# --------------------------------------------------------------------------- #
def _joined(onto: dict) -> dict[str, set]:
    """class -> lower names of the columns any relationship or open suggestion uses."""
    tables = {str(c.get("source_table") or cid).lower(): cid for cid, c in onto.get("classes", {}).items()}
    used: dict[str, set] = {}
    for p in onto.get("object_properties", {}).values():
        j = p.get("join") or {}
        for ref in j.get("from", []) + j.get("to", []):
            tbl, _, col = ref.rpartition(".")
            if tables.get(tbl.lower()):
                used.setdefault(tables[tbl.lower()], set()).add(col.lower())
    for s in onto.get("suggested_relationships") or []:
        if s.get("status") in ("proposed", "rejected"):
            used.setdefault(s.get("domain"), set()).update(c.lower() for c in s.get("from_cols") or [])
    return used


def unlinked_ids(onto: dict) -> list[dict]:
    """Identifier-like columns that are no class's key and take part in no relationship, with the classes whose
    single-column key could be their target (same type family; name matches first)."""
    classes, used = onto.get("classes", {}), _joined(onto)
    keys = {cid: [k.lower() for k in _key(c)] for cid, c in classes.items()}
    out = []
    for cid, c in classes.items():
        for col in _cols(c):
            low = col.lower()
            if not is_id_like(col) or low in used.get(cid, set()) or keys[cid] == [low]:
                continue
            stem = re.sub(r"_?(id|key|code|no|num|nbr|sk|fk)$", "", low)
            fam = sqlcheck._family(_type_of(c, col))
            targets = []
            for t, tc in classes.items():
                if len(keys[t]) != 1 or (t == cid and not SELF_REF.search(col)):
                    continue
                tfam = sqlcheck._family(_type_of(tc, keys[t][0]))
                if fam and tfam and fam != tfam:
                    continue
                words = {t.lower(), str(tc.get("source_table") or "").lower()}
                targets.append((not any(stem and (stem in w or w.rstrip("s") in stem) for w in words if w), t))
            if re.search(r"code$", low) and not any(not miss for miss, _ in targets):
                continue                          # postal_code, iso_code: a *_code is an id only if a table is named so
            out.append({"class": cid, "column": col, "targets": [t for _, t in sorted(targets)][:8]})
    return out


def observed_joins(onto: dict, sqls: list[str], dialect: str | None = "databricks") -> list[dict]:
    """Joins in queries people ran or confirmed (Ask, Check SQL, verified examples) that the model has not
    modeled, reviewed or suggested yet, most used first."""
    cat, seen, text = sqlcheck.catalog(onto), Counter(), {}
    for sql in sqls:
        for key, lbl in sqlcheck.joins_in(sql, onto, dialect, cat):
            if key not in cat["rels"]:
                seen[key] += 1
                text[key] = lbl
    out = []
    for key, n in seen.most_common(20):
        (ta, ca), (tb, cb) = sorted(key)
        out.append({"join": text[key], "uses": n, "a": (cat["tables"][ta]["cid"], ca), "b": (cat["tables"][tb]["cid"], cb)})
    return [o for o in out if o["a"][0] and o["b"][0]]


def add_observed(onto: dict, item: dict) -> str:
    """Model an observed join: the side whose column is its class key is the referenced side."""
    from . import ontology_edit as ed
    (ca, cola), (cb, colb) = item["a"], item["b"]
    names = {cid: {k.lower(): k for k in _cols(onto["classes"][cid])} for cid in (ca, cb)}
    cola, colb = names[ca].get(cola, cola), names[cb].get(colb, colb)
    if [k.lower() for k in _key(onto["classes"][ca])] == [cola.lower()]:
        (ca, cola), (cb, colb) = (cb, colb), (ca, cola)
    return ed.add_relationship(onto, ca, cb, [cola], [colb], provenance="observed")


def multi_role(onto: dict) -> list[dict]:
    """Pairs of classes joined by more than one relationship (billing vs delivery address): each needs a role,
    and one should be the default for an unqualified question."""
    ops, pairs = onto.get("object_properties", {}), {}
    for pid, p in ops.items():
        if p.get("join") and p.get("cardinality") in ("many-to-one", "one-to-one"):
            pairs.setdefault((p["domain"], p["range"]), []).append(pid)
    return [{"domain": d, "range": r, "relationships": pids,
             "default": next((x for x in pids if ops[x].get("default")), None)}
            for (d, r), pids in pairs.items() if len(pids) > 1]


def containment(run, onto: dict, cid: str, col: str, target: str, dialect: str | None = "databricks") -> float | None:
    """Share of sampled distinct values of cid.col found in target's single-column key (None: cannot tell)."""
    cat, key = sqlcheck.catalog(onto), _key(onto["classes"][target])
    a, b = cat["by_class"].get(cid), cat["by_class"].get(target)
    if not a or not b or len(key) != 1 or col.lower() not in cat["tables"][a]["cols"]:
        return None
    return sqlcheck._containment(run, cat, (a, col.lower()), (b, key[0].lower()), dialect)


def set_default(onto: dict, pid: str) -> None:
    ops = onto["object_properties"]
    p = ops[pid]
    for qid, q in ops.items():
        if q.get("join") and q.get("domain") == p["domain"] and q.get("range") == p["range"] \
                and q.get("cardinality") in ("many-to-one", "one-to-one"):
            q["default"] = qid == pid


# --------------------------------------------------------------------------- #
# Filters from small value sets, and testing expressions on the data
# --------------------------------------------------------------------------- #
def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60]


def value_filters(onto: dict, per_column: int = 4, limit: int = 40) -> list[dict]:
    """Filter candidates (status = 'Shipped') from columns with a short, complete list of values, skipping
    filters a rule already holds. Literals encode business categories, so they make good named filters."""
    have = {re.sub(r"\s+", "", str(r.get("sql_hint") or "").lower()) for r in onto.get("rules", [])}
    meanings = {(v.get("class"), v.get("property")): v.get("values") or {} for v in onto.get("value_meanings", [])}
    out = []
    for cid, c in onto.get("classes", {}).items():
        table = c.get("source_table") or cid
        for pid, p in (c.get("data_properties") or {}).items():
            vals = [v for v in p.get("allowed_values") or [] if isinstance(v, str)]
            if p.get("identifier") or not 1 < len(vals) <= 8:
                continue
            col = p.get("source_column") or pid
            for v in vals[:per_column]:
                sql = f"{table}.{col} = '{v.replace(chr(39), chr(39) * 2)}'"
                if re.sub(r"\s+", "", sql.lower()) in have:
                    continue
                meaning = meanings.get((cid, pid), {}).get(v) or v
                out.append({"class": cid, "name": _snake(f"{p.get('label') or col} {meaning}"), "filter": sql,
                            "description": f"{cid} rows whose {p.get('label') or col} is {meaning}"})
    return out[:limit]


def save_rule(onto: dict, item: dict) -> str:
    names = {r.get("name") for r in onto.get("rules", [])}
    name, i = item["name"], 2
    while name in names:
        name, i = f"{item['name']}_{i}", i + 1
    onto.setdefault("rules", []).append({"name": name, "description": item.get("description") or "",
                                         "applies_to": [item["class"]], "sql_hint": item["filter"], "synonyms": [],
                                         "provenance": "suggested", "status": "draft"})
    return name


def test_expression(run, onto: dict, cid: str, expr: str, kind: str = "measure",
                    dialect: str | None = "databricks") -> str | None:
    """Run a metric (measure) or rule (filter) expression read-only against its class's table. Returns the
    database's error, or None when it ran."""
    c = onto["classes"][cid]
    d = sqlcheck.glot(dialect)
    parts = c["source_fqn"].split(".") if str(c.get("source_fqn") or "").count(".") == 2 else [c.get("source_table") or cid]
    ref = ".".join(exp.to_identifier(p).sql(dialect=d) for p in parts)
    sql = f"SELECT COUNT(*) AS n FROM {ref} WHERE {expr}" if kind == "filter" else f"SELECT {expr} AS v FROM {ref} LIMIT 1"
    try:
        sqlcheck.parse(sql, d)
        run(sql)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e).splitlines()[0][:400]


# --------------------------------------------------------------------------- #
# Lints for the review queue
# --------------------------------------------------------------------------- #
_METRIC_CHECKS: dict = {}


def _metric_problem(onto: dict, m: dict, cat: dict, dialect: str | None) -> str | None:
    """Static check of a metric: its SQL over its base class, joined along the model to any other table it names."""
    from .answering import connect, join_sql
    base = m.get("base_class") or ""
    if base not in onto["classes"] or not m.get("sql"):
        return None
    by_name = {}
    for cid, c in onto["classes"].items():
        for n in (cid, c.get("source_table")):
            if n:
                by_name[str(n).lower()] = cid
    try:
        quals = {col.table.lower() for col in sqlcheck.parse(f"SELECT {m['sql']}", sqlcheck.glot(dialect))
                 .find_all(exp.Column) if col.table}
    except Exception as e:  # noqa: BLE001
        return str(e)
    if any(q not in by_name for q in quals):
        return None                                  # qualifiers the model cannot place: leave it to a person
    others = [by_name[q] for q in quals if by_name[q] != base]
    key = (m["sql"], base, tuple(sorted(others)), tuple(_cols(onto["classes"][base])), dialect)
    if key not in _METRIC_CHECKS:                   # metric checks are pure: cache them across reruns
        if len(_METRIC_CHECKS) > 5000:
            _METRIC_CHECKS.clear()
        hops, unreachable = connect(onto, [base, *others])
        if unreachable:
            _METRIC_CHECKS[key] = f"it reads {', '.join(unreachable)}, which no modeled join links to {base}"
        else:
            r = sqlcheck.validate(f"SELECT {m['sql']} AS v {join_sql(onto, base, hops, dialect)}", onto, dialect,
                                  cat=cat)
            _METRIC_CHECKS[key] = (r["errors"] or [None])[0]
    return _METRIC_CHECKS[key]


def lint(onto: dict, dialect: str | None = "databricks") -> list[dict]:
    """Review-queue items for gaps and risks in the model; nothing is changed or certified here."""
    items, classes = [], onto.get("classes", {})
    grainless = [cid for cid, c in classes.items() if not GRAIN.search(c.get("description") or "")]
    if grainless:
        items.append({"id": "lint:grain", "type": "lint", "priority": 0.45, "domains": [],
                      "title": f"{len(grainless)} table(s) do not say what one row is",
                      "why": ", ".join(grainless[:12]) + ". A description that states the grain (one row per order "
                             "line) stops double counting and helps Ask pick the right table.",
                      "fix": "Modeling worklist → ✨ Draft with the LLM, or the class inspector."})
    for m in multi_role(onto):
        if not m["default"]:
            ops = onto["object_properties"]
            items.append({"id": f"lint:default:{m['domain']}:{m['range']}", "type": "lint", "priority": 0.55,
                          "domains": [], "title": f"Choose the default join between {m['domain']} and {m['range']}",
                          "why": f"{len(m['relationships'])} relationships: " + "; ".join(
                              f"{pid} ({ops[pid].get('role') or 'no role'})" for pid in m["relationships"])
                          + ". Ask, join paths and the SQL checker need to know which one a plain question means.",
                          "fix": "Modeling worklist → Relationships, or the relationship inspector."})
    un = unlinked_ids(onto)
    if un:
        items.append({"id": "lint:unlinked", "type": "lint", "priority": 0.5, "domains": [],
                      "title": f"{len(un)} identifier column(s) have no relationship",
                      "why": ", ".join(f"{u['class']}.{u['column']}" for u in un[:10])
                             + ". If they point at another table, model the join; if not, dismiss this.",
                      "fix": "Modeling worklist → Relationships → Unlinked identifier columns."})
    text = " ".join(str(m.get("sql") or "").lower() for m in onto.get("metrics", []) if m.get("status") != "deprecated")
    bare = [f"{cid}.{p.get('source_column') or pid}" for cid, c in classes.items()
            for pid, p in (c.get("data_properties") or {}).items()
            if p.get("semantic_role") == "measure" and str(p.get("source_column") or pid).lower() not in text]
    if bare:
        items.append({"id": "lint:measures", "type": "lint", "priority": 0.4, "domains": [],
                      "title": f"{len(bare)} measure column(s) have no metric",
                      "why": ", ".join(bare[:10]) + ". Without a metric every answer re-derives the formula.",
                      "fix": "Add a metric in the class inspector (it is tested on the data before it is saved)."})
    cat = sqlcheck.catalog(onto)
    for m in onto.get("metrics", [])[:80]:
        if m.get("status") == "deprecated":
            continue
        problem = _metric_problem(onto, m, cat, dialect)
        if problem:
            items.append({"id": f"lint:metric:{m['name']}", "type": "lint", "priority": 0.75, "domains": m.get("domains") or [],
                          "title": f"Metric {m['name']} does not check out",
                          "why": f"`{m['sql']}` on {m.get('base_class')}: {problem}",
                          "fix": "Fix its SQL in the class inspector, or deprecate it."})
    return items
