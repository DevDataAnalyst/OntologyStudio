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


def ontology_context(onto: dict) -> str:
    import copy
    onto = copy.deepcopy(onto)
    keep = {k: onto.get(k) for k in ("classes", "object_properties", "value_meanings",
                                     "rules", "metrics")}
    for k in ("suggested_relationships", "lineage", "layout"):
        keep.pop(k, None)
    for c in (keep["classes"] or {}).values():          # trim noise the model doesn't need
        c.pop("instance_count", None)
        c.pop("source_columns", None)
        for p in c.get("data_properties", {}).values():
            for k in ("label", "cardinality", "sql_type", "provenance"):
                p.pop(k, None)
    return yaml.safe_dump(keep, sort_keys=False, width=200)


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


def generate_sql(question: str, onto: dict, mode: str = "ontology",
                 dialect: str = "sqlite", qualifier: str | None = None) -> str:
    o = onto
    if mode == "ontology":
        ctx = ("Use this ontology. Map business words via synonyms, join via `join`, decode codes "
               "via value_meanings, apply rules and metric SQL where relevant:\n"
               + ontology_context(o))
    else:
        ctx = "Tables:\n" + baseline_context(o)
    if any(c.get("source_fqn") for c in o["classes"].values()):
        qualifier = None
        ctx += ("\nTable names: use each class's `source_fqn` (catalog.schema.table) in FROM/JOIN; "
                "`join` entries use short table names — map them to the matching source_fqn.")
    system = PROMPT.format(
        dialect="Databricks SQL" if dialect == "databricks" else dialect,
        context_intro=ctx,
        qualify=f"Prefix every table with `{qualifier}.`. " if qualifier else "")
    sql = chat(system, question, max_tokens=1200)
    return re.sub(r"^```(?:sql)?|```$", "", sql.strip(), flags=re.M | re.I).strip().rstrip(";")


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
