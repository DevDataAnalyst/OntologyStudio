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
    return yaml.safe_dump(keep, sort_keys=False, width=200, allow_unicode=True)


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
