"""
SIR → normalized semantic concepts (WS2 steps 4–13). Deterministic only: anything it can't settle becomes a
finding with provenance NEEDS_HUMAN or UNSUPPORTED, never a guess.

    run(sir) -> concepts = {
        "datasets":      {name: dataset},           # fact / dimension / aggregate, UC source, fields, keys
        "relationships": [relationship],            # from = many side
        "metrics":       {name: metric},            # universe measures + translatable report variables
        "filters":       {name: filter},            # predefined filters
        "parameters":    [prompt],
        "hierarchies":   [hierarchy],
        "report_calcs":  [report calculation kept outside the canonical model],
        "variants":      [same/similar names with different definitions],
        "findings":      [...],                     # parse findings + everything found here
    }

Expressions are Databricks SQL. Field expressions use the dataset's source columns (unqualified); metric
expressions use dataset.field (Ossie name resolution); `components` keeps metric-to-metric composition, which
Ossie can't express but metric views can (MEASURE()).
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import bo_sql
from . import provenance as pv
from . import webi_formula as wf
from .parse import _finding

FACT_HINT = "fact"


def snake(s: str) -> str:
    s = re.sub(r"[%]", " pct ", s or "")
    return re.sub(r"\W+", "_", s).strip("_").lower()


def _label(name: str) -> str:
    return name.strip()


class _Ctx:
    def __init__(self, sir: dict):
        self.sir = sir
        self.dialect = sir["universe"]["dialect"]
        self.findings: list[dict] = list(sir.get("findings", []))
        self.tables = {t["name"]: t for t in sir["physical"]["tables"]}
        self.objects = {o["sir_id"]: o for o in sir["semantic"]["objects"]}
        self.datasets: dict[str, dict] = {}
        self.ds_of_table: dict[str, str] = {}
        self.col_field: dict[tuple[str, str], str] = {}     # (BO table, COLUMN) -> field name
        self.bool_cols: set[tuple[str, str]] = set()

    def find(self, *a, **k):
        self.findings.append(_finding(*a, **k))


# --------------------------------------------------------------------------------------------- datasets
def _datasets(c: _Ctx) -> None:
    for t in c.sir["physical"]["tables"]:
        base = t["alias_of"] or t["name"]
        m = c.sir["mapping"].get(t["name"]) or c.sir["mapping"].get(base) or {}
        uc = m.get("uc_table")
        kind = t["kind"]
        if t["name"].startswith("AGG_"):
            kind = "aggregate"
        source, src_state, src_rule = uc, pv.EXTRACTED, None
        if t["kind"] == "derived":
            mp = {k: v.get("uc_table") for k, v in c.sir["mapping"].items() if v.get("uc_table")}
            source, err = bo_sql.transpile_statement(t["sql"], c.dialect, mp)
            src_state, src_rule = (pv.DERIVED, "derived_table_transpile") if source else (pv.NEEDS_HUMAN, None)
            if source:
                source = " ".join(source.split())
        if not source:
            c.find("unmapped_table", "warning",
                   f"BO table {t['name']} has no Unity Catalog counterpart; objects and joins that use it are "
                   "left out until it is mapped.", [t["sir_id"]])
            continue
        name = snake(t["name"])
        uc_cols = m.get("uc_columns") or {}
        for col, typ in uc_cols.items():
            if typ == "Boolean":
                c.bool_cols.add((t["name"], col.upper()))
        c.datasets[name] = {
            "name": name, "bo_table": t["name"], "kind": kind, "alias_of": snake(t["alias_of"]) if t["alias_of"] else None,
            "source": pv.record(source, src_state, "normalizer", rule_id=src_rule, evidence=[t["sir_id"]]),
            "uc_columns": uc_cols, "primary_key": [x for x in (m.get("uc_primary_key") or [])],
            "fields": {}, "role": None, "trace": {"bo": f"bo:table/{t['bo_id']}", "sir": t["sir_id"]},
            "description": pv.record(None, pv.NEEDS_HUMAN),
        }
        c.ds_of_table[t["name"]] = name


def _add_field(c: _Ctx, ds: str, col: str, rule: str) -> str:
    """Make sure a source column is a field (join keys, measure inputs). Returns the field name."""
    t = c.datasets[ds]["bo_table"]
    key = (t, col.upper())
    if key in c.col_field:
        return c.col_field[key]
    name = col.lower()
    fields = c.datasets[ds]["fields"]
    while name in fields:
        name += "_col"
    typ = c.datasets[ds]["uc_columns"].get(col.lower())
    fields[name] = {"name": name, "label": name.replace("_", " ").title(), "expression":
                    pv.record(col.lower(), pv.DERIVED, "normalizer", rule_id=rule), "datatype": typ,
                    "role": "key" if rule == "join_key_field" else "measure_input", "is_time": typ == "Date",
                    "description": pv.record(None, pv.NEEDS_HUMAN), "bo_object": None, "scope": "universe",
                    "hidden": True}
    c.col_field[key] = name
    return name


def _resolver_for_field(c: _Ctx, ds: str):
    t = c.datasets[ds]["bo_table"]

    def r(table, col):
        if table != t:
            raise KeyError(f"{table}.{col}")
        if col.lower() not in c.datasets[ds]["uc_columns"] and c.datasets[ds]["uc_columns"]:
            raise KeyError(f"{table}.{col}")
        return None, col.lower()
    return r


def _metric_resolver(c: _Ctx, rule_list: list):
    def r(table, col):
        ds = c.ds_of_table.get(table)
        if ds is None:
            raise KeyError(f"{table}.{col}")
        if c.datasets[ds]["uc_columns"] and col.lower() not in c.datasets[ds]["uc_columns"]:
            raise KeyError(f"{table}.{col}")
        f = c.col_field.get((table, col)) or _add_field(c, ds, col, "measure_input_field")
        return ds, f
    return r


# --------------------------------------------------------------------------------------------- fields
def _fields(c: _Ctx) -> None:
    hier_time = {lv for h in c.sir["semantic"]["hierarchies"] if "fisc" in h["name"].lower() or "time" in h["name"].lower()
                 for lv in h["levels"]}
    for o in c.sir["semantic"]["objects"]:
        if o["kind"] not in ("dimension", "attribute"):
            continue
        tabs = o.get("tables") or []
        dss = [c.ds_of_table.get(t) for t in tabs]
        if not tabs or o.get("parse_errors"):
            continue
        if None in dss:
            o["status"] = pv.NEEDS_HUMAN
            c.find("unmapped_object", "warning", f"Dimension '{o['name']}' reads {', '.join(tabs)}, which isn't "
                   "mapped to Unity Catalog.", [o["sir_id"]])
            continue
        if len(set(dss)) > 1:
            c.find("cross_table_dimension", "warning", f"Dimension '{o['name']}' reads several tables "
                   f"({', '.join(tabs)}); a field belongs to one dataset.", [o["sir_id"]])
            continue
        ds = dss[0]
        sql, rules, err = bo_sql.rewrite(o["select_resolved"], c.dialect, _resolver_for_field(c, ds), c.bool_cols)
        if err:
            c.find("unresolved_column", "error", f"Dimension '{o['name']}': {err}.", [o["sir_id"]])
            continue
        tree, _ = bo_sql.parse(o["select_resolved"], c.dialect)
        bare = bo_sql.is_bare_column(tree)
        name = snake(o["name"])
        fields = c.datasets[ds]["fields"]
        if name in fields:
            name = f"{name}_{snake(o['folder'])}"
        col = tree.name.lower() if bare else None
        typ = c.datasets[ds]["uc_columns"].get(col) if bare else o["datatype"]
        is_time = (typ in ("Date", "DateTime") or o["folder"].lower() in ("time", "calendar")
                   or o["sir_id"] in hier_time)
        fields[name] = {
            "name": name, "label": _label(o["name"]),
            "expression": pv.record(sql, pv.EXTRACTED if bare and not rules[1:] else pv.DERIVED, "normalizer",
                                    rule_id=None if bare else ",".join(rules) or "expression",
                                    evidence=[o["sir_id"]], original=o["select"]),
            "datatype": typ, "role": "dimension", "is_time": is_time, "description": o["description"],
            "bo_object": o["sir_id"], "folder": o["folder"], "format": o.get("format"), "scope": "universe",
            "hidden": o.get("hidden", False), "parent": o.get("parent"),
            "trace": {"bo": f"bo:{c.sir['universe']['cuid']}/{o['bo_id']}", "sir": o["sir_id"]},
        }
        if bare:
            c.col_field[(c.datasets[ds]["bo_table"], col.upper())] = name
        o["field"] = f"{ds}.{name}"


# --------------------------------------------------------------------------------------------- relationships
def _relationships(c: _Ctx) -> list[dict]:
    rels = []
    for j in c.sir["physical"]["joins"]:
        if j["extra_predicates"] or not j["pairs"]:
            c.find("theta_join", "warning", f"Join {j['bo_id']} ({j['expression']}) isn't an equi-join; Ossie "
                   "relationships and metric view joins need key equality. Re-express it (e.g. a derived "
                   "dimension) by hand.", [j["sir_id"]], klass="Unsupported/unresolved")
            continue
        (lt, _), (rt, _) = j["pairs"][0]
        lds, rds = c.ds_of_table.get(lt), c.ds_of_table.get(rt)
        if not lds or not rds:
            continue
        card_rec = dict(j["cardinality"])
        card = card_rec.get("value")
        if card is None:
            rpk = set(c.datasets[rds]["primary_key"])
            lpk = set(c.datasets[lds]["primary_key"])
            rcols = {p[1][1].lower() for p in j["pairs"]}
            lcols = {p[0][1].lower() for p in j["pairs"]}
            if rpk and rcols == rpk:
                card = "N-1"
            elif lpk and lcols == lpk:
                card = "1-N"
            card_rec = pv.record(card, pv.DERIVED if card else pv.NEEDS_HUMAN, "normalizer",
                                 rule_id="cardinality_from_uc_primary_key" if card else None,
                                 evidence=[j["sir_id"]])
            if card:
                c.find("derived_cardinality", "info", f"Join {j['bo_id']} has no cardinality in the Universe; "
                       f"derived {card} from the Unity Catalog primary key.", [j["sir_id"]],
                       klass="Derived deterministically")
            else:
                c.find("unknown_cardinality", "warning", f"Join {j['bo_id']}: cardinality unknown.", [j["sir_id"]])
                continue
        if card == "N-N":
            c.find("many_to_many", "warning", f"Join {j['bo_id']} is many-to-many.", [j["sir_id"]])
            continue
        frm, to, pairs = (lds, rds, j["pairs"]) if card in ("N-1", "1-1") else (rds, lds,
                                                                               [[b, a] for a, b in j["pairs"]])
        fcols = [_add_field(c, frm, p[0][1], "join_key_field") for p in pairs]
        tcols = [_add_field(c, to, p[1][1], "join_key_field") for p in pairs]
        rels.append({"name": f"{frm}_to_{to}" + ("" if not any(r["name"] == f"{frm}_to_{to}" for r in rels)
                                                  else f"_{j['bo_id'].lower()}"),
                     "from": frm, "to": to, "from_columns": fcols, "to_columns": tcols,
                     "cardinality": card_rec | {"value": "many_to_one" if card != "1-1" else "one_to_one"},
                     "contexts": j["contexts"], "bo_join": j["sir_id"],
                     "trace": {"bo": f"bo:join/{j['bo_id']}", "sir": j["sir_id"]}})
    return rels


def _roles(c: _Ctx, rels: list[dict], metrics: dict) -> None:
    many = defaultdict(int)
    one = defaultdict(int)
    for r in rels:
        many[r["from"]] += 1
        one[r["to"]] += 1
    measured = defaultdict(int)
    for m in metrics.values():
        if m["scope"] == "universe":
            for d in m["datasets"]:
                measured[d] += 1
    for name, d in c.datasets.items():
        if d["kind"] == "aggregate":
            role, rule = "aggregate", "aggregate_table_name"
        elif many[name] and not one[name] and measured[name]:
            role, rule = "fact", "many_side_with_measures"
        elif many[name] and one[name] and not measured[name]:
            role, rule = "dimension", "snowflake_dimension"
        elif one[name]:
            role, rule = "dimension", "one_side"
        elif measured[name]:
            role, rule = "fact", "measures_only"
        else:
            role, rule = "dimension", "default"
        d["role"] = pv.record(role, pv.DERIVED, "normalizer", rule_id=rule)


# --------------------------------------------------------------------------------------------- metrics
def _kind(aggs: list[str], datasets: list[str], filtered: bool) -> str:
    if len(datasets) > 1:
        return "multi_fact"
    if len(aggs) > 1:
        return "ratio"
    a = aggs[0] if aggs else ""
    if a == "COUNT_DISTINCT":
        return "distinct_count"
    if a == "AVG":
        return "average"
    if filtered:
        return "filtered"
    return "base"


def _universe_metrics(c: _Ctx) -> dict:
    metrics: dict[str, dict] = {}
    measures = [o for o in c.sir["semantic"]["objects"] if o["kind"] == "measure"]
    name_count = defaultdict(int)
    for o in measures:
        name_count[snake(o["name"])] += 1
    for o in measures:
        if o.get("parse_errors"):
            continue
        tabs = o.get("tables") or []
        if any(t not in c.ds_of_table for t in tabs):
            c.find("unmapped_object", "warning", f"Measure '{o['name']}' reads unmapped tables "
                   f"({', '.join(t for t in tabs if t not in c.ds_of_table)}).", [o["sir_id"]])
            continue
        rules: list = []
        sql, r1, err = bo_sql.rewrite(o["select_resolved"], c.dialect, _metric_resolver(c, rules), c.bool_cols)
        if err:
            c.find("unresolved_column", "error", f"Measure '{o['name']}': {err}.", [o["sir_id"]])
            continue
        rules += r1
        datasets = list(dict.fromkeys(c.ds_of_table[t] for t in tabs))
        filt = None
        dialects = {}
        if o.get("where_resolved"):
            cond, r2, err = bo_sql.rewrite(o["where_resolved"], c.dialect, _metric_resolver(c, rules), c.bool_cols)
            if err:
                c.find("unresolved_column", "error", f"Measure '{o['name']}' WHERE: {err}.", [o["sir_id"]])
                continue
            filt = cond
            dbx, ansi = bo_sql.filter_form(sql, cond)
            dialects = {"DATABRICKS": dbx}
            sql, rules = ansi, rules + r2 + ["measure_where_to_filter"]
            for t in bo_sql.tables(bo_sql.parse(o["where_resolved"], c.dialect)[0]):
                if t in c.ds_of_table:
                    datasets = list(dict.fromkeys(datasets + [c.ds_of_table[t]]))
        aa = [n for n in o.get("bo_functions", []) if n["function"] == "@Aggregate_Aware"]
        if aa:
            rules.append("aa_most_detailed")
            c.find("aggregate_awareness", "info",
                   f"Measure '{o['name']}' is aggregate-aware over {len(aa[0]['detail']['candidates'])} tables; the "
                   "canonical definition uses the most detailed one. Metric view materialization is the Databricks "
                   "counterpart of the aggregate tables.", [o["sir_id"]], klass="Derived deterministically",
                   status="Partial")
        aggs = o.get("aggregations") or []
        kind = _kind(aggs, datasets, bool(filt))
        proj = (o.get("projection") or "").lower()
        if proj == "sum" and kind in ("average", "ratio", "distinct_count"):
            c.find("projection_conflict", "warning",
                   f"Measure '{o['name']}' computes {', '.join(aggs)} in SQL but its projection is Sum, so Webi "
                   "would add up the partial results in totals. The canonical metric re-aggregates in the database "
                   "(the database-delegated behaviour); confirm totals with Finance.", [o["sir_id"]])
        name = snake(o["name"])
        if name_count[name] > 1:
            name = f"{name}_{datasets[0]}"
        metrics[name] = {
            "name": name, "label": o["name"], "scope": "universe", "kind": kind,
            "expression": pv.record(sql, pv.EXTRACTED if not rules or rules == [f"transpile_{c.dialect}"]
                                    else pv.DERIVED, "normalizer", rule_id=",".join(dict.fromkeys(rules)) or None,
                                    evidence=[o["sir_id"]], original=o["select"]),
            "dialects": dialects, "aggregations": aggs, "datasets": datasets, "filter": filt, "components": [],
            "window": None, "format": o.get("format"), "projection": o.get("projection"),
            "description": o["description"], "bo_object": o["sir_id"], "folder": o["folder"],
            "datatype": "Decimal" if kind in ("ratio", "average", "multi_fact") or aggs[:1] == ["SUM"] else "Integer",
            "trace": {"bo": f"bo:{c.sir['universe']['cuid']}/{o['bo_id']}", "sir": o["sir_id"]},
            "reports": [],
        }
        o["metric"] = name
    return metrics


def _report_names(c: _Ctx, rpt: dict, metrics: dict) -> dict[str, list[str]]:
    """Name -> candidate metric names (or 'var:<name>'), in data-provider order."""
    out: dict[str, list[str]] = defaultdict(list)
    for dp in rpt["data_providers"]:
        for oid in dp["objects"]:
            o = c.objects.get(oid)
            if not o:
                continue
            if o.get("metric"):
                if o["metric"] not in out[o["name"]]:
                    out[o["name"]].append(o["metric"])
                metrics[o["metric"]]["reports"] = sorted(set(metrics[o["metric"]]["reports"]) | {rpt["bo_id"]})
            elif o.get("field"):
                if ("field:" + o["field"]) not in out[o["name"]]:
                    out[o["name"]].append("field:" + o["field"])
    return out


def _report_metrics(c: _Ctx, metrics: dict, datasets: dict) -> list[dict]:
    calcs = []
    by_var_name = defaultdict(list)
    for rpt in c.sir["reports"]:
        names = _report_names(c, rpt, metrics)
        for name, cands in names.items():
            ms = [x for x in cands if not x.startswith("field:")]
            if len(ms) > 1:
                c.find("ambiguous_name", "warning",
                       f"{rpt['bo_id']}: '{name}' is returned by {len(ms)} different objects "
                       f"({', '.join(ms)}); report calculations that say [{name}] use the first.",
                       [rpt["sir_id"]], klass="Requires human validation")
        local: dict[str, str] = {}
        for v in rpt["variables"]:
            cl = v["classification"]
            by_var_name[v["name"]].append((rpt["bo_id"], v["formula"]))
            mname = snake(v["name"])
            if mname in metrics:
                mname = f"{mname}_{rpt['bo_id'].lower()}"

            def resolve_ref(ref: str):
                if ref in local:
                    return ("metric", local[ref])
                cands = names.get(ref, [])
                if not cands:
                    return None
                x = cands[0]
                return ("field", x[6:]) if x.startswith("field:") else ("metric", x)
            refs = {r: resolve_ref(r) for r in cl["references"]}
            missing = [r for r, x in refs.items() if x is None]
            calc = {"name": mname, "label": v["name"], "report": rpt["bo_id"], "formula": v["formula"],
                    "kind": cl["kind"], "status": cl["status"], "reason": cl["reason"], "sir": v["sir_id"]}
            if missing:
                calc.update(status="Manual", reason=f"References not found in the report: {', '.join(missing)}.")
                calcs.append(calc)
                c.find("unresolved_reference", "warning", f"{rpt['bo_id']} variable '{v['name']}': "
                       f"{calc['reason']}", [v["sir_id"]])
                continue
            if cl["kind"] == "arithmetic" and all(x[0] == "metric" for x in refs.values()):
                comps = [refs[r][1] for r in cl["references"]]
                expanded = wf.placeholders_to(cl["translation"]["sql"],
                                              lambda r: f"({metrics[refs[r][1]]['expression']['value']})")
                mv_expr = wf.placeholders_to(cl["translation"]["sql"], lambda r: f"MEASURE({refs[r][1]})")
                dsets = list(dict.fromkeys(d for cc in comps for d in metrics[cc]["datasets"]))
                over_window = [cc for cc in comps if metrics[cc]["window"] or metrics[cc].get("over_window")]
                metrics[mname] = {
                    "name": mname, "label": v["name"], "scope": f"report:{rpt['bo_id']}",
                    "kind": "multi_fact" if len(dsets) > 1 else "derived",
                    "expression": pv.record(expanded, pv.DERIVED, "normalizer", rule_id="webi_arithmetic_inline",
                                            evidence=[v["sir_id"]], original=v["formula"]),
                    "mv_expression": mv_expr, "dialects": {}, "aggregations": [], "datasets": dsets,
                    "filter": None, "components": comps, "window": None, "format": None, "projection": None,
                    "description": pv.record(None, pv.NEEDS_HUMAN), "bo_object": v["sir_id"], "folder": "",
                    "datatype": "Decimal", "reports": [rpt["bo_id"]],
                    "trace": {"bo": f"bo:{rpt['cuid']}/{v['bo_id']}", "sir": v["sir_id"]},
                }
                if over_window:
                    # a window can't be inlined into one aggregate expression: Ossie can only carry the
                    # metric-view form; the inlined text would be wrong (it would drop the window)
                    metrics[mname]["over_window"] = over_window
                    metrics[mname]["expression"] = pv.record(mv_expr, pv.DERIVED, "normalizer",
                                                             rule_id="composed_over_window", evidence=[v["sir_id"]],
                                                             original=v["formula"])
                local[v["name"]] = mname
                calc["metric"] = mname
            elif cl["kind"] == "time" and cl["translation"]:
                t = cl["translation"]
                base_ref = refs.get(t["measure"])
                if not base_ref or base_ref[0] != "metric":
                    calc.update(status="Manual", reason="Time calculation over something that isn't a measure.")
                    calcs.append(calc)
                    continue
                base = metrics[base_ref[1]]
                win = dict(t)
                win["measure"] = base_ref[1]
                for k in ("along", "reset_by"):
                    if t.get(k):
                        ref = refs.get(t[k]) or resolve_ref(t[k])
                        win[k] = ref[1] if ref else t[k]
                metrics[mname] = {
                    "name": mname, "label": v["name"], "scope": f"report:{rpt['bo_id']}", "kind": "time",
                    "expression": pv.record(base["expression"]["value"], pv.DERIVED, "normalizer",
                                            rule_id=f"webi_time_{t['window']}", evidence=[v["sir_id"]],
                                            original=v["formula"]),
                    "dialects": dict(base.get("dialects") or {}), "aggregations": base["aggregations"],
                    "datasets": base["datasets"], "filter": base["filter"], "components": [base_ref[1]],
                    "window": win, "format": base["format"], "projection": None,
                    "description": pv.record(None, pv.NEEDS_HUMAN), "bo_object": v["sir_id"], "folder": "",
                    "datatype": base["datatype"], "reports": [rpt["bo_id"]],
                    "trace": {"bo": f"bo:{rpt['cuid']}/{v['bo_id']}", "sir": v["sir_id"]},
                }
                local[v["name"]] = mname
                calc["metric"] = mname
            elif cl["kind"] == "conditional_dimension" and cl["translation"]:
                fs = [refs[r][1] for r in cl["references"]]
                dss = {f.split(".")[0] for f in fs}
                if len(dss) != 1 or any(refs[r][0] != "field" for r in cl["references"]):
                    calc.update(status="Manual", reason="Condition spans datasets or uses measures.")
                else:
                    ds = dss.pop()
                    expr = wf.placeholders_to(cl["translation"]["sql"], lambda r: "(" + datasets[ds]["fields"][
                        refs[r][1].split(".")[1]]["expression"]["value"] + ")")
                    fname = snake(v["name"])
                    datasets[ds]["fields"][fname] = {
                        "name": fname, "label": v["name"], "expression": pv.record(
                            expr, pv.DERIVED, "normalizer", rule_id="webi_if_to_case", evidence=[v["sir_id"]],
                            original=v["formula"]),
                        "datatype": "String", "role": "dimension", "is_time": False,
                        "description": pv.record(None, pv.NEEDS_HUMAN), "bo_object": v["sir_id"],
                        "scope": f"report:{rpt['bo_id']}", "hidden": False, "folder": "",
                        "trace": {"bo": f"bo:{rpt['cuid']}/{v['bo_id']}", "sir": v["sir_id"]}}
                    calc["field"] = f"{ds}.{fname}"
            calcs.append(calc)
    # same variable name, different formula across reports (C6)
    c._var_names = by_var_name
    return calcs


def _variants(c: _Ctx, metrics: dict) -> list[dict]:
    out = []
    by_label = defaultdict(list)
    for m in metrics.values():
        by_label[m["label"].strip().lower()].append(m["name"])
    for label, names in by_label.items():
        exprs = {n: metrics[n]["expression"]["value"] for n in names}
        if len(names) > 1 and len({bo_sql.normalize_hash(e) for e in exprs.values()}) > 1:
            out.append({"label": metrics[names[0]]["label"], "metrics": names, "expressions": exprs,
                        "reason": "same business name, different calculation"})
            c.find("same_name_different_calc", "warning",
                   f"'{metrics[names[0]]['label']}' has {len(names)} different definitions: "
                   + "; ".join(f"{n} = {e}" for n, e in exprs.items()), [metrics[n]["bo_object"] for n in names])
    by_hash = defaultdict(list)
    for m in metrics.values():
        by_hash[bo_sql.normalize_hash(m["expression"]["value"])].append(m["name"])
    for h, names in by_hash.items():
        labels = {metrics[n]["label"].lower() for n in names}
        if len(names) > 1 and len(labels) > 1 and not all(metrics[n]["window"] for n in names):
            if any(metrics[n]["window"] for n in names):
                continue
            out.append({"label": " / ".join(metrics[n]["label"] for n in names), "metrics": names,
                        "expressions": {n: metrics[n]["expression"]["value"] for n in names},
                        "reason": "different names, same calculation"})
            c.find("same_calc_different_name", "info",
                   f"{', '.join(repr(metrics[n]['label']) for n in names)} compute the same thing.",
                   [metrics[n]["bo_object"] for n in names])
    return out


# --------------------------------------------------------------------------------------------- conflicts
def _conflicts(c: _Ctx) -> None:
    """Report SQL vs Universe: joins and predicates the Universe doesn't define (custom SQL, overrides)."""
    known_joins = {bo_sql.normalize_hash(j["expression"], c.dialect) for j in c.sir["physical"]["joins"]}
    known_preds = set()
    for o in c.sir["semantic"]["objects"]:
        for k in ("where", "where_resolved"):
            if o.get(k):
                known_preds.add(bo_sql.normalize_hash(re.sub(r":\w+", "X", o[k]), c.dialect))
    for rpt in c.sir["reports"]:
        for dp in rpt["data_providers"]:
            cond_cols = {c.objects[f"obj:{x['object']}"]["select"] for x in dp["conditions"]
                         if f"obj:{x['object']}" in c.objects}
            for st in dp["statements"]:
                tree, err = bo_sql.parse(st["sql"], c.dialect)
                if tree is None:
                    continue
                where = tree.args.get("where")
                if not where:
                    continue
                conj = list(where.this.flatten()) if isinstance(where.this, bo_sql.exp.And) else [where.this]
                for p in conj:
                    p = p.unnest() if hasattr(p, "unnest") else p
                    text = p.sql(dialect=c.dialect)
                    h = bo_sql.normalize_hash(text, c.dialect)
                    if h in known_joins:
                        continue
                    if h in known_preds or any(col and col in text for col in cond_cols):
                        continue
                    c.find("sql_conflict", "warning",
                           f"{rpt['bo_id']}/{dp['id']} SQL has a predicate the Universe doesn't define: {text}"
                           + (" (custom SQL in the data provider)" if dp["custom_sql"] else ""),
                           [st["sir_id"]], klass="Requires human validation")


