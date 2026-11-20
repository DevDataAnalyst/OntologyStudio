"""
What Ask needs around the SQL: join paths, the coverage check, verified examples, the glossary and
readiness.

Adapted from auto-ontology-mcp (github.com/gogoymh/auto-ontology-mcp, Apache-2.0: graph.py, search.py,
naming.py, guidance.py, service.check_answerable / get_query_context / check_readiness), which ports
NVIDIA Auto Ontology (Apache-2.0). See NOTICE. Works on the studio's ontology dict.

  join_path / connect / join_sql  shortest chain of modeled joins between classes, as FROM ... JOIN ...
  check_answerable                 does the model cover each part of a question, before any SQL
  context_for                      what Ask adds to its prompt: join path, verified examples, glossary, values
  save_example / similar_examples  question + SQL pairs a person confirmed (saved with the model)
  set_glossary                     abbreviations and jargon (MRR -> monthly recurring revenue)
  readiness                        can SQL run, how much is modeled and certified, the next step
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache

from sqlglot import exp

# --------------------------------------------------------------------------- #
# Lexical matching (no embeddings): token overlap, trigrams for typos, phrase containment
# --------------------------------------------------------------------------- #
STOP = frozenset("""a an the of for to in on at by with from and or per vs versus is are was were be been what
which who whom whose when where why how many much does do did show list give get find tell me my our their his her
its this that these those all any each every please can could would should will there than then into over under
between during last next previous current first top bottom most least total number count sum avg average min max
minimum maximum""".split())
CALENDAR = frozenset("""year years yearly month months monthly quarter quarters quarterly week weeks weekly day days
daily date dates today yesterday january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec ytd mtd""".split())
_C1, _C2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])"), re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN, _SPLIT = re.compile(r"[0-9A-Za-zÀ-￿]+"), re.compile(r"[^0-9A-Za-zÀ-￿]+")


def _words(name: str) -> list[str]:
    s = _C2.sub(" ", _C1.sub(" ", str(name or "").strip()))
    return [p for p in _SPLIT.split(s) if p]


def _singular(w: str) -> str:
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes", "zes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokens(text: str, drop_stop: bool = True) -> list[str]:
    out = []
    for raw in _TOKEN.findall(unicodedata.normalize("NFKC", str(text or ""))):
        for w in _words(raw) or [raw]:
            w = w.casefold()
            if not (drop_stop and w in STOP):
                out.append(_singular(w) if w.isascii() else w)
    return out


@lru_cache(maxsize=50000)
def _prep(text: str) -> tuple[frozenset, str]:
    return frozenset(tokens(text)), unicodedata.normalize("NFKC", text or "").casefold()


def _trigrams(s: str) -> set:
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _sim(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def score(query: str, fields: list[tuple[str, float]]) -> float:
    """0..1: how well `query` matches a thing described by weighted text fields."""
    return round(min(1.0, _raw(query, fields)), 4)


def _raw(query: str, fields: list[tuple[str, float]]) -> float:
    """Uncapped score, so a tight match ("Order" for "orders") outranks a looser one ("OrderDetail")."""
    qt = tokens(query) or tokens(query, drop_stop=False)
    if not qt:
        return 0.0
    qn = unicodedata.normalize("NFKC", query).casefold().strip()
    got = []
    for text, w in fields:
        fset, fnorm = _prep(str(text or ""))
        if not fset:
            continue
        total = 0.0
        for q in qt:
            if q in fset:
                total += 1.0
                continue
            total += max([_sim(q, f) * 0.8 for f in fset if abs(len(f) - len(q)) <= 3 and _sim(q, f) >= 0.45] or [0.0])
        cov = total / len(qt)
        if len(qn) >= 3 and qn in fnorm:
            cov = max(cov, 1.0)
        got.append(w * cov / (1.0 + 0.04 * max(0, len(fset) - len(set(qt)))))   # tight matches beat long ones
    got.sort(reverse=True)
    return (got[0] + 0.15 * got[1]) if len(got) > 1 else (got[0] if got else 0.0)


def docs(onto: dict) -> list[dict]:
    """Searchable things of the model: classes, columns, metrics, rules, pages, glossary entries."""
    out = []
    for cid, c in onto.get("classes", {}).items():
        if (c.get("certification") or {}).get("status") == "deprecated":
            continue
        props = c.get("data_properties") or {}
        out.append({"kind": "class", "name": cid, "class": cid,
                    "fields": [(cid, 1.0), (c.get("label") or "", 1.0), (" ".join(c.get("synonyms") or []), 0.95),
                               (c.get("source_table") or "", 0.6), (c.get("description") or "", 0.45),
                               (" ".join(p.get("label") or k for k, p in props.items()), 0.3)]})
        for pid, p in props.items():
            out.append({"kind": "column", "name": f"{cid}.{pid}", "class": cid,
                        "column": f"{c.get('source_table') or cid}.{p.get('source_column') or pid}",
                        "fields": [(p.get("label") or pid, 0.9), (p.get("source_column") or pid, 0.75),
                                   (f"{cid} {p.get('label') or pid}", 0.85), (" ".join(p.get("synonyms") or []), 0.9),
                                   (p.get("description") or "", 0.4)]})
    for m in onto.get("metrics", []):
        if m.get("status") != "deprecated":
            out.append({"kind": "metric", "name": m["name"], "class": m.get("base_class"),
                        "status": m.get("status", "draft"), "sql": m.get("sql"),
                        "fields": [(m["name"].replace("_", " "), 1.0), (" ".join(m.get("synonyms") or []), 0.95),
                                   (m.get("description") or "", 0.5), (str(m.get("sql") or ""), 0.25)]})
    for r in onto.get("rules", []):
        if r.get("status") != "deprecated" and r.get("name"):
            out.append({"kind": "rule", "name": r["name"], "class": (r.get("applies_to") or [None])[0],
                        "fields": [(r["name"].replace("_", " "), 0.9), (r.get("description") or "", 0.5)]})
    for pg in onto.get("pages", []):
        if pg.get("status") != "deprecated":
            out.append({"kind": "page", "name": pg["title"], "class": None,
                        "fields": [(pg["title"], 1.0), (" ".join(pg.get("synonyms") or []), 0.95),
                                   ((pg.get("body") or "")[:4000], 0.35)]})
    for g in onto.get("glossary") or []:
        out.append({"kind": "glossary", "name": g["name"], "definition": g.get("definition"), "class": None,
                    "fields": [(g["name"], 1.0), (g.get("definition") or "", 0.6)]})
    return out


def search(query: str, items: list[dict], kinds=None, limit: int = 10, min_score: float = 0.2) -> list[dict]:
    rank = {"class": 0, "metric": 1, "column": 2, "rule": 3, "page": 4, "glossary": 5}
    hits = []
    for d in items:
        if kinds and d["kind"] not in kinds:
            continue
        raw = _raw(query, d["fields"])
        if raw >= min_score:
            hits.append((raw, {**{k: v for k, v in d.items() if k != "fields"}, "score": round(min(1.0, raw), 4)}))
    hits.sort(key=lambda h: (-h[0], rank.get(h[1]["kind"], 9)))
    return [h for _, h in hits[:limit]]


# --------------------------------------------------------------------------- #
# Join paths
# --------------------------------------------------------------------------- #
def _graph(onto: dict) -> dict[str, list[dict]]:
    classes, g = onto.get("classes", {}), {}
    for pid, p in onto.get("object_properties", {}).items():
        j = p.get("join") or {}
        d, r = p.get("domain"), p.get("range")
        if not j or p.get("cardinality") == "one-to-many" or d not in classes or r not in classes:
            continue
        if j.get("via"):
            g.setdefault(d, []).append({"pid": pid, "from": d, "to": r, "kind": "many-to-many", "join": j})
            continue
        g.setdefault(d, []).append({"pid": pid, "from": d, "to": r, "kind": "to-one", "join": j, "reverse": False})
        g.setdefault(r, []).append({"pid": pid, "from": r, "to": d, "join": j, "reverse": True,
                                    "kind": "to-one" if p.get("cardinality") == "one-to-one" else "to-many"})
    order = {"to-one": 0, "many-to-many": 1, "to-many": 2}
    for edges in g.values():
        edges.sort(key=lambda e: order[e["kind"]])
    return g


def join_path(onto: dict, a: str, b: str) -> list[dict] | None:
    """Shortest chain of modeled joins from class a to class b ([] when a == b, None when unreachable)."""
    if a == b:
        return []
    g, seen, queue = _graph(onto), {a: None}, deque([a])
    while queue:
        u = queue.popleft()
        for e in g.get(u, []):
            if e["to"] not in seen:
                seen[e["to"]] = e
                if e["to"] == b:
                    hops, cur = [], b
                    while seen[cur] is not None:
                        hops.append(seen[cur])
                        cur = seen[cur]["from"]
                    return hops[::-1]
                queue.append(e["to"])
    return None


def connect(onto: dict, classes: list[str]) -> tuple[list[dict], list[str]]:
    """Joins that connect all the given classes (a tree grown from the first one) + the unreachable ones."""
    classes = [c for c in dict.fromkeys(classes) if c in onto.get("classes", {})]
    if not classes:
        return [], []
    have, hops, unreachable = {classes[0]}, [], []
    for c in classes[1:]:
        if c in have:
            continue
        paths = [p for p in (join_path(onto, h, c) for h in sorted(have)) if p is not None]
        if not paths:
            unreachable.append(c)
            continue
        for e in min(paths, key=len):
            if e["to"] not in have:
                hops.append(e)
                have.add(e["to"])
    return hops, unreachable


def _ident(name: str, d: str | None) -> str:
    return exp.to_identifier(str(name)).sql(dialect=d)


def _alias(name: str) -> str:
    return re.sub(r"\W+", "_", str(name)).strip("_").lower() or "t"


def join_sql(onto: dict, start: str, hops: list[dict], dialect: str | None = "databricks") -> str:
    """A ready FROM/JOIN clause for a chain of joins, with each table aliased by its name."""
    d = None if dialect in (None, "auto") else dialect
    classes, aliases = onto["classes"], {}

    def ref(table: str, fqn: str | None) -> str:
        parts = fqn.split(".") if fqn and fqn.count(".") == 2 else [table]
        return ".".join(_ident(p, d) for p in parts)

    def al(key: str, table: str) -> str:
        if key not in aliases:
            base, a, i = _alias(table), _alias(table), 2
            while a in aliases.values():
                a, i = f"{base}{i}", i + 1
            aliases[key] = a
        return aliases[key]

    def cls(cid):
        c = classes[cid]
        return ref(c.get("source_table") or cid, c.get("source_fqn")), al(cid, c.get("source_table") or cid)

    def pk(cid):
        props = classes[cid].get("data_properties") or {}
        return [str((props.get(p) or {}).get("source_column") or p) for p in classes[cid].get("identifier") or []]

    def col(a: str, ref_: str) -> str:
        return f"{a}.{_ident(ref_.rpartition('.')[2], d)}"

    sql, a0 = cls(start)
    lines = [f"FROM {sql} AS {a0}"]
    for e in hops:
        j, (tref, ta) = e["join"], cls(e["to"])
        fa = aliases[e["from"]]
        if e["kind"] == "many-to-many":
            via = j["via"]
            fqn = classes[e["from"]].get("source_fqn")
            vref = ref(via, ".".join(fqn.split(".")[:2] + [via]) if fqn and fqn.count(".") == 2 else None)
            va = al(f"via:{via}:{e['pid']}", via)
            on1 = " AND ".join(f"{col(va, r)} = {fa}.{_ident(k, d)}" for r, k in zip(j["from"], pk(e["from"])))
            on2 = " AND ".join(f"{ta}.{_ident(k, d)} = {col(va, r)}" for r, k in zip(j["to"], pk(e["to"])))
            lines += [f"JOIN {vref} AS {va} ON {on1}", f"JOIN {tref} AS {ta} ON {on2}"]
            continue
        dom, rng = (ta, fa) if e["reverse"] else (fa, ta)          # the key holder's columns are join["from"]
        on = " AND ".join(f"{col(dom, f)} = {col(rng, t)}" for f, t in zip(j["from"], j["to"]))
        lines.append(f"JOIN {tref} AS {ta} ON {on}")
    return "\n".join(lines)


def path_notes(onto: dict, hops: list[dict]) -> list[str]:
    """Fan-out and shared-parent warnings for a chain of joins, plus other roles for the same hop."""
    notes, into = [], {}
    for e in hops:
        if e["kind"] == "to-many":
            notes.append(f"{e['from']} → {e['to']} is one-to-many: each {e['from']} row repeats once per {e['to']} "
                         f"row, so aggregate {e['to']} first before summing {e['from']} columns.")
            if e["from"] in into:
                notes.append(f"{into[e['from']]} and {e['to']} are related only through {e['from']}: aggregate each "
                             f"in its own subquery, then join them on {e['from']}.")
        if e["kind"] == "to-one":
            into[e["to"]] = e["from"]
        others = [o["pid"] for o in _graph(onto).get(e["from"], []) if o["to"] == e["to"] and o["pid"] != e["pid"]]
        if others:
            notes.append(f"{e['from']} and {e['to']} have other joins too ({', '.join(others)}); "
                         f"this path uses {e['pid']}.")
    return notes


def path_view(onto: dict, a: str, b: str, dialect: str | None = "databricks") -> dict:
    hops = join_path(onto, a, b)
    if hops is None:
        return {"found": False, "hops": [], "sql": "", "notes": [f"No chain of modeled joins links {a} and {b}."]}
    rows = [{"from": e["from"], "to": e["to"], "relationship": e["pid"], "kind": e["kind"],
             "on": " AND ".join(f"{f} = {t}" for f, t in zip(e["join"]["from"], e["join"]["to"]))
             + (f" (via {e['join']['via']})" if e["join"].get("via") else "")} for e in hops]
    return {"found": True, "hops": rows, "sql": join_sql(onto, a, hops, dialect), "notes": path_notes(onto, hops)}


# --------------------------------------------------------------------------- #
# Coverage check (before any SQL)
# --------------------------------------------------------------------------- #
# Ported from Auto Ontology's entity coverage prompt (via auto-ontology-mcp guidance.ENTITY_RULES).
ENTITY_RULES = """Extract 1-5 entity phrases from the question:
- Include subject nouns and domain terms (invoice, customer, shipment) and qualified phrases (order shipment).
- Keep an aggregation word attached when it names a metric (total revenue, average order value), but not when it is
  only how the question is asked ("how many shipments" -> shipment).
