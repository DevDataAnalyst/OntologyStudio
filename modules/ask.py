"""
"Ask" — test the ontology by generating SQL from a business question.

mode="ontology": the model sees the semantic layer (classes, synonyms, joins,
                 value meanings, rules, metrics) — what the ontology is for.
mode="baseline": the model sees only raw tables/columns — so you can compare
                 and see what the ontology adds.
"""
from __future__ import annotations

import re
import sqlite3

import pandas as pd
import yaml

from .llm_enrich import chat

PROMPT = """You write one {dialect} SQL query that answers the user's question.
{context_intro}
Rules: return ONLY the SQL, no explanation and no markdown fences. Read-only (SELECT/WITH).
Use exactly the table and column names given. {qualify}Limit results to 200 rows unless aggregated."""


GOV_KEYS = ("status", "owner", "certified_by", "certified_at", "domains", "learned_from", "provenance")


def ontology_context(onto: dict, snippets: list[dict] | None = None, max_page_chars: int = 1500) -> str:
    """The ontology as the model sees it. Deprecated metrics/rules/pages are left out, certified ones are
    flagged `certified: true` (prefer them), pages are included as governed explanations and the
    most authoritative learned snippets as examples of how people really query the data."""
    import copy
    onto = copy.deepcopy(onto)
    keep = {k: onto.get(k) for k in ("classes", "object_properties", "value_meanings",
                                     "rules", "metrics")}
    for k in ("suggested_relationships", "lineage", "layout"):
        keep.pop(k, None)
    retired = {cid for cid, c in (keep["classes"] or {}).items()
               if (c.get("certification") or {}).get("status") == "deprecated"}
    if retired:                                         # deprecated tables never reach the model
        keep["classes"] = {cid: c for cid, c in keep["classes"].items() if cid not in retired}
        keep["object_properties"] = {pid: p for pid, p in (keep["object_properties"] or {}).items()
                                     if p.get("domain") not in retired and p.get("range") not in retired}
        keep["metrics"] = [m for m in keep.get("metrics") or [] if m.get("base_class") not in retired]
        keep["rules"] = [r for r in keep.get("rules") or [] if not set(r.get("applies_to") or []) & retired]
        keep["value_meanings"] = [v for v in keep.get("value_meanings") or [] if v.get("class") not in retired]
    for c in (keep["classes"] or {}).values():          # trim noise the model doesn't need
        c.pop("instance_count", None)
        c.pop("source_columns", None)
        c.pop("ossie", None)                            # Apache Ossie round-trip details
        c.pop("domains", None)
        c.pop("certification", None)
        for p in c.get("data_properties", {}).values():
            for k in ("label", "cardinality", "sql_type", "provenance", "ossie"):
                p.pop(k, None)
    for kind in ("metrics", "rules"):
        items = []
        for x in keep.get(kind) or []:
            if x.get("status") == "deprecated":
                continue
            if x.get("status") == "certified":
                x["certified"] = True
            for k in GOV_KEYS:
                x.pop(k, None)
            items.append(x)
        keep[kind] = items
    for x in list((keep["object_properties"] or {}).values()) + list(keep["metrics"] or []):
        x.pop("ossie", None)
    pages = [{"title": p["title"], **({"certified": True} if p.get("status") == "certified" else {}),
              "text": (p.get("body") or "")[:max_page_chars]}
             for p in onto.get("pages", []) if p.get("status") != "deprecated"]
    pages.sort(key=lambda p: not p.get("certified"))
    if pages:
        keep["pages"] = pages
    if snippets:
        keep["learned_snippets"] = snippets
    return yaml.safe_dump(plain(keep), sort_keys=False, width=200, allow_unicode=True)


