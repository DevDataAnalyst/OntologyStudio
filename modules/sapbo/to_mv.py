"""
Apache Ossie → Databricks metric views (WS5). Reads an Ossie document (any Ossie model works; the SAP_BO
extensions add windows, composition and scope when present) and writes metric view YAML (version 1.1), the
CREATE VIEW … WITH METRICS DDL and a conversion report.

Design (roadmap §9):
  * one metric view per fact dataset: source = the fact, joins = its many-to-one relationships (nested for
    snowflakes, depth ≤ 3), fields = dimension fields of the fact and of every joined dataset
  * a measure lives in the metric view of the dataset it aggregates; a measure that aggregates a dimension
    table (e.g. headcount on cost centers) gets a metric view sourced on that table, so it isn't fanned out
    by fact rows
  * measures over several facts (budget vs actual, fuel recovery) get a *stacked* metric view: the facts are
    UNION ALL-ed on their shared (conformed) dimension keys, each fact's inputs in its own column, and the
    shared dimensions joined once. Additive measures then combine correctly at any grain of the shared
    dimensions
  * composed measures use MEASURE(); windows use `window` (offset, cumulative + reset, trailing)
  * statuses: Direct / Transformed / Partial / Unsupported / Manual
"""
from __future__ import annotations

import re
from collections import defaultdict

import sqlglot
from sqlglot import exp

from modules import metric_views as mvlib

from .to_ossie import MV_DIALECT, VENDOR

MAX_DEPTH = 3
SQL_TYPES = {"Integer": "BIGINT", "Decimal": "DECIMAL(38,6)", "Float": "DOUBLE", "String": "STRING",
             "Boolean": "BOOLEAN", "Date": "DATE", "DateTime": "TIMESTAMP"}
TRAILING_NOTE = ("Webi Previous() sums are a trailing window over the period date. It matches BO when the "
                 "period date is on the axis; grouped by fiscal year and an opaque period number the window "
                 "collapses to the month, so such a block needs the date on its axis or a human rewrite.")


def _x(el: dict) -> dict:
    for e in el.get("nonportable_extensions") or []:
        if e.get("vendor_name") == VENDOR:
            return e.get("data") or {}
    for e in el.get("nonportable_extensions") or []:
        if e.get("vendor_name") == "ONTOLOGY_STUDIO":
            return e.get("data") or {}
    return {}


def _expr_of(el: dict, prefer=("DATABRICKS",)) -> str | None:
    e = el.get("expression") or {}
    for d in e.get("dialects") or []:
        if d.get("dialect") in prefer:
            return d["expression"]
    return e.get("sql")


def _snake(s):
    return re.sub(r"\W+", "_", s or "").strip("_").lower()


class Model:
    def __init__(self, doc: dict):
        self.doc = doc
        self.ds = {d["name"]: d for d in doc.get("datasets", [])}
        self.fields = {(d["name"], f["name"]): f for d in doc.get("datasets", []) for f in d.get("fields") or []}
        self.rels = doc.get("relationships", [])
        self.metrics = {m["name"]: m for m in doc.get("metrics", [])}
        self.out_rels = defaultdict(list)
        for r in self.rels:
            if _x(r).get("cardinality", "many_to_one") in ("many_to_one", "one_to_one"):
                self.out_rels[r["from"]].append(r)

    def role(self, ds: str) -> str:
        r = _x(self.ds[ds]).get("role")
        if r:
            return r
        return "fact" if self.out_rels.get(ds) and not any(x["to"] == ds for x in self.rels) else "dimension"

    def field_expr(self, ds: str, field: str) -> str:
        return _expr_of(self.fields[(ds, field)]) or field


def _qualify(expr: str, path: str) -> str:
    """Prefix every unqualified column with the join path (e.g. commodities.commodity_groups)."""
    tree = sqlglot.parse_one(expr, read="databricks")
    parts = path.split(".")

    def q(node):
        if isinstance(node, exp.Column) and not node.table and not isinstance(node.this, exp.Star):
            ids = [exp.to_identifier(p) for p in parts]
            kw = {"table": ids[-1]}
            if len(ids) >= 2:
                kw["db"] = ids[-2]
            if len(ids) >= 3:
                kw["catalog"] = ids[-3]
            return exp.Column(this=node.this.copy(), **kw)
        return node
    return tree.transform(q).sql(dialect="databricks")