- Keep a multi-word filter item together and keep names that identify something: products, brands, categories, places.
- Resolve glossary abbreviations to their definition (MRR -> monthly recurring revenue).
- Exclude bare verbs, numbers, calendar words and dates, standalone status adjectives and generic words (id, name,
  type, code, value)."""
NAME_LIKE = re.compile(r"name|title|category|type|status|city|country|region|state|brand|segment|label|desc", re.I)


def extract_entities(question: str, onto: dict, chat_fn=None) -> list[str]:
    """1-5 phrases to look up: by the model (chat_fn(system, user, max_tokens=...)) or, without one, the content
    words of the question."""
    if chat_fn is not None:
        gl = "; ".join(f"{g['name']} = {g.get('definition')}" for g in onto.get("glossary") or [])
        system = ENTITY_RULES + (f"\nGlossary: {gl}" if gl else "") + \
            '\nAnswer with ONLY a JSON array of strings, e.g. ["net revenue", "customer", "Chai"].'
        try:
            m = re.search(r"\[.*\]", chat_fn(system, question, max_tokens=200), re.S)
            ents = [str(x).strip() for x in json.loads(m.group(0)) if str(x).strip()] if m else []
            if ents:
                return list(dict.fromkeys(ents))[:5]
        except Exception:  # noqa: BLE001 - fall back to the words of the question
            pass
    words = [w for w in re.findall(r"[^\W\d_][\w'-]*", question)
             if w.lower() not in STOP and w.lower() not in CALENDAR and len(w) > 2]
    return list(dict.fromkeys(words))[:6]


def _word_in(word: str, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", str(text), re.I))


def _values(ent: str, onto: dict, run, dialect, budget: list) -> list[dict]:
    """Stored values naming the entity (as a whole word): profiled values and code meanings first, then (run
    given, while the budget lasts) one query over the model's name-like text columns."""
    e = ent.strip()
    if len(e) < 2:
        return []
    hits = []
    for cid, c in onto.get("classes", {}).items():
        for pid, p in (c.get("data_properties") or {}).items():
            for v in list(p.get("allowed_values") or []) + list(p.get("samples") or []):
                if isinstance(v, str) and _word_in(e, v):
                    hits.append({"class": cid, "column": f"{c.get('source_table') or cid}.{p.get('source_column') or pid}",
                                 "value": v})
    for vm in onto.get("value_meanings", []):
        for code, meaning in (vm.get("values") or {}).items():
            if meaning and _word_in(e, meaning):
                hits.append({"class": vm.get("class"), "column": vm.get("column"), "value": code, "meaning": meaning})
    if hits or run is None or budget[0] <= 0:
        return list({(h["column"], str(h["value"])): h for h in hits}.values())[:5]
    budget[0] -= 1
    cols = []
    for cid, c in onto.get("classes", {}).items():
        if (c.get("instance_count") or 0) > 5_000_000:              # ponytail: no live scans of huge tables
            continue
        for pid, p in (c.get("data_properties") or {}).items():
            col = str(p.get("source_column") or pid)
            text = re.search(r"STRING|CHAR|TEXT", str(p.get("sql_type") or ""), re.I) or \
                (not p.get("sql_type") and p.get("range") == "xsd:string")
            if text and NAME_LIKE.search(col) and not p.get("identifier") and not p.get("allowed_values") \
                    and not p.get("expression"):
                cols.append((cid, col))
    from . import sqlcheck
    try:
        return sqlcheck.find_value(run, onto, cols[:12], e, dialect)
    except Exception:  # noqa: BLE001
        return []


