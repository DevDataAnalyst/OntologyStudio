"""
Reconciliation (WS7): BO report SQL (re-run on Databricks) vs the generated metric views.

For every report data-provider statement:
  L1  the captured BO SQL, transpiled from the source dialect and re-pointed at Unity Catalog
  L3  the same question asked of the metric view: SELECT <fields>, MEASURE(<m>) … WHERE <same filters> GROUP BY ALL
and one comparison query that FULL OUTER JOINs the two on the dimension columns (null-safe) and counts rows
whose measures differ beyond a tolerance.

In Track S there is no BO server, so L1 (BO's own SQL, executed) is the baseline. With a client BO, L0 (the
report's exported values) is compared to L1 first.

Column order assumption: BO query plans list the result objects' dimensions first, then measures, in
result-object order (true for the fixture; check it on client SQL).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from . import bo_sql

ABS_TOL, REL_TOL = 1e-6, 1e-9


def _lit(v) -> str:
    return str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"


def _alias_statement(sql: str, dialect: str, names: list[str]) -> str | None:
    tree, _ = bo_sql.parse(sql, dialect)
    if tree is None or not isinstance(tree, exp.Select):
        return None
    sel = tree.expressions
    if len(sel) != len(names):
        return None
    tree.set("expressions", [exp.alias_(e.unalias() if isinstance(e, exp.Alias) else e, n) for e, n in zip(sel, names)])
    return tree.sql(dialect=dialect)


def plan(sir: dict, concepts: dict, mv: dict) -> list[dict]:
    """One reconciliation item per report statement: L1 SQL, L3 SQL, comparison SQL (or why not)."""
    objects = {o["sir_id"]: o for o in sir["semantic"]["objects"]}
    uc = {k: v["uc_table"] for k, v in sir["mapping"].items() if v.get("uc_table")}
    dialect = sir["universe"]["dialect"]
    bool_cols = set()
    for t, v in sir["mapping"].items():
        for col, typ in (v.get("uc_columns") or {}).items():
            if typ == "Boolean":
                bool_cols.add((t, col.upper()))
    items = []
    for rpt in sir["reports"]:
        for dp in rpt["data_providers"]:
            dims = [objects[o] for o in dp["objects"] if o in objects and objects[o]["kind"] in ("dimension", "attribute")]
            meas = [objects[o] for o in dp["objects"] if o in objects and objects[o]["kind"] == "measure"]
            for st in dp["statements"]:
                item = {"report": rpt["bo_id"], "data_provider": dp["id"], "statement": st["sir_id"],
                        "custom_sql": dp["custom_sql"], "dims": [d["name"] for d in dims], "measures": [],
                        "l1_sql": None, "l3_sql": None, "compare_sql": None, "status": "ready", "reason": ""}
                # which measures this statement returns (multi-context data providers split measures)
                st_tables = set(st.get("tables") or [])
                mine = [m for m in meas if set(m.get("tables") or []) & st_tables
                        or any(t in (st.get("aliases") or {}) for t in (m.get("tables") or []))]
                if len(dp["statements"]) == 1:
                    mine = meas
                item["measures"] = [m["name"] for m in mine]
                names = [f"d{i}" for i in range(len(dims))] + [f"m{i}" for i in range(len(mine))]
                l1 = _alias_statement(st["sql"], dialect, names)
                if l1 is None:
                    item.update(status="manual", reason="statement columns don't line up with the result objects")
                    items.append(item)
                    continue
                l1_dbx, err = bo_sql.transpile_statement(l1, dialect, uc, bool_cols=bool_cols)
                if err:
                    item.update(status="manual", reason=f"L1 doesn't transpile: {err}")
                    items.append(item)
                    continue
                item["l1_sql"] = l1_dbx
                metric_names = [m.get("metric") for m in mine]
                from .to_mv import pick_views
                need = [d.get("field") for d in dims] + [objects[f"obj:{c['object']}"].get("field") for c in dp["conditions"]
                                                         if f"obj:{c['object']}" in objects]
                chosen = pick_views(mv, metric_names, need) if all(metric_names) else []
                if len(chosen) != 1:
                    item.update(status="manual", reason="the measures aren't all in one metric view: "
                                + ", ".join(f"{n}→{mv['measure_view'].get(n)}" for n in metric_names))
                    items.append(item)
                    continue
                view = chosen[0][0]
                fmap = mv["field_names"][view]
                sel, missing = [], []
                for i, d in enumerate(dims):
                    f = fmap.get(d.get("field") or "")
                    (sel.append(f"{f} AS d{i}") if f else missing.append(d["name"]))
                where = []
                for cnd in dp["conditions"]:
                    o = objects.get(f"obj:{cnd['object']}")
                    f = fmap.get((o or {}).get("field") or "")
                    vals = dp["prompt_values"].get(cnd["prompt"], []) if cnd.get("prompt") else cnd.get("values", [])
                    if not f:
                        missing.append(f"filter on {(o or {}).get('name')}")
                    elif cnd["operator"] == "Equal" and len(vals) == 1:
                        where.append(f"{f} = {_lit(vals[0])}")
                    elif vals:
                        where.append(f"{f} IN ({', '.join(_lit(v) for v in vals)})")
                for pf in dp["predefined_filters"]:
                    flt = concepts["filters"].get(_snake(objects[pf]["name"])) if pf in objects else None
                    expr = (flt or {}).get("expression", {}).get("value")
                    if not expr:
                        missing.append(f"filter {pf}")
                        continue
                    rewritten = _rewrite_filter(expr, fmap)
                    (where.append(rewritten) if rewritten else missing.append(f"filter {objects[pf]['name']}"))
                if missing:
                    item.update(status="manual", reason="not in the metric view: " + ", ".join(missing))
                    items.append(item)
                    continue
                for i, n in enumerate(metric_names):
                    sel.append(f"MEASURE({n}) AS m{i}")
                target = mv["targets"][view]
                l3 = f"SELECT {', '.join(sel)}\nFROM {target}"
                if where:
                    l3 += "\nWHERE " + " AND ".join(where)
                if dims:
                    l3 += "\nGROUP BY ALL"
                item["l3_sql"] = l3
                item["view"] = target
                item["compare_sql"] = _compare(l1_dbx, l3, len(dims), len(mine))
                items.append(item)
    return items


def _snake(s):
    return re.sub(r"\W+", "_", s.replace("%", " pct ")).strip("_").lower()


def _rewrite_filter(expr: str, fmap: dict) -> str | None:
    try:
        tree = sqlglot.parse_one(expr, read="databricks")
    except Exception:  # noqa: BLE001
        return None
    bad = []

    def ren(node):
        if isinstance(node, exp.Column) and node.table:
            f = fmap.get(f"{node.table}.{node.name}")
            if not f:
                bad.append(node.sql())
                return node
            return exp.column(f)
        return node
    out = tree.transform(ren)
    return None if bad else out.sql(dialect="databricks")


def _compare(l1: str, l3: str, nd: int, nm: int) -> str:
    on = " AND ".join(f"l1.d{i} <=> l3.d{i}" for i in range(nd)) or "TRUE"
    diffs = [f"(l1.m{i} IS NULL) <> (l3.m{i} IS NULL) OR ABS(COALESCE(l1.m{i}, 0) - COALESCE(l3.m{i}, 0)) > "
             f"GREATEST({ABS_TOL}, {REL_TOL} * ABS(COALESCE(l1.m{i}, 0)))" for i in range(nm)]
    per = ",\n  ".join(f"SUM(CASE WHEN {d} THEN 1 ELSE 0 END) AS m{i}_mismatches" for i, d in enumerate(diffs))
    only = ("SUM(CASE WHEN l1.d0 IS NULL AND l3.d0 IS NOT NULL THEN 1 ELSE 0 END) AS only_in_l3,\n  "
            "SUM(CASE WHEN l3.d0 IS NULL AND l1.d0 IS NOT NULL THEN 1 ELSE 0 END) AS only_in_l1") if nd else \
        "0 AS only_in_l3, 0 AS only_in_l1"
    return (f"WITH l1 AS (\n{l1}\n),\nl3 AS (\n{l3}\n)\nSELECT COUNT(*) AS rows_compared,\n  "
            f"(SELECT COUNT(*) FROM l1) AS l1_rows, (SELECT COUNT(*) FROM l3) AS l3_rows,\n  {only}"
            + (f",\n  {per}" if per else "") + f"\nFROM l1 FULL OUTER JOIN l3 ON {on}")


def run(items: list[dict], run_sql) -> list[dict]:
    """Execute the comparison queries with run_sql(sql) -> DataFrame; returns matrix rows."""
    out = []
    for it in items:
        row = {k: it[k] for k in ("report", "data_provider", "statement", "status", "reason")}
        row["measures"] = ", ".join(it["measures"])
        if it["status"] != "ready":
            out.append(row | {"result": "not run"})
            continue
        try:
            df = run_sql(it["compare_sql"])
            r = df.iloc[0].to_dict()
            mism = sum(int(v or 0) for k, v in r.items() if k.endswith("_mismatches"))
            only = int(r.get("only_in_l1") or 0) + int(r.get("only_in_l3") or 0)
            row.update(rows=int(r.get("rows_compared") or 0), l1_rows=int(r.get("l1_rows") or 0),
                       l3_rows=int(r.get("l3_rows") or 0), mismatched_cells=mism, unmatched_rows=only,
                       result="match" if mism == 0 and only == 0 else "mismatch")
        except Exception as e:  # noqa: BLE001
            row.update(result="error", reason=str(e).splitlines()[0][:300])
        out.append(row)
    return out


def summary(rows: list[dict]) -> dict:
    from collections import Counter
    c = Counter(r["result"] for r in rows)
    return dict(c) | {"total": len(rows)}