def _rewrite_metric(expr: str, resolve) -> tuple[str | None, str | None]:
    """dataset.field references -> metric-view expressions via resolve(ds, field) (returns SQL or None)."""
    try:
        tree = sqlglot.parse_one(expr, read="databricks")
    except Exception as e:  # noqa: BLE001
        return None, str(e).splitlines()[0]
    bad = []

    def ren(node):
        if isinstance(node, exp.Column) and node.table:
            sql = resolve(node.table, node.name)
            if sql is None:
                bad.append(f"{node.table}.{node.name}")
                return node
            return sqlglot.parse_one(sql, read="databricks")
        return node
    out = tree.transform(ren)
    if bad:
        return None, "not reachable in this view: " + ", ".join(dict.fromkeys(bad))
    return out.sql(dialect="databricks"), None


def _window(win: dict, order_field: str | None, year_field: str | None,
            field_names: dict[str, str] | None = None) -> tuple[list | None, str, str]:
    """A Webi time function as a metric view window. Returns (window, status, note).

    The window is ordered on the dimension the Webi formula names, which is what BO does: RelativeValue(...;
    [Fiscal Year];-1) moves along Fiscal Year, RunningSum(...;[Fiscal Year]) restarts per Fiscal Year and runs
    along the block's period. Ordering on a date instead only matches BO when the date itself is on the axis
    (verified live 2026-11-27: date-ordered PY/YTD were NULL or wrong when grouped by fiscal year and period).
    """
    fn = field_names or {}
    k = win.get("window")
    if k == "offset":
        along = fn.get(win.get("along") or "")
        if along:
            return [{"order": along, "range": "current", "offset": int(win["offset"])}], "Transformed", ""
        if not order_field:
            return None, "Manual", "no field to order the window by"
        months = 12 if "year" in (win.get("along") or "") else 1
        return ([{"order": order_field, "range": "current", "offset": f"{win['offset'] * months} month"}],
                "Partial", "ordered on the date: matches BO only when the date is on the axis")
    if k == "cumulative":
        reset = fn.get(win.get("reset_by") or "") or year_field
        period = next((v for key, v in fn.items() if key.endswith(".fiscal_period")), None) or order_field
        if not period:
            return None, "Manual", "no field to order the window by"
        w = [{"order": period, "range": "cumulative"}]
        if win.get("reset_by") and reset:
            w.append({"order": reset, "range": "current"})
        return w, "Transformed", ""
    if k == "trailing":
        if not order_field:
            return None, "Manual", "no date field to order the window by"
        n = int(win.get("periods", 1))
        return ([{"order": order_field, "range": f"trailing {n} month inclusive"}], "Partial",
                TRAILING_NOTE)
    return None, "Manual", f"window kind {k} not supported"