def run(sir: dict) -> dict:
    c = _Ctx(sir)
    _datasets(c)
    _fields(c)
    rels = _relationships(c)
    metrics = _universe_metrics(c)
    _roles(c, rels, metrics)
    calcs = _report_metrics(c, metrics, c.datasets)
    variants = _variants(c, metrics)
    _conflicts(c)
    filters = {}
    for o in sir["semantic"]["objects"]:
        if o["kind"] != "filter":
            continue
        cond, rules, err = bo_sql.rewrite(re.sub(r":(\w+)", r"__p_\1", o.get("where_resolved") or ""), c.dialect,
                                          _metric_resolver(c, []), c.bool_cols)
        prm = re.findall(r":(\w+)", o.get("where_resolved") or "")
        filters[snake(o["name"])] = {
            "name": snake(o["name"]), "label": o["name"],
            "expression": pv.record(cond.replace("__p_", ":") if cond else None,
                                    pv.DERIVED if cond else pv.NEEDS_HUMAN, "normalizer",
                                    rule_id=",".join(rules) or None, evidence=[o["sir_id"]], original=o["where"]),
            "parameters": prm, "description": o["description"], "bo_object": o["sir_id"]}
    params = []
    for p in sir["semantic"]["prompts"]:
        bound = c.objects.get(p["bound_object"] or "", {}).get("field")
        params.append({"name": snake(p["name"]), "label": p["name"], "datatype": p["datatype"], "multi": p["multi"],
                       "default": p["default"], "field": bound, "bo_prompt": p["sir_id"]})
    hierarchies = [{"name": snake(h["name"]), "label": h["name"],
                    "levels": [c.objects[x].get("field") for x in h["levels"] if x in c.objects], "sir": h["sir_id"]}
                   for h in sir["semantic"]["hierarchies"]]
    for s in sir["semantic"]["security"]:
        c.find("security", "info", f"Security '{s['name']}' ({s['kind']} on {s['target']}: {s['expression']}) is "
               "inventoried only; metric view sources can't carry row filters, so it stays a Unity Catalog policy "
               "decision.", [s["sir_id"]], klass="Unsupported/unresolved")
    return {"datasets": c.datasets, "relationships": rels, "metrics": metrics, "filters": filters,
            "parameters": params, "hierarchies": hierarchies, "report_calcs": calcs, "variants": variants,
            "findings": c.findings, "universe": sir["universe"]}