def plain(obj):
    """Values YAML/JSON can represent: profiled samples from Unity Catalog arrive as Decimal,
    datetime, date or bytes, which yaml.safe_dump cannot write."""
    import datetime as _dt
    from decimal import Decimal
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple, set)):
        return [plain(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return obj if obj == obj else None
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return "<binary>"
    if hasattr(obj, "item"):                            # numpy scalars
        try:
            return plain(obj.item())
        except Exception:  # noqa: BLE001
            pass
    return str(obj)


def baseline_context(onto: dict) -> str:
    lines = []
    fk_cols: dict[str, list[str]] = {}
    for p in onto["object_properties"].values():
        for ref in (p.get("join") or {}).get("from", []):
            tbl, _, col = ref.rpartition(".")
            fk_cols.setdefault(tbl, []).append(col)
    for c in onto["classes"].values():
        tbl = c.get("source_fqn") or c.get("source_table")
        cols = [p.get("source_column", pid) for pid, p in c["data_properties"].items()]
        cols += [x for x in fk_cols.get(c.get("source_table"), []) if x not in cols]
        lines.append(f'{tbl}({", ".join(cols)})')
    class_tables = {c.get("source_table") for c in onto["classes"].values()}
    for p in onto["object_properties"].values():   # junction tables collapsed into M:N
        j = p.get("join") or {}
        if j.get("via") and j["via"] not in class_tables:
            cols = [r.rpartition(".")[2] for r in j["from"] + j["to"]]
            lines.append(f'{j["via"]}({", ".join(dict.fromkeys(cols))})')
            class_tables.add(j["via"])
    return "\n".join(lines)


def build_context(question: str, onto: dict, mode: str = "ontology", dialect: str = "sqlite",
                  qualifier: str | None = None, snippets: list[dict] | None = None) -> str:
    """The system prompt for one question (kept separate so its size can be traced)."""
    o = onto
    if mode == "ontology":
        ctx = ("Use this ontology. Map business words via synonyms, join via `join`, decode codes "
               "via value_meanings, apply rules and metric SQL where relevant. Prefer items marked "
               "certified; pages explain business meaning; learned_snippets show how colleagues "
               "query the data (they name columns as Class.column; higher authority = more trusted):\n"
               + ontology_context(o, snippets))
    else:
        ctx = "Tables:\n" + baseline_context(o)
    if any(c.get("source_fqn") for c in o["classes"].values()):
        qualifier = None
        ctx += ("\nTable names: use each class's `source_fqn` (catalog.schema.table) in FROM/JOIN; "
                "`join` entries use short table names — map them to the matching source_fqn.")
    return PROMPT.format(
        dialect="Databricks SQL" if dialect == "databricks" else dialect,
        context_intro=ctx,
        qualify=f"Prefix every table with `{qualifier}.`. " if qualifier else "")


def clean_sql(text: str) -> str:
    return re.sub(r"^```(?:sql)?|```$", "", text.strip(), flags=re.M | re.I).strip().rstrip(";")


def generate_sql(question: str, onto: dict, mode: str = "ontology",
                 dialect: str = "sqlite", qualifier: str | None = None,
                 snippets: list[dict] | None = None) -> str:
    system = build_context(question, onto, mode, dialect, qualifier, snippets)
    return clean_sql(chat(system, question, max_tokens=1200))


def is_read_only(sql: str) -> bool:
    s = re.sub(r"--.*?$|/\*.*?\*/", "", sql, flags=re.S | re.M).strip().lower()
    return bool(re.match(r"^(select|with)\b", s)) and ";" not in s


def run_on_sqlite(sql: str, db_bytes: bytes | None = None, script: str | None = None) -> pd.DataFrame:
    import os
    import tempfile
    if db_bytes is not None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(db_bytes)
        conn = sqlite3.connect(f"file:{f.name}?mode=ro", uri=True)
        try:
            return pd.read_sql_query(sql, conn)
        finally:
            conn.close()
            os.unlink(f.name)
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script or "")
        return pd.read_sql_query(sql, conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Answering engine: focus, plan, generate, validate, run, self-correct
# --------------------------------------------------------------------------- #
ANSWER_PROMPT = """You are a senior analytics engineer. You write ONE {dialect} query that answers a business \
question over the data model below.

Answer with ONLY a JSON object, no markdown fences:
{{"reasoning": "2-4 short sentences: the tables, joins, metric definitions, filters and grain you chose",
 "assumptions": ["how you read anything ambiguous"],
 "sql": "one read-only SELECT or WITH query"}}

How to write the query:
- Map business words to tables, columns and metrics through names, labels, synonyms, pages and value meanings.
- When a metric (above all a certified one) or a learned snippet defines the measure asked for (e.g. "net revenue"),
  reuse its SQL expression exactly instead of inventing a new formula. Pages explain business meaning: follow them.
- Join only along the declared relationships (`join` from -> to). Never invent tables or columns; use the exact names.
- Grain: "for each month / by month / per month" means one row per month (GROUP BY the month), ordered
  chronologically; "in 1997" is a filter on the year. Return a readable period label (e.g. 1997-01) and, for
  entities, their names, not only ids.
- Name output columns in snake_case business terms; round money to 2 decimals; ORDER BY something meaningful.
- Lists of rows (no aggregation) end with LIMIT 200. No SELECT *.
- Apply business rules only when they are relevant to the question.
- Dates: {date_hints}
{qualify}
{context_intro}"""

DATE_HINTS = {
    "databricks": "use year(col), month(col), date_trunc('MONTH', col) and date_format(col, 'yyyy-MM'); "
                  "if a date column is a STRING, wrap it in to_date(col) or to_timestamp(col).",
    "sqlite": "dates are TEXT like '1997-07-04 00:00:00'; use strftime('%Y', col), strftime('%Y-%m', col) "
              "or substr(col, 1, 7); compare with strings such as col >= '1997-01-01'.",
    "postgres": "use date_trunc('month', col), extract(year from col), to_char(col, 'YYYY-MM').",
    "mysql": "use YEAR(col), MONTH(col), DATE_FORMAT(col, '%Y-%m').",
    "tsql": "use YEAR(col), MONTH(col), FORMAT(col, 'yyyy-MM').",
    "snowflake": "use DATE_TRUNC('MONTH', col), YEAR(col), TO_CHAR(col, 'YYYY-MM').",
    "bigquery": "use DATE_TRUNC(col, MONTH), EXTRACT(YEAR FROM col), FORMAT_DATE('%Y-%m', col).",
}
FIX_PROMPT = """Your previous query for this question {problem}

Previous query:
{sql}

Fix it. Check table and column names against the model, quoting, date handling and filters (use the sample
values to match case and format). Answer with the same JSON object (reasoning, assumptions, sql)."""
SQLGLOT_DIALECT = {"databricks": "databricks", "sqlite": "sqlite", "mysql": "mysql", "postgres": "postgres",
                   "tsql": "tsql", "oracle": "oracle", "snowflake": "snowflake", "bigquery": "bigquery",
                   "redshift": "redshift", "duckdb": "duckdb", "teradata": "teradata"}


def _words(text: str) -> set[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    out = set()
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if len(w) > 2:
            out.add(w)
            out.add(w[:-1] if w.endswith("s") else w)
    return out


def focus(question: str, onto: dict, top: int = 8) -> dict:
    """What in the ontology the question most likely needs: matched classes, columns and metrics
    (by names, labels, synonyms, descriptions, page titles), so the model looks there first."""
    q = _words(question)
    if not q:
        return {}
    cls_score: dict[str, float] = {}
    cols: list[str] = []
    for cid, c in onto.get("classes", {}).items():
        sc = 3 * len(q & _words(f"{cid} {c.get('label', '')} {c.get('source_table', '')}"))
        sc += 2 * len(q & _words(" ".join(c.get("synonyms") or [])))
        for pid, p in c.get("data_properties", {}).items():
            hit = len(q & _words(f"{pid} {p.get('label', '')} {p.get('source_column', '')} "
                                 + " ".join(p.get("synonyms") or [])))
            if hit:
                sc += hit
                cols.append(f"{cid}.{p.get('source_column') or pid}")
        if sc:
            cls_score[cid] = sc
    mets = []
    for m in onto.get("metrics", []):
        if m.get("status") == "deprecated":
            continue
        hit = len(q & _words(f"{m['name']} {m.get('description', '')} " + " ".join(m.get("synonyms") or [])))
        if hit:
            mets.append((hit + (1 if m.get("status") == "certified" else 0), m))
            cls_score[m.get("base_class")] = cls_score.get(m.get("base_class"), 0) + 2 * hit
    mets.sort(key=lambda x: -x[0])
    pages = [p["title"] for p in onto.get("pages", [])
             if p.get("status") != "deprecated" and q & _words(p.get("title", "") + " " + " ".join(p.get("synonyms") or []))]
    classes = [c for c, _ in sorted(cls_score.items(), key=lambda x: -x[1]) if c in onto.get("classes", {})][:top]
    # the join path between the focused classes (one hop), so the model sees how to connect them
    joins = []
    for pid, p in onto.get("object_properties", {}).items():
        j = p.get("join") or {}
        if j and p.get("cardinality") != "one-to-many" and (p.get("domain") in classes or p.get("range") in classes):
            if p.get("domain") in classes and p.get("range") in classes:
                joins.append(f"{p['domain']} -> {p['range']}: " + ", ".join(
                    f"{a} = {b}" for a, b in zip(j.get("from", []), j.get("to", []))))
    out = {"classes": classes, "columns": cols[:20],
           "metrics": [{"name": m["name"], "sql": m.get("sql"), "base_class": m.get("base_class"),
                        **({"certified": True} if m.get("status") == "certified" else {})} for _, m in mets[:6]],
           "joins": joins[:12], "pages": pages[:5]}
    return {k: v for k, v in out.items() if v}


def answer_prompt(question: str, onto: dict, mode: str = "ontology", dialect: str = "sqlite",
                  snippets: list[dict] | None = None) -> str:
    """System prompt for the answering engine (ontology or schema-only baseline)."""
    if mode == "ontology":
        ctx = ("DATA MODEL (ontology). learned_snippets show how colleagues query the data; they name columns "
               "as Class.column — translate to the class's table. Higher authority = more trusted.\n"
               + ontology_context(onto, snippets))
        f = focus(question, onto)
        if f:
            ctx = ("MOST RELEVANT FOR THIS QUESTION (start here):\n"
                   + yaml.safe_dump(plain(f), sort_keys=False, width=200, allow_unicode=True) + "\n" + ctx)
    else:
        ctx = "TABLES:\n" + baseline_context(onto)
    qualify = ""
    if any(c.get("source_fqn") for c in onto["classes"].values()):
        qualify = ("- Table names: use each class's `source_fqn` (catalog.schema.table) in FROM/JOIN; `join` entries "
                   "use short table names, so map them to the matching source_fqn.")
    return ANSWER_PROMPT.format(dialect="Databricks SQL" if dialect == "databricks" else dialect,
                                date_hints=DATE_HINTS.get(dialect, DATE_HINTS["databricks"]),
                                qualify=qualify, context_intro=ctx)


def parse_answer(text: str) -> dict:
    """{"sql", "reasoning", "assumptions"} from the model's reply; plain SQL replies still work."""
    import json
    t = re.sub(r"^```(?:json|sql)?|```$", "", text.strip(), flags=re.M | re.I).strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            d = json.loads(t[start:end + 1])
            if isinstance(d, dict) and d.get("sql"):
                a = d.get("assumptions") or []
                return {"sql": clean_sql(str(d["sql"])), "reasoning": str(d.get("reasoning") or ""),
                        "assumptions": [str(x) for x in (a if isinstance(a, list) else [a])]}
        except Exception:  # noqa: BLE001
            m = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', t, re.S)
            if m:
                return {"sql": clean_sql(json.loads(f'"{m.group(1)}"')), "reasoning": "", "assumptions": []}
    m = re.search(r"\b(WITH|SELECT)\b.*", t, re.S | re.I)
    return {"sql": clean_sql(m.group(0) if m else t), "reasoning": "", "assumptions": []}


def check_sql(sql: str, dialect: str) -> str | None:
    """Static checks before anything runs. Returns a problem description or None."""
    if not is_read_only(sql):
        return "is not a single read-only SELECT/WITH statement."
    try:
        import logging

        import sqlglot
        logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
        trees = [t for t in sqlglot.parse(sql, read=SQLGLOT_DIALECT.get(dialect, "databricks")) if t is not None]
        if len(trees) != 1:
            return "must be exactly one statement."
    except Exception as e:  # noqa: BLE001
        return f"does not parse: {str(e)[:300]}"
    return None


def answer(question: str, onto: dict, mode: str = "ontology", dialect: str = "sqlite",
           execute=None, snippets: list[dict] | None = None, max_attempts: int = 3, chat_fn=None) -> dict:
    """Generate SQL, check it, run it with `execute(sql) -> DataFrame` (None = don't run) and, when it fails
    or returns nothing, show the model the error and let it fix its own query (up to max_attempts).
    Returns sql, reasoning, assumptions, result, status, error, attempts and token / time counters."""
    import time as _t
    chat_fn = chat_fn or chat
    system = answer_prompt(question, onto, mode, dialect, snippets)
    out = {"sql": None, "reasoning": "", "assumptions": [], "result": None, "status": "error", "error": None,
           "attempts": [], "prompt_tokens": 0, "completion_tokens": 0, "llm_ms": 0, "sql_ms": 0}
    user, empty_retry_used, best = question, False, None
    for _ in range(max_attempts):
        t0 = _t.time()
        reply = chat_fn(system, user, max_tokens=1500)
        out["llm_ms"] += int((_t.time() - t0) * 1000)
        out["prompt_tokens"] += (len(system) + len(user)) // 4
        out["completion_tokens"] += max(1, len(reply) // 4)
        a = parse_answer(reply)
        out.update(sql=a["sql"], reasoning=a["reasoning"], assumptions=a["assumptions"])
        attempt = {"sql": a["sql"], "error": None, "rows": None}
        out["attempts"].append(attempt)
        problem = check_sql(a["sql"], dialect)
        if problem is None and execute is not None:
            try:
                t0 = _t.time()
                res = execute(a["sql"])
                out["sql_ms"] += int((_t.time() - t0) * 1000)
                attempt["rows"] = len(res)
                out["result"] = res
                best = {k: out[k] for k in ("sql", "reasoning", "assumptions", "result")}
                if len(res) == 0 and not empty_retry_used:
                    empty_retry_used = True
                    problem = ("ran but returned 0 rows. If that is not plausible, check filters, date formats "
                               "and value spelling/case against the samples.")
            except Exception as e:  # noqa: BLE001
                problem = f"failed when it ran: {str(e)[:600]}"
        if problem is None:
            out.update(status="ok", error=None)
            return out
        attempt["error"] = problem
        out.update(status="refused" if problem.startswith("is not a single") else "error",
                   error=f"The query {problem}", result=None)
        user = (f"Question: {question}\n\n"
                + FIX_PROMPT.format(problem=problem, sql=a["sql"]))
    if best is not None:                        # an earlier query ran (e.g. returned 0 rows): keep it
        out.update(best, status="ok", error=None)
    return out