class _View:
    def __init__(self, m: Model, name: str, source: str, base_ds: list[str], comment: str):
        self.m, self.name = m, name
        self.view = {"version": "1.1", "comment": comment, "source": source, "joins": [], "fields": [],
                     "measures": []}
        self.base_ds = base_ds          # datasets whose fields are in the source itself
        self.paths: dict[str, str] = {}  # dataset -> join path
        self.colmap: dict[tuple[str, str], str] = {}   # (ds, field) -> source column (stacked views)
        self.names: set[str] = set()
        self.order_field = None
        self.year_field = None
        self.dim_members: list[str] = []
        self.field_names: dict[str, str] = {}    # "ds.field" -> field name in this view
        self.force: set[tuple[str, str]] = set(getattr(m, "filter_inputs", set()))

    def add_joins(self, start: str, rels=None):
        def walk(ds, prefix, depth, into):
            for r in rels if (rels is not None and ds == start) else self.m.out_rels.get(ds, []):
                to = r["to"]
                if to in self.paths or to in self.base_ds or depth > MAX_DEPTH:
                    continue
                if _x(self.m.ds[to]).get("kind") == "aggregate" or self.m.role(to) == "fact":
                    continue
                path = f"{prefix}.{to}" if prefix else to
                self.paths[to] = path
                left = prefix if prefix else "source"
                on = " AND ".join(
                    f"{left}.{self._key(ds, fc)} = {to}.{self.m.field_expr(to, tc)}"
                    for fc, tc in zip(r["from_columns"], r["to_columns"]))
                j = {"name": to, "source": self.m.ds[to]["source"], "on": on}
                into.append(j)
                sub = []
                walk(to, path, depth + 1, sub)
                if sub:
                    j["joins"] = sub
        walk(start, "", 1, self.view["joins"])
        if not self.view["joins"]:
            self.view.pop("joins")

    def _key(self, ds, field):
        if (ds, field) in self.colmap:
            return self.colmap[(ds, field)]
        return self.m.field_expr(ds, field)

    def resolve(self, ds, field):
        if (ds, field) not in self.m.fields:
            return None
        if (ds, field) in self.colmap:
            return self.colmap[(ds, field)]
        if ds in self.base_ds:
            return f"({self.m.field_expr(ds, field)})" if not re.fullmatch(r"\w+", self.m.field_expr(ds, field)) \
                else self.m.field_expr(ds, field)
        if ds in self.paths:
            e = _qualify(self.m.field_expr(ds, field), self.paths[ds])
            return e if re.fullmatch(r"[\w.]+", e) else f"({e})"
        return None

    def add_fields(self):
        for ds in self.base_ds + list(self.paths):
            for f in self.m.ds[ds].get("fields") or []:
                x = _x(f)
                if (x.get("role") in ("key", "measure_input") or x.get("hidden")) and (ds, f["name"]) not in self.force:
                    continue
                if (ds, f["name"]) in self.colmap:
                    expr = self.colmap[(ds, f["name"])]
                elif ds in self.base_ds:
                    expr = self.m.field_expr(ds, f["name"])
                elif ds in self.paths:
                    expr = _qualify(self.m.field_expr(ds, f["name"]), self.paths[ds])
                else:
                    continue
                if (ds, f["name"]) in self.colmap and ds not in self.paths:
                    continue
                name = f["name"] if f["name"].lower() not in self.names else f"{ds}_{f['name']}"
                self.names.add(name.lower())
                self.field_names[f"{ds}.{f['name']}"] = name
                fd = {"name": name, "expr": expr}
                if f.get("label"):
                    fd["display_name"] = f["label"][:255]
                if f.get("description"):
                    fd["comment"] = f["description"]
                syn = (f.get("ai_context") or {}).get("synonyms") if isinstance(f.get("ai_context"), dict) else None
                if syn:
                    fd["synonyms"] = syn[:10]
                self.view["fields"].append(fd)
                if (f.get("dimension") or {}).get("is_time") and f.get("datatype") == "Date" and not self.order_field:
                    self.order_field = name
                if f["name"] == "fiscal_year" and not self.year_field:
                    self.year_field = name

    def add_measure(self, metric: dict, rows: list[dict], report_metrics: bool) -> bool:
        x = _x(metric)
        name = metric["name"]
        scope = x.get("scope", "universe")
        row = {"element": "metric", "name": name, "scope": scope, "kind": x.get("kind", "base"), "view": self.name}
        if scope != "universe" and not report_metrics:
            return False
        comps = x.get("components") or []
        defined = {mm["name"] for mm in self.view["measures"]}
        win = x.get("window")
        if win:
            base = win.get("measure")
            if base not in defined:
                row.update(status="Manual", reason=f"its base measure {base} isn't in this view")
                rows.append(row)
                return False
            spec, status, note = _window(win, self.order_field, self.year_field, self.field_names)
            if not spec:
                row.update(status=status, reason=note)
                rows.append(row)
                return False
            base_expr = next(mm["expr"] for mm in self.view["measures"] if mm["name"] == base)
            md = {"name": name, "expr": base_expr, "window": spec}
            row.update(status=status, reason=f"Webi {win['window']} → window measure. {note}".strip())
        elif comps and (x.get("mv_expression") or _expr_of(metric, (MV_DIALECT,))):
            missing = [cc for cc in comps if cc not in defined]
            if missing:
                row.update(status="Manual", reason=f"components not in this view: {', '.join(missing)}")
                rows.append(row)
                return False
            md = {"name": name, "expr": x.get("mv_expression") or _expr_of(metric, (MV_DIALECT,))}
            row.update(status="Transformed", reason="composed with MEASURE() (Ossie can only inline it)")
        else:
            src = _expr_of(metric)
            expr, err = _rewrite_metric(src, self.resolve)
            if err:
                row.update(status="Manual", reason=err)
                rows.append(row)
                return False
            if not mvlib.is_aggregate(expr):
                row.update(status="Unsupported", reason="the expression doesn't aggregate")
                rows.append(row)
                return False
            md = {"name": name, "expr": expr}
            kind = x.get("kind", "base")
            if "FILTER (WHERE" in expr.upper():
                row.update(status="Transformed", reason="measure WHERE → FILTER (WHERE …)")
            elif self.name.endswith("_stacked_metrics"):
                row.update(status="Transformed", reason="multi-fact measure on a stacked (UNION ALL) source")
            elif kind in ("ratio", "distinct_count", "average"):
                row.update(status="Direct", reason="non-additive; re-aggregated by the metric view at query time")
            else:
                row.update(status="Direct", reason="")
        if self.name.endswith("_stacked_metrics") and self.dim_members:
            row.update(status="Partial", reason=(row.get("reason", "") + "; " if row.get("reason") else "")
                       + f"only the shared keys can filter it: {', '.join(self.dim_members)} rows have no "
                         "other dimensions (e.g. a fiscal year filter can't reach them)")
        if name.lower() in self.names:
            md["name"] = name = f"{name}_measure"
        self.names.add(name.lower())
        if x.get("label"):
            md["display_name"] = x["label"][:255]
        comment = metric.get("description") or ""
        tag = f"[bo:{(x.get('trace') or {}).get('bo', '').split('/')[-1]}]" if x.get("trace") else ""
        if scope != "universe":
            tag += f" [{scope}]"
        md["comment"] = (comment + " " + tag).strip() or None
        if not md["comment"]:
            md.pop("comment")
        syn = (metric.get("ai_context") or {}).get("synonyms") if isinstance(metric.get("ai_context"), dict) else None
        if syn:
            md["synonyms"] = syn[:10]
        fmt = _format(x.get("format"))
        if fmt:
            md["format"] = fmt
        self.view["measures"].append(md)
        rows.append(row)
        return True


