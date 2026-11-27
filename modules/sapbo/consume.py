"""
Power BI consumption helpers (WS6). What can be produced from the app; building and publishing the Power BI
reports themselves happens in Power BI Desktop / the Power BI service.

Pattern 1  metric view → Power BI semantic model (replicated): `dax_measures()` translates each metric view
           measure into DAX over a star model of the same tables (TMDL text via `tmdl_measures()`), and says
           which measures couldn't be translated (they'd need hand-written DAX = duplicated modelling).
Pattern 2  SQL over metric views → Power BI (visualization only): `p2_views()` writes one plain SQL view per
           report block (SELECT fields, MEASURE(m) … GROUP BY ALL), so Power BI reads pre-aggregated rows and
           doesn't need MEASURE(). Non-additive columns are flagged "Do not summarize".
Pattern 3  metric view + extra DAX: `classify_dax()` tells a presentation extension (only governed measures,
           safe functions) from a semantic override (re-aggregates raw columns, changes a governed measure's
           filters, or reuses a governed name with different logic). `PATTERN3_SAMPLES` are the six planted
           measures from the roadmap test plan.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from . import normalize

NON_ADDITIVE = ("ratio", "distinct_count", "average", "derived", "multi_fact", "time")


# ------------------------------------------------------------------------------------------------ Pattern 2
def block_specs(sir: dict, concepts: dict, mv: dict) -> list[dict]:
    """Report blocks (the 'visuals') mapped to one metric view each: fields + measures."""
    objects = {o["sir_id"]: o for o in sir["semantic"]["objects"]}
    specs = []
    for rpt in sir["reports"]:
        by_label: dict[str, dict] = {}
        for dp in rpt["data_providers"]:
            for oid in dp["objects"]:
                o = objects.get(oid)
                if o and o["name"] not in by_label:
                    by_label[o["name"]] = {"field": o.get("field"), "metric": o.get("metric")}
        for calc in concepts["report_calcs"]:
            if calc["report"] == rpt["bo_id"]:
                by_label.setdefault(calc["label"], {"field": calc.get("field"), "metric": calc.get("metric"),
                                                    "calc": calc})
        for blk in rpt["blocks"]:
            measures = [(lbl, by_label.get(lbl, {}).get("metric")) for lbl in blk.get("measures", [])]
            axes = [(lbl, by_label.get(lbl, {}).get("field")) for lbl in blk.get("axes", [])]
            from .to_mv import pick_views
            chosen = pick_views(mv, [m for _, m in measures if m and mv["measure_view"].get(m)],
                                [f for _, f in axes])
            spec = {"report": rpt["bo_id"], "block": blk["id"], "type": blk.get("type"), "axes": axes,
                    "measures": measures, "ranking": blk.get("ranking"), "views": [v for v, _ in chosen], "parts": []}
            # one part per metric view the block needs (BO merges data providers; Power BI would use two visuals
            # or a composite model)
            for view, in_view in chosen:
                fmap = mv["field_names"][view]
                cols = [(lbl, fmap.get(f or "")) for lbl, f in axes]
                ms = [(lbl, m) for lbl, m in measures if m in in_view]
                spec["parts"].append({"view": view, "target": mv["targets"][view], "fields": cols, "measures": ms})
            spec["not_converted"] = [lbl for lbl, m in measures if not m or not mv["measure_view"].get(m)]
            specs.append(spec)
    return specs


def p2_views(specs: list[dict], catalog: str = "cnfinance", schema: str = "semantic") -> list[dict]:
    out = []
    for s in specs:
        for i, part in enumerate(s["parts"]):
            fields = [(lbl, f) for lbl, f in part["fields"] if f]
            missing = [lbl for lbl, f in part["fields"] if not f]
            sel = [f"{f} AS `{lbl}`" for lbl, f in fields] + [f"MEASURE({m}) AS `{lbl}`" for lbl, m in part["measures"]]
            name = f"v_{s['report'].lower()}_{s['block'].lower()}" + (f"_{i + 1}" if len(s["parts"]) > 1 else "")
            sql = f"SELECT {', '.join(sel)}\nFROM {part['target']}" + ("\nGROUP BY ALL" if fields else "")
            out.append({"name": f"{catalog}.{schema}.{name}", "report": s["report"], "block": s["block"],
                        "select": sql, "ddl": f"CREATE OR REPLACE VIEW {catalog}.{schema}.{name} AS\n{sql}",
                        "missing_axes": missing, "not_converted": s["not_converted"]})
    return out


def summarize_flags(concepts: dict) -> dict[str, str]:
    """Measure -> Power BI 'Summarize by' for Pattern 2 columns (non-additive must not be summed)."""
    return {m["name"]: ("None (do not summarize)" if m["kind"] in NON_ADDITIVE else "Sum")
            for m in concepts["metrics"].values()}


# ------------------------------------------------------------------------------------------------ Pattern 1
class _Untranslatable(Exception):
    pass


def _dax_of(node, table: str, measures: set[str]) -> str:
    if isinstance(node, exp.Paren):
        return f"({_dax_of(node.this, table, measures)})"
    if isinstance(node, exp.Literal):
        return node.this if not node.is_string else f'"{node.this}"'
    if isinstance(node, exp.Boolean):
        return "TRUE()" if node.this else "FALSE()"
    if isinstance(node, exp.Column):
        if node.table:
            return f"'{node.table}'[{node.name}]"
        return f"'{table}'[{node.name}]"
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        op = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*"}[type(node)]
        return f"{_dax_of(node.this, table, measures)} {op} {_dax_of(node.expression, table, measures)}"
    if isinstance(node, exp.Div):
        den = node.expression
        if isinstance(den, exp.Nullif):
            den = den.this
        return f"DIVIDE({_dax_of(node.this, table, measures)}, {_dax_of(den, table, measures)})"
    if isinstance(node, exp.Nullif):
        return _dax_of(node.this, table, measures)
    if isinstance(node, exp.Coalesce):
        return f"COALESCE({_dax_of(node.this, table, measures)}, " + ", ".join(
            _dax_of(e, table, measures) for e in node.expressions) + ")"
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        op = {exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}[type(node)]
        return f"{_dax_of(node.this, table, measures)} {op} {_dax_of(node.expression, table, measures)}"
    if isinstance(node, exp.Between):
        c = _dax_of(node.this, table, measures)
        return f"{c} >= {_dax_of(node.args['low'], table, measures)} && {c} <= {_dax_of(node.args['high'], table, measures)}"
    if isinstance(node, exp.And):
        return f"{_dax_of(node.this, table, measures)} && {_dax_of(node.expression, table, measures)}"
    if isinstance(node, exp.Anonymous) and node.name.upper() == "MEASURE":
        return f"[{node.expressions[0].sql()}]"
    if isinstance(node, exp.Filter):
        agg = _dax_of(node.this, table, measures)
        cond = node.expression.this if isinstance(node.expression, exp.Where) else node.expression
        return f"CALCULATE({agg}, FILTER('{table}', {_dax_of(cond, table, measures)}))"
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct):
        return f"DISTINCTCOUNT({_dax_of(node.this.expressions[0], table, measures)})"
    agg = {exp.Sum: "SUM", exp.Avg: "AVERAGE", exp.Min: "MIN", exp.Max: "MAX", exp.Count: "COUNT"}
    for k, v in agg.items():
        if isinstance(node, k):
            inner = node.this
            if isinstance(inner, exp.Column):
                return f"{v}({_dax_of(inner, table, measures)})"
            if isinstance(inner, exp.Star):
                return f"COUNTROWS('{table}')"
            return f"{v}X('{table}', {_dax_of(inner, table, measures)})"
    raise _Untranslatable(node.sql())


def _dax_window(name: str, base: str, window: list[dict], date_col: str) -> str:
    w0 = window[0]
    if w0.get("offset"):
        n, unit = w0["offset"].split()
        return f"CALCULATE([{base}], DATEADD({date_col}, {n}, {unit.upper().rstrip('S')}))"
    if w0["range"] == "cumulative" and len(window) > 1:
        return f"CALCULATE([{base}], DATESYTD({date_col}))"
    if w0["range"] == "cumulative":
        return f"CALCULATE([{base}], FILTER(ALL({date_col}), {date_col} <= MAX({date_col})))"
    m = re.match(r"trailing (\d+) (\w+?)s?( inclusive)?$", w0["range"])
    if m:
        n = int(m.group(1)) + (1 if m.group(3) else 0)
        return f"CALCULATE([{base}], DATESINPERIOD({date_col}, MAX({date_col}), -{n}, {m.group(2).upper()}))"
    raise _Untranslatable(str(window))


def dax_measures(mv: dict, date_table: str = "fiscal_periods", date_column: str = "period_start") -> list[dict]:
    """Pattern 1a: DAX for every metric view measure, over a star model whose tables are named like the metric
    view joins (source fact = the view's base dataset name)."""
    rows = []
    for view, v in mv["views"].items():
        table = view[:-len("_metrics")] if view.endswith("_metrics") else view
        names = {m["name"] for m in v["measures"]}
        joins = {j["name"] for j in _walk(v.get("joins") or [])}
        for m in v["measures"]:
            row = {"view": view, "measure": m["name"], "label": m.get("display_name", m["name"]),
                   "description": m.get("comment", ""), "format": _fmt(m.get("format"))}
            try:
                if m.get("window"):
                    base = next((x["name"] for x in v["measures"] if x["expr"] == m["expr"] and not x.get("window")),
                                None)
                    if not base:
                        raise _Untranslatable("window over an unnamed base")
                    row["dax"] = _dax_window(m["name"], base, m["window"], f"'{date_table}'[{date_column}]")
                    row["status"] = "Transformed (time intelligence)"
                else:
                    tree = sqlglot.parse_one(m["expr"], read="databricks")
                    # nested-join references commodities.commodity_groups.x -> 'commodity_groups'[x]
                    for c in tree.find_all(exp.Column):
                        if c.args.get("db"):
                            c.set("db", None)
                        if c.table and c.table not in joins and c.table != "source":
                            pass
                    row["dax"] = _dax_of(tree, table, names)
                    row["status"] = "Direct" if not re.search(r"CALCULATE|DIVIDE|SUMX|(?<!')\[", row["dax"]) else "Transformed"
                if "stacked" in view:
                    row["status"] += "; star model needs both facts with conformed dimensions"
            except _Untranslatable as e:
                row.update(dax=None, status="Manual", reason=f"no DAX translation for {e}")
            rows.append(row)
    return rows


def _walk(joins):
    for j in joins:
        yield j
        yield from _walk(j.get("joins") or [])


def _fmt(f: dict | None) -> str | None:
    if not f:
        return None
    places = (f.get("decimal_places") or {}).get("places", 0)
    dec = "." + "0" * places if places else ""
    if f.get("type") == "percentage":
        return f"0{dec}%"
    return ("#,##0" if f.get("thousand_separators") else "0") + dec


def tmdl_measures(rows: list[dict]) -> str:
    """Measures as TMDL text, grouped by table (paste into a PBIP project's tables/*.tmdl)."""
    out, cur = [], None
    for r in sorted(rows, key=lambda r: r["view"]):
        if not r.get("dax"):
            continue
        if r["view"] != cur:
            cur = r["view"]
            out.append(f"table {cur}\n")
        out.append(f"\tmeasure '{r['label']}' = {r['dax']}")
        if r.get("format"):
            out.append(f"\t\tformatString: {r['format']}")
        if r.get("description"):
            out.append(f"\t\tdescription: {r['description']}")
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------ Pattern 3
SAFE_FUNCS = {"DIVIDE", "SELECTEDVALUE", "FORMAT", "IF", "BLANK", "ISBLANK", "SAMEPERIODLASTYEAR", "DATEADD",
              "DATESYTD", "DATESINPERIOD", "TOTALYTD", "PARALLELPERIOD", "PREVIOUSMONTH", "PREVIOUSYEAR", "MAX",
              "MIN", "COALESCE", "AVERAGEX", "VALUES", "KEEPFILTERS"}
OVERRIDE_FUNCS = {"ALL", "ALLEXCEPT", "REMOVEFILTERS", "ALLSELECTED"}
AGG_FUNCS = {"SUM", "SUMX", "AVERAGE", "COUNT", "COUNTROWS", "DISTINCTCOUNT", "MAXX", "MINX", "COUNTX"}

PATTERN3_SAMPLES = [
    {"name": "Freight Revenue YoY %", "kind_expected": "presentation extension",
     "dax": "DIVIDE([freight_revenue] - CALCULATE([freight_revenue], SAMEPERIODLASTYEAR('fiscal_periods'[period_start])), "
            "CALCULATE([freight_revenue], SAMEPERIODLASTYEAR('fiscal_periods'[period_start])))"},
    {"name": "Freight Revenue 3M Avg", "kind_expected": "presentation extension",
     "dax": "AVERAGEX(DATESINPERIOD('fiscal_periods'[period_start], MAX('fiscal_periods'[period_start]), -3, MONTH), "
            "[freight_revenue])"},
    {"name": "Revenue per RTM", "kind_expected": "review",
     "dax": "DIVIDE([freight_revenue], [revenue_ton_miles])"},
    {"name": "Key Account Revenue", "kind_expected": "review",
     "dax": "CALCULATE([freight_revenue], 'customers'[is_key_account] = TRUE())"},
    {"name": "Freight Revenue", "kind_expected": "semantic override",
     "dax": "SUM('freight_billing'[linehaul_revenue]) + SUM('freight_billing'[accessorial_revenue])"},
    {"name": "Operating Ratio", "kind_expected": "semantic override",
     "dax": "DIVIDE([operating_expenses], CALCULATE([freight_revenue], REMOVEFILTERS('commodity_groups')))"},
]


def classify_dax(name: str, dax: str, governed: dict[str, str]) -> dict:
    """governed: governed measure name -> display label. Returns {classification, reasons}."""
    funcs = {f.upper() for f in re.findall(r"\b([A-Za-z]+)\s*\(", dax)}
    refs = set(re.findall(r"(?<!')\[([^\]]+)\]", re.sub(r"'[^']*'\[[^\]]*\]", "", dax)))
    cols = re.findall(r"'([^']+)'\[([^\]]+)\]", dax)
    labels = {v.lower(): k for k, v in governed.items()} | {k.lower(): k for k in governed}
    reasons = []
    unknown_refs = [r for r in refs if r.lower() not in labels]
    if name.lower() in labels and normalize.snake(name) in governed:
        reasons.append(f"reuses the governed name '{name}' with its own logic")
    elif name.lower() in labels:
        reasons.append(f"reuses the governed name '{name}' with its own logic")
    if funcs & AGG_FUNCS and not funcs & {"AVERAGEX"}:
        raw = [f"{t}[{c}]" for t, c in cols if "period_start" not in c]
        if raw:
            reasons.append("aggregates raw columns (" + ", ".join(raw[:3]) + ") instead of governed measures")
    if funcs & OVERRIDE_FUNCS:
        reasons.append("removes filters (" + ", ".join(sorted(funcs & OVERRIDE_FUNCS)) + "), changing totals semantics")
    if unknown_refs:
        reasons.append("references measures that aren't governed: " + ", ".join(sorted(unknown_refs)))
    if reasons:
        return {"name": name, "classification": "semantic override", "reasons": reasons}
    filters_cols = [f"{t}[{c}]" for t, c in cols if "period_start" not in c]
    if "CALCULATE" in funcs and filters_cols:
        return {"name": name, "classification": "review",
                "reasons": ["CALCULATE adds a filter on " + ", ".join(filters_cols) + ": a new business definition; "
                            "consider making it a governed measure"]}
    if refs and len(refs) >= 2 and not funcs - SAFE_FUNCS - {"CALCULATE"}:
        time = funcs & {"SAMEPERIODLASTYEAR", "DATEADD", "DATESYTD", "DATESINPERIOD", "TOTALYTD", "PARALLELPERIOD"}
        if not time:
            return {"name": name, "classification": "review",
                    "reasons": ["a new ratio of governed measures: allowed if the name isn't a governed metric; "
                                "register it if others will reuse it"]}
    other = funcs - SAFE_FUNCS - {"CALCULATE"}
    if other:
        return {"name": name, "classification": "review", "reasons": ["uses " + ", ".join(sorted(other))]}
    return {"name": name, "classification": "presentation extension",
            "reasons": ["only governed measures with presentation / time-intelligence functions"]}