def check_answerable(question: str, onto: dict, entities: list[str] | None = None, run=None,
                     dialect: str | None = "databricks", chat_fn=None, probe_budget: int = 3) -> dict:
    """Per entity: covered (a class, column, metric, rule or page), covered_via_glossary, covered_by_value (a
    stored value), weak or not_covered; plus a verdict and the next step."""
    ents = entities or extract_entities(question, onto, chat_fn)
    items, budget, results = docs(onto), [probe_budget], []
    kinds = ["class", "column", "metric", "rule", "page"]
    for ent in ents:
        found = search(ent, items, kinds + ["glossary"], limit=6)
        gloss = [h for h in found if h["kind"] == "glossary"]
        hits = [h for h in found if h["kind"] != "glossary"][:3]
        top = hits[0] if hits else None
        entry = {"entity": ent}
        if gloss and gloss[0]["score"] >= 0.55:
            entry["glossary"] = {"name": gloss[0]["name"], "definition": gloss[0].get("definition")}
        if top and top["score"] >= 0.55:
            entry["state"] = "covered"
        elif entry.get("glossary") and (via := [h for h in search(entry["glossary"]["definition"] or "", items, kinds, 3)
                                                if h["score"] >= 0.55]):
            entry["state"], hits = "covered_via_glossary", via
        elif vals := _values(ent, onto, None, dialect, budget):
            entry["state"], entry["values"] = "covered_by_value", vals
        else:
            entry["state"], entry["_weak"] = None, bool(top and top["score"] >= 0.35)
        entry["matches"] = [{k: h.get(k) for k in ("kind", "name", "class", "score", "status")} for h in hits]
        results.append(entry)
    pending = [r for r in results if r["state"] is None]
    for r in sorted(pending, key=lambda r: not re.search(r"[A-Z0-9]", r["entity"])):   # names first
        if run is not None and budget[0] > 0 and (vals := _values(r["entity"], onto, run, dialect, budget)):
            r["state"], r["values"] = "covered_by_value", vals
    for r in results:
        weak = r.pop("_weak", False)
        if r["state"] is None:
            r["state"] = "weak" if weak else "not_covered"
    missing = [r["entity"] for r in results if r["state"] == "not_covered"]
    weak = [r["entity"] for r in results if r["state"] == "weak"]
    classes = []                                     # the class each entity lands on, for the join path
    for r in results:
        if r["state"] == "covered_by_value":
            classes += [v["class"] for v in r["values"][:1] if v.get("class")]
        elif r["state"] != "not_covered" and r["matches"] and r["matches"][0].get("class"):
            classes.append(r["matches"][0]["class"])
    classes = list(dict.fromkeys(classes))
    _, unreachable = connect(onto, classes)
    if not results:
        verdict, step = "not_answerable", "The question names nothing to look up."
    elif not missing and not weak:
        verdict, step = "answerable", "Every part of the question is covered."
    elif missing and len(missing) == len(results):
        verdict, step = "not_answerable", (f"Nothing in the model covers: {', '.join(missing)}. Say what data would "
                                           "be needed instead of guessing.")
    elif missing:
        verdict, step = "partially_answerable", (f"Not covered: {', '.join(missing)}. Do not substitute a similar "
                                                 "measure; say what is missing and answer the rest.")
    else:
        verdict, step = "uncertain", f"Weak matches for: {', '.join(weak)}. Check them before trusting the answer."
    if unreachable:
        step += f" {', '.join(unreachable)} cannot be joined to {classes[0]} in the model."
        verdict = "uncertain" if verdict == "answerable" else verdict
    covered = sum(1 for r in results if r["state"] not in ("not_covered", "weak")) + 0.5 * len(weak)
    return {"question": question, "entities": results, "verdict": verdict, "next_step": step,
            "coverage": round(covered / len(results), 2) if results else 0.0, "classes": classes,
            "unreachable": unreachable}