def _format(bo: str | None) -> dict | None:
    if not bo:
        return None
    if bo.endswith("%"):
        return {"type": "percentage", "decimal_places": {"type": "exact", "places": bo.count("0") - 1 if "." in bo else 0}}
    places = len(bo.split(".")[1]) if "." in bo else 0
    fmt = {"type": "number", "decimal_places": {"type": "exact", "places": places}}
    if "," not in bo:
        fmt["hide_group_separator"] = True      # YAML 1.1 key; there is no thousand_separators
    return fmt


def _order(metrics: list[dict], all_metrics: dict) -> list[dict]:
    names = {m["name"] for m in metrics}
    done, out = set(), []

    def visit(m, stack=()):
        if m["name"] in done or m["name"] in stack:
            return
        x = _x(m)
        deps = list(x.get("components") or [])
        if x.get("window"):
            deps.append(x["window"].get("measure"))
        for d in deps:
            if d in names:
                visit(all_metrics[d], stack + (m["name"],))
        done.add(m["name"])
        out.append(m)
    for m in metrics:
        visit(m)
    return out


def _home(m: Model, metric: dict) -> tuple[str, ...]:
    """The dataset(s) a metric must be computed over. One dataset = a star view on it; several = stacked."""
    x = _x(metric)
    dss = x.get("datasets")
    if not dss:
        dss = sorted({c.table for c in sqlglot.parse_one(_expr_of(metric) or "1").find_all(exp.Column) if c.table})
    comps = [cc for cc in (x.get("components") or []) if cc in m.metrics]
    win = x.get("window")
    if win and win.get("measure") in m.metrics:
        comps.append(win["measure"])
    if comps:
        homes = {_home(m, m.metrics[cc]) for cc in comps}
        if len(homes) == 1:
            return homes.pop()
        return tuple(sorted({d for h in homes for d in h}))
    facts = [d for d in dss if d in m.ds and m.role(d) == "fact"]
    if len(facts) > 1 or (facts and any(d != facts[0] and not _reachable(m, facts[0], d) for d in dss)):
        return tuple(sorted(dss))
    if facts:
        return (facts[0],)
    return (dss[0],) if dss else ()


def _reachable(m: Model, start: str, target: str, depth: int = 0) -> bool:
    if start == target:
        return True
    if depth > MAX_DEPTH:
        return False
    return any(_reachable(m, r["to"], target, depth + 1) for r in m.out_rels.get(start, []))


def _stacked(m: Model, members: tuple[str, ...], metrics: list[dict], catalog_note: str) -> _View | None:
    """UNION ALL of the member datasets on their shared dimension keys."""
    keysets = []
    for ds in members:
        ks = {}
        if m.role(ds) == "fact":
            for r in m.out_rels.get(ds, []):
                if len(r["from_columns"]) == 1:
                    ks[r["to"]] = (r["from_columns"][0], r["to_columns"][0])
        else:
            pk = m.ds[ds].get("primary_key") or []
            if len(pk) == 1:
                ks[ds] = (pk[0], pk[0])
        keysets.append(ks)
    shared = set(keysets[0])
    for ks in keysets[1:]:
        shared &= set(ks)
    if not shared:
        return None
    shared = sorted(shared)
    # columns each member contributes: every field its measures reference
    used = defaultdict(set)
    for met in metrics:
        for c in sqlglot.parse_one(_expr_of(met) or "1").find_all(exp.Column):
            if c.table in members:
                used[c.table].add(c.name)
    selects = []
    key_cols = {d: f"{d}_key" if d in members else f"{d}_key" for d in shared}
    for i, ds in enumerate(members):
        cols = []
        for d in shared:
            fcol = keysets[i][d][0]
            cols.append(f"{m.field_expr(ds, fcol)} AS {key_cols[d]}")
        for ods in members:
            for f in sorted(used[ods]):
                typ = SQL_TYPES.get(m.fields[(ods, f)].get("datatype"), "STRING")
                cols.append(f"{m.field_expr(ds, f)} AS {ods}__{f}" if ods == ds else f"CAST(NULL AS {typ}) AS {ods}__{f}")
        cols.append(f"'{ds}' AS fact_name")
        selects.append(f"SELECT {', '.join(cols)} FROM {m.ds[ds]['source']}")
    name = "_".join(members) + "_stacked_metrics"
    v = _View(m, name, "\n UNION ALL ".join(selects),
              [], f"Stacked metric view over {', '.join(members)} on shared keys {', '.join(shared)}. {catalog_note}")
    v.dim_members = [d for d in members if m.role(d) != "fact"]
    for ods in members:
        for f in used[ods]:
            v.colmap[(ods, f)] = f"{ods}__{f}"
    rels = [{"from": "__stack", "to": d, "from_columns": [key_cols[d]], "to_columns": [keysets[0][d][1]]}
            for d in shared]
    v.base_ds = []
    for d in shared:
        v.colmap[("__stack", key_cols[d])] = key_cols[d]
    m.fields.update({("__stack", key_cols[d]): {"name": key_cols[d], "expression": {"sql": key_cols[d]}} for d in shared})
    v.add_joins("__stack", rels)
    v.add_fields()
    v.view["fields"].insert(0, {"name": "fact_name", "expr": "fact_name", "comment": "Which fact the row came from."})
    v.names.add("fact_name")
    return v