# --------------------------------------------------------------------------- #
# Verified examples, glossary, prompt context
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_example(onto: dict, question: str, sql: str, note: str = "", by: str = "") -> dict:
    """Keep a question and the SQL a person confirmed; saving the same question again replaces its SQL."""
    q, s = str(question or "").strip(), str(sql or "").strip().rstrip(";").strip()
    if not q or not s:
        raise ValueError("A verified example needs a question and its SQL.")
    ex = onto.setdefault("examples", [])
    item = {"question": q, "sql": s, "note": note or None, "by": by or None, "at": _now()}
    cur = next((e for e in ex if e.get("question", "").strip().casefold() == q.casefold()), None)
    if cur is None:
        ex.append(item)
    else:
        cur.update(item)
    return item


def delete_example(onto: dict, question: str) -> None:
    onto["examples"] = [e for e in onto.get("examples") or [] if e.get("question") != question]


def similar_examples(onto: dict, question: str, k: int = 3, min_score: float = 0.35) -> list[dict]:
    scored = [(score(question, [(e.get("question") or "", 0.9), (e.get("note") or "", 0.4)]), e)
              for e in onto.get("examples") or []]
    return [e for s, e in sorted(scored, key=lambda x: -x[0]) if s >= min_score][:k]


def set_glossary(onto: dict, rows: list[dict]) -> list[dict]:
    """Replace the glossary with the non-empty rows (one entry per name)."""
    out = {}
    for r in rows or []:
        name, definition = str(r.get("name") or "").strip(), str(r.get("definition") or "").strip()
        if name and definition and name.lower() != "nan" and definition.lower() != "nan":
            out[name.casefold()] = {"name": name, "definition": definition}
    onto["glossary"] = list(out.values())
    return onto["glossary"]