def convert(doc: dict, catalog: str = "cnfinance", schema: str = "semantic", report_metrics: bool = True) -> dict:
    """Returns {"views": {name: yaml_dict}, "ddl": {name: sql}, "report": [rows], "targets": {name: fqn}}."""
    m = Model(doc)
    m.filter_inputs = set()
    for f in _x(doc).get("filters") or []:
        try:
            for c in sqlglot.parse_one(re.sub(r":\w+", "0", f["expression"] or "1"), read="databricks").find_all(exp.Column):
                if c.table:
                    m.filter_inputs.add((c.table, c.name))
        except Exception:  # noqa: BLE001
            pass
    rows: list[dict] = []
    groups: dict[tuple, list] = defaultdict(list)
    for met in m.metrics.values():
        groups[_home(m, met)].append(met)
    views: dict[str, _View] = {}
    model_name = doc.get("name", "model")
    note = f"Generated from Apache Ossie model '{model_name}' by Ontology Studio."
    for home, mets in sorted(groups.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if not home:
            for met in mets:
                rows.append({"element": "metric", "name": met["name"], "scope": _x(met).get("scope", "universe"),
                             "kind": _x(met).get("kind"), "view": "", "status": "Manual",
                             "reason": "no dataset found"})
            continue
        if len(home) == 1:
            ds = home[0]
            name = f"{ds}_metrics"
            v = _View(m, name, m.ds[ds]["source"], [ds],
                      f"Metrics over {ds} ({m.role(ds)}). {note}")
            v.add_joins(ds)
            v.add_fields()
            # derived measures over this fact may need components that live in the same home; included above
        else:
            v = _stacked(m, home, [m.metrics[c] for met in mets for c in (_x(met).get("components") or [])
                                   if c in m.metrics] + mets, note)
            if v is None:
                for met in mets:
                    rows.append({"element": "metric", "name": met["name"], "scope": _x(met).get("scope"),
                                 "kind": _x(met).get("kind"), "view": "", "status": "Manual",
                                 "reason": f"{', '.join(home)} share no dimension key to stack on"})
                continue
        # components defined in another home are re-declared here (as hidden building blocks)
        needed = []
        for met in mets:
            for cname in _x(met).get("components") or []:
                if cname in m.metrics and m.metrics[cname] not in mets and m.metrics[cname] not in needed:
                    needed.append(m.metrics[cname])
            win = _x(met).get("window")
            if win and win.get("measure") in m.metrics and m.metrics[win["measure"]] not in mets + needed:
                needed.append(m.metrics[win["measure"]])
        for met in _order(needed + mets, m.metrics):
            v.add_measure(met, rows if met in mets else [], report_metrics)
        if v.view["measures"]:
            views[v.name] = v
    # datasets, relationships, fields and model-level constructs in the report
    used_ds = {d for v in views.values() for d in v.base_ds + list(v.paths)}
    for ds, d in m.ds.items():
        x = _x(d)
        kind, role = x.get("kind", "table"), m.role(ds)
        if kind == "aggregate":
            st, why = "Partial", "aggregate table: use metric view materialization instead"
        elif ds not in used_ds:
            st, why = "Manual", "not reachable from any metric view (no measure uses it)"
        elif kind == "alias":
            st, why = "Transformed", f"alias of {x.get('alias_of')}: joined again under its own name"
        elif kind == "derived":
            st, why = "Direct", "derived table becomes a SQL source"
        else:
            st, why = "Direct", "source" if role == "fact" else "join"
        rows.append({"element": "dataset", "name": ds, "scope": "universe", "kind": role, "view": "", "status": st,
                     "reason": why})
    for r in m.rels:
        hit = any(r["to"] in v.paths and (r["from"] in v.base_ds or r["from"] in v.paths) for v in views.values())
        rows.append({"element": "relationship", "name": r["name"], "scope": "universe",
                     "kind": _x(r).get("cardinality", "many_to_one"), "view": "",
                     "status": "Direct" if hit else "Manual",
                     "reason": "join" if hit else "not used by any view (fact-to-aggregate or reverse direction)"})
    mx = _x(doc)
    for h in mx.get("hierarchies") or []:
        rows.append({"element": "hierarchy", "name": h["name"], "scope": "universe", "kind": "hierarchy",
                     "view": "", "status": "Partial", "reason": "flattened into level fields (no hierarchy object)"})
    for p in mx.get("parameters") or []:
        rows.append({"element": "prompt", "name": p["name"], "scope": "universe", "kind": "prompt", "view": "",
                     "status": "Partial", "reason": "a report filter on its field; metric view parameters are an "
                                                     "option (runtime 18.2+) but block materialization"})
    for f in mx.get("filters") or []:
        rows.append({"element": "filter", "name": f["name"], "scope": "universe", "kind": "filter", "view": "",
                     "status": "Partial", "reason": "kept as a reusable WHERE; a view-level filter would apply to "
                                                     "every query"})
    for calc in mx.get("report_calculations") or []:
        st = "Manual" if calc.get("status") == "Manual" else "Unsupported"
        rows.append({"element": "report_calc", "name": f"{calc['report']}.{calc['label']}",
                     "scope": f"report:{calc['report']}", "kind": calc.get("kind"), "view": "", "status": st,
                     "reason": calc.get("reason", "") + (" (consumer specific: BI layer)" if st == "Unsupported" else "")})
    for s in mx.get("security_inventory") or []:
        rows.append({"element": "security", "name": s.split("'")[1] if "'" in s else s[:40], "scope": "universe",
                     "kind": "security", "view": "", "status": "Unsupported",
                     "reason": "metric view sources can't carry row filters; decide a Unity Catalog policy"})
    out_views = {n: v.view for n, v in views.items()}
    field_names = {n: v.field_names for n, v in views.items()}
    measure_view = {}
    for r in rows:
        if r["element"] == "metric" and r.get("view") and r["status"] != "Manual":
            measure_view.setdefault(r["name"], r["view"])
    targets = {n: f"{catalog}.{schema}.{n}" for n in out_views}
    ddl = {n: mvlib.ddl(mvlib.to_yaml(out_views[n]), targets[n]) for n in out_views}
    checks = {n: mvlib.check(out_views[n]) for n in out_views}
    measure_views = defaultdict(list)
    for n, v in out_views.items():
        for mm in v["measures"]:
            measure_views[mm["name"]].append(n)
    return {"views": out_views, "ddl": ddl, "report": rows, "targets": targets, "checks": checks,
            "field_names": field_names, "measure_view": measure_view, "measure_views": dict(measure_views)}


def pick_views(mv: dict, metrics: list[str], fields: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """Fewest metric views that cover the metrics and have every field ("ds.field") the question needs: the
    metrics' home view when it qualifies, else the smallest view holding all of them, else each metric's home."""
    metrics = [m for m in metrics if m]
    fields = [f for f in (fields or []) if f]

    def ok(v):
        return all(f in mv["field_names"].get(v, {}) for f in fields)
    common = None
    for m in metrics:
        vs = {v for v in mv.get("measure_views", {}).get(m, []) if ok(v)}
        common = vs if common is None else common & vs
    if metrics and common:
        homes = {mv["measure_view"].get(m) for m in metrics}
        if len(homes) == 1 and next(iter(homes)) in common:
            return [(homes.pop(), metrics)]
        best = sorted(common, key=lambda v: (len(mv["views"][v]["measures"]), v))[0]
        return [(best, metrics)]
    out: dict[str, list[str]] = {}
    for m in metrics:
        v = mv["measure_view"].get(m)
        if v:
            out.setdefault(v, []).append(m)
    return list(out.items())


def report_counts(rows: list[dict]) -> dict:
    out = defaultdict(lambda: defaultdict(int))
    for r in rows:
        out[r["element"]][r["status"]] += 1
    return {k: dict(v) for k, v in out.items()}


def report_md(result: dict) -> str:
    rows = result["report"]
    lines = ["# Ossie → Databricks metric view conversion report", "",
             f"{len(result['views'])} metric views: " + ", ".join(f"`{t}`" for t in result["targets"].values()), "",
             "| Element | Direct | Transformed | Partial | Unsupported | Manual |", "|---|---|---|---|---|---|"]
    for el, c in report_counts(rows).items():
        lines.append(f"| {el} | " + " | ".join(str(c.get(s, 0)) for s in
                                               ("Direct", "Transformed", "Partial", "Unsupported", "Manual")) + " |")
    lines += ["", "| Element | Name | Scope | View | Status | Reason |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['element']} | {r['name']} | {r['scope']} | {r['view']} | {r['status']} | {r['reason']} |")
    bad = {n: c for n, c in result["checks"].items() if c}
    if bad:
        lines += ["", "## Checks that failed", ""] + [f"- `{n}`: {'; '.join(c)}" for n, c in bad.items()]
    return "\n".join(lines) + "\n"