def context_for(question: str, onto: dict, classes: list[str], dialect: str | None = "databricks",
                coverage: dict | None = None) -> dict:
    """What Ask adds to its prompt for one question."""
    out: dict = {}
    hops, _ = connect(onto, (coverage or {}).get("classes") or classes)
    if hops:
        out["join_path"] = join_sql(onto, hops[0]["from"], hops, dialect)
        notes = path_notes(onto, hops)
        if notes:
            out["join_notes"] = notes
    ex = similar_examples(onto, question)
    if ex:
        out["verified_examples"] = [{"question": e["question"], "sql": e["sql"]} for e in ex]
    if onto.get("glossary"):
        out["glossary"] = {g["name"]: g["definition"] for g in onto["glossary"][:40]}
    for e in (coverage or {}).get("entities") or []:
        if e["state"] == "not_covered":
            out.setdefault("not_covered_by_the_model", []).append(e["entity"])
        for v in e.get("values") or []:
            out.setdefault("use_these_stored_values", []).append(f"{v['column']} = '{v['value']}'")
    return out


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #
def readiness(onto: dict, runner_kind: str | None) -> dict:
    classes = onto.get("classes", {})
    n = len(classes)
    described = sum(1 for c in classes.values() if c.get("description"))
    certified = sum(1 for c in classes.values() if (c.get("certification") or {}).get("status") == "certified")
    metrics = [m for m in onto.get("metrics", []) if m.get("status") != "deprecated"]
    cert_m = sum(1 for m in metrics if m.get("status") == "certified")
    pending = sum(1 for s in onto.get("suggested_relationships") or [] if s.get("status") == "proposed")
    joins = sum(1 for p in onto.get("object_properties", {}).values()
                if p.get("join") and p.get("cardinality") != "one-to-many")
    examples, glossary = len(onto.get("examples") or []), len(onto.get("glossary") or [])
    if runner_kind not in ("uc", "sqlite"):
        verdict, step = "no live data", ("Ask writes SQL but cannot run or check it on data: build or open the "
                                         "model from Unity Catalog tables (or a SQLite file).")
    elif pending:
        verdict, step = "joins to review", f"Review {pending} suggested join(s) in the Network tab; Ask uses accepted ones."
    elif n and described < n / 2:
        verdict, step = "partly described", (f"{n - described} of {n} tables have no business description: add them in "
                                             "the inspector or with LLM enrichment.")
    elif not metrics and not examples:
        verdict, step = "no shared definitions", "Add metrics and verified examples so answers reuse agreed definitions."
    elif not certified and not cert_m:
        verdict, step = "nothing certified", "Certify the tables and metrics a person agreed on."
    else:
        verdict, step = "ready", "Ask questions; paste any query into Check SQL to test it."
    def count(k: int, one: str, many: str | None = None) -> str:
        return f"{k} {one if k == 1 else many or one + 's'}"
    return {"verdict": verdict, "next_step": step, "ready": verdict == "ready",
            "facts": f"{count(n, 'table')} ({described} described, {certified} certified) · {count(joins, 'join')} "
                     f"({pending} suggested) · {count(len(metrics), 'metric')} ({cert_m} certified) · "
                     f"{count(examples, 'verified example')} · {count(glossary, 'glossary entry', 'glossary entries')}"}
