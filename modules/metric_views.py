"""
Export the ontology as Databricks metric views (YAML spec 1.1), one per fact class.

  source     : the fact table (three-part name for Unity Catalog sources)
  joins      : many-to-one relationships from the fact, nested for snowflake
               schemas (max depth 3)
  fields     : the fact's dimension/time columns + joined classes' dimension columns
               (`fields` is the current key; `dimensions` is its backward-compatible synonym)
  measures   : ontology metrics whose base_class is the fact (SQL translated to
               metric-view references), plus SUM/AVG of measures without one

Follows https://docs.databricks.com/aws/en/uc-semantics/metric-views/yaml-reference :
names unique across fields and measures ignoring case, display_name <= 255 chars,
at most 10 synonyms of <= 255 chars, measures must aggregate, `rely.at_most_one_match`
only on joins that land on the joined table's key.

`ddl()` wraps a definition in CREATE OR REPLACE VIEW … WITH METRICS LANGUAGE YAML.
"""
from __future__ import annotations

import re

import yaml

MAX_DEPTH = 3
MAX_TEXT, MAX_SYNONYMS = 255, 10
RESERVED = {"date", "time", "timestamp", "order", "user", "group", "year", "month", "day", "select",
            "table", "from", "where", "join", "on", "by", "source", "case", "end", "interval", "values",
            "current", "all", "any", "as", "key", "location", "range", "row", "rows", "window"}


def _snake(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"\W+", "_", s).strip("_").lower()


def _title(s: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[_\s]+", s) if w)


def _src(c: dict, cid: str) -> str:
    return c.get("source_fqn") or c.get("source_table") or cid


def _col(p: dict, pid: str) -> str:
    if p.get("expression"):          # computed field (e.g. imported from an Apache Ossie model)
        return f"({p['expression']})"
    c = p.get("source_column", pid)
    return c if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", c) else f"`{c}`"


def _dim_expr(p: dict, pid: str, prefix: str) -> str:
    """Dimension expression; columns of a joined class are prefixed with the join path.
    A computed expression gets the prefix on each of its bare columns."""
    if not prefix or not p.get("expression"):
        return f"{prefix}{_col(p, pid)}"
    parts = prefix.rstrip(".").split(".")
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(p["expression"], read="databricks")
        if len(parts) > 3:
            raise ValueError("join path too deep to qualify")
        for col in list(tree.find_all(exp.Column)):
            if col.table or isinstance(col.this, exp.Star):
                continue
            new = exp.column(col.name, table=parts[-1], db=parts[-2] if len(parts) > 1 else None,
                             catalog=parts[-3] if len(parts) > 2 else None)
            if col is tree:
                tree = new
            else:
                col.replace(new)
        return tree.sql(dialect="databricks")
    except Exception:  # noqa: BLE001
        return f"{prefix}{_col(p, pid)}"


def _synonyms(items) -> list[str]:
    out, seen = [], set()
    for x in items or []:
        x = str(x).strip()[:MAX_TEXT]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:MAX_SYNONYMS]


def _display(text: str) -> str:
    return str(text)[:MAX_TEXT]


def is_aggregate(sql: str) -> bool:
    """A measure expression must aggregate (SUM, COUNT, … or a ratio of aggregates)."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="databricks")
        return any(True for _ in tree.find_all(exp.AggFunc)) or bool(
            re.search(r"\b(any_value|approx_count_distinct|percentile\w*|median|mode|count_if|"
                      r"bool_and|bool_or|collect_\w+|stddev\w*|variance|var_\w+|measure)\s*\(", sql, re.I))
    except Exception:  # noqa: BLE001
        return bool(re.search(r"\b(sum|count|avg|min|max|count_if|median|percentile\w*|stddev\w*|"
                              r"variance|any_value|approx_count_distinct|measure)\s*\(", sql, re.I))


def _joins_on_key(onto: dict, p: dict) -> bool:
    """True when the join columns are the joined table's identifier (so at most one row matches)."""
    tgt = onto["classes"].get(p["range"], {})
    if tgt.get("identifier_inferred"):          # a guessed key is no guarantee: wrong rely = wrong SUM/COUNT
        return False
    cols = {t.rpartition(".")[2].lower() for t in p["join"]["to"]}
    ident = tgt.get("identifier")
    keys = [ident] if isinstance(ident, str) else list(ident or [])
    keys += [pid for pid, dp in tgt.get("data_properties", {}).items() if dp.get("identifier")] if not keys else []
    key_cols = {str(tgt.get("data_properties", {}).get(k, {}).get("source_column", k)).lower() for k in keys}
    if key_cols and cols == key_cols:
        return True
    for uk in tgt.get("unique_keys") or []:
        if {str(c).lower() for c in uk} == cols:
            return True
    return False


def fact_classes(onto: dict) -> list[str]:
    facts = [cid for cid, c in onto["classes"].items() if c.get("role") == "fact"]
    if facts:
        return facts
    return [cid for cid, c in onto["classes"].items()          # fallback: anything with measures
            if any(p.get("semantic_role") == "measure" for p in c["data_properties"].values())]


def _many_to_one(onto: dict, cid: str):
    for pid, p in onto["object_properties"].items():
        if p["domain"] == cid and p.get("join") and p["cardinality"] in ("many-to-one", "one-to-one"):
            yield pid, p


def build_metric_view(onto: dict, fact: str, include_synonyms: bool = True) -> dict:
    classes = onto["classes"]
    fc = classes[fact]
    used_alias: set[str] = {"source"}
    alias_of: dict[str, str] = {fc.get("source_table", fact).lower(): "source"}
    dims: list[dict] = []
    dim_names: set[str] = set()

    def add_dim(name, expr, p=None):
        name = _snake(name)
        if name in dim_names:
            return
        dim_names.add(name)
        d = {"name": name, "expr": expr}
        if p:
            if p.get("description"):
                d["comment"] = p["description"]
            d["display_name"] = _display(_title(p.get("label", name)))
            if include_synonyms and _synonyms(p.get("synonyms")):
                d["synonyms"] = _synonyms(p.get("synonyms"))
        dims.append(d)

    def dims_of(cid: str, prefix: str, path_label: str):
        for pid, p in classes[cid]["data_properties"].items():
            if p.get("semantic_role") in ("dimension", "time") or (
                    prefix and p.get("semantic_role") is None and not p.get("identifier")):
                expr = _dim_expr(p, pid, prefix)
                col = p.get("source_column", pid)
                stem = path_label.rstrip("_").removesuffix("_dim")
                name = col if (not path_label or col.lower().startswith(stem)) else f"{stem}_{col}"
                add_dim(name, expr, p)

    def joins_of(cid: str, parent_alias: str, path: str, depth: int, seen: set):
        out = []
        if depth > MAX_DEPTH:
            return out
        for pid, p in _many_to_one(onto, cid):
            tgt = p["range"]
            if tgt in seen or tgt == fact:
                continue
            alias = _snake(tgt)
            if alias in RESERVED:
                alias = f"{alias}_dim"
            base = alias
            i = 2
            while alias in used_alias:
                alias, i = f"{base}_{i}", i + 1
            used_alias.add(alias)
            tc = classes[tgt]
            alias_of.setdefault(tc.get("source_table", tgt).lower(), f"{path}{alias}")
            conds = []
            for f, t in zip(p["join"]["from"], p["join"]["to"]):
                fcol, tcol = f.rpartition(".")[2], t.rpartition(".")[2]
                conds.append(f"{parent_alias}.{fcol} = {alias}.{tcol}")
            j = {"name": alias, "source": _src(tc, tgt), "on": " AND ".join(conds)}
            if p["cardinality"] in ("many-to-one", "one-to-one") and _joins_on_key(onto, p):
                j["rely"] = {"at_most_one_match": True}
            full_path = f"{path}{alias}."
            dims_of(tgt, full_path, f"{alias}_")
            nested = joins_of(tgt, alias, full_path, depth + 1, seen | {tgt})
            if nested:
                j["joins"] = nested
            out.append(j)
        return out

    joins = joins_of(fact, "source", "", 1, {fact})
    # the fact's own dimensions go first
    own_before = list(dims)
    dims.clear()
    dim_names.clear()
    dims_of(fact, "", "")
    for d in own_before:
        if d["name"] not in dim_names:
            dim_names.add(d["name"])
            dims.append(d)

    # ---- measures --------------------------------------------------------
    measures, mnames = [], set()

    def translate(sql: str) -> str:
        """table.column -> source.column / alias.column ; leave bare columns."""
        def sub(m):
            tbl, col = m.group(1), m.group(2)
            a = alias_of.get(tbl.lower())
            return f"{a}.{col}" if a and a != "source" else col if a == "source" else m.group(0)
        return re.sub(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b", sub, sql)

    skipped = []
    for m in onto.get("metrics", []):
        if m.get("base_class") != fact or not m.get("sql"):
            continue
        if not is_aggregate(m["sql"]):
            skipped.append(m["name"])          # a measure must aggregate; the spec rejects row-level exprs
            continue
        name = _snake(m["name"])
        if name in mnames or name in dim_names:
            name = f"{name}_measure"
        if name in mnames:
            continue
        mnames.add(name)
        e = {"name": name, "expr": translate(m["sql"]), "display_name": _display(_title(m["name"]))}
        if m.get("description"):
            e["comment"] = m["description"]
        if include_synonyms and _synonyms(m.get("synonyms")):
            e["synonyms"] = _synonyms(m.get("synonyms"))
        measures.append(e)
    exprs = {x["expr"].replace(" ", "").lower() for x in measures}
    for pid, p in fc["data_properties"].items():
        if p.get("semantic_role") != "measure":
            continue
        agg = p.get("aggregation", "SUM")
        expr = f"COUNT(DISTINCT {_col(p, pid)})" if agg == "COUNT_DISTINCT" else f"{agg}({_col(p, pid)})"
        if expr.replace(" ", "").lower() in exprs:
            continue
        name = _snake(f"{agg.lower()}_{p.get('source_column', pid)}")
        if name in mnames or name in dim_names:
            continue
        mnames.add(name)
        measures.append({"name": name, "expr": expr,
                         "display_name": _display(_title(f"{agg} {p.get('label', pid)}"))})
    if not measures:
        measures.append({"name": "row_count", "expr": "COUNT(1)", "display_name": "Row Count"})

    mv = {"version": "1.1"}
    if fc.get("description"):
        mv["comment"] = fc["description"]
    mv["source"] = _src(fc, fact)
    if joins:
        mv["joins"] = joins
    if dims:
        mv["fields"] = dims
    mv["measures"] = measures
    if skipped:
        mv["comment"] = (mv.get("comment", "") + ("; " if mv.get("comment") else "")
                         + "not aggregations, left out: " + ", ".join(skipped))[:1000]
    return mv


# --------------------------------------------------------------------------- #
# Guided builder (Metric views tab → ➕ New metric view)
# --------------------------------------------------------------------------- #
WINDOW = re.compile(r"\bOVER\s*\(", re.I)
WINDOW_HINT = ("a window function (OVER …) can't be part of a measure expression; metric views express "
               "windows with a `window:` spec, so untick it or rewrite it")


def _foreign(expr: str, known: set[str]) -> list[str]:
    """Table qualifiers an expression uses that are neither `source` nor a join of the view."""
    bare = re.sub(r"'(?:[^']|'')*'", "''", expr)             # not inside string literals
    return sorted({q for q in re.findall(r"\b([A-Za-z_]\w*)\.[A-Za-z_`]", bare) if q.lower() not in known})


def _join_paths(joins, prefix: str = ""):
    """(alias path, join) for every join; nested joins as parent.child."""
    for j in joins or []:
        path = f"{prefix}{j['name']}"
        yield path, j
        yield from _join_paths(j.get("joins"), f"{path}.")


def _values_hint(p: dict) -> str:
    vals = p.get("allowed_values") or p.get("samples") or []
    if not vals:
        return ""
    shown = ", ".join(str(v) for v in vals[:4]) + (", …" if len(vals) > 4 else "")
    return f"{len(vals)} values: {shown}" if p.get("allowed_values") else f"e.g. {shown}"


def _qualify(sql: str, alias_of: dict[str, str]) -> str | None:
    """Class.column (learned SQL) → metric-view reference; None when a class is not in the view."""
    missing = []

    def sub(m):
        a = alias_of.get(m.group(1).lower())
        if a is None:
            missing.append(m.group(1))
            return m.group(0)
        return f"{a}.{m.group(2)}" if a else m.group(2)
    out = re.sub(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", sub, sql)
    return None if missing else out


def suggest(onto: dict, fact: str) -> dict:
    """Candidates for a new metric view on `fact`, each with why it is suggested.

    Ticked: what build_metric_view generates (same rules). Offered, unticked: a row count, a distinct
    count per joined table and the aggregates people actually run (learned snippets, by authority).
    filters: learned WHERE conditions on the fact's own columns; values: profiled values to filter on."""
    classes = onto["classes"]
    base = build_metric_view(onto, fact)
    by_src: dict[str, str] = {}
    for cid, c in classes.items():
        by_src.setdefault(_src(c, cid), cid)
    paths = [("", fact)] + [(path, by_src.get(j["source"])) for path, j in _join_paths(base.get("joins"))]
    alias_of: dict[str, str] = {}
    for path, cid in paths:
        if cid:
            alias_of.setdefault(cid.lower(), path)       # ponytail: a class joined twice → its first alias
    tops = [j["name"] for j in base.get("joins", [])]

    def via(expr: str) -> list[str]:
        return [a for a in tops if re.search(rf"\b{re.escape(a)}\.", expr)]

    snips = sorted((s for s in onto.get("snippets") or [] if s.get("status") != "dismissed"),
                   key=lambda s: -(s.get("authority") or 0))
    usage: dict[str, int] = {}
    for s in snips:
        for col in s.get("columns") or []:
            usage[col.lower()] = usage.get(col.lower(), 0) + (s.get("uses") or 0)

    origin: dict[str, tuple] = {}
    for path, cid in paths:
        for pid, p in (classes[cid]["data_properties"].items() if cid else []):
            origin.setdefault(_dim_expr(p, pid, f"{path}." if path else ""), (cid, pid, p))

    ranked, values = [], {}
    for f in base.get("fields", []):
        cid, pid, p = origin.get(f["expr"], (None, None, {}))
        used = usage.get(f"{cid}.{p.get('source_column', pid)}".lower(), 0) if cid else 0
        why = (["time column"] if p.get("semantic_role") == "time" else []) + [
            f"{cid} column" if cid == fact else f"from {cid}" if cid else "generated field"]
        why += [h for h in (_values_hint(p), f"used in {used} queries" if used else "") if h]
        row = {"pick": True, "name": f["name"], "display_name": f.get("display_name") or _title(f["name"]),
               "expr": f["expr"], "why": " · ".join(why), "via": via(f["expr"]),
               "comment": f.get("comment"), "synonyms": f.get("synonyms")}
        grains = []
        if str(p.get("range", "")).lower().endswith(("date", "datetime")):      # time grains for trends
            grains = [{**row, "pick": False, "name": f"{g.lower()}_of_{f['name']}", "comment": None, "synonyms": None,
                       "display_name": f"{row['display_name']} ({g.title()})", "expr": f"DATE_TRUNC('{g}', {f['expr']})",
                       "why": f"{g.lower()} of {f['name']}, for {g.lower()}ly trends"} for g in ("MONTH", "YEAR")]
        ranked.append(((p.get("semantic_role") != "time", -used), [row, *grains]))
        if p.get("allowed_values") and cid == fact:
            values[f["name"]] = {"expr": f["expr"], "values": list(p["allowed_values"])}
    fields = [r for _, rows in sorted(ranked, key=lambda x: x[0]) for r in rows]
    columns = sorted(({"expr": e, "label": f"{e} · {p.get('label', pid)}", "via": via(e),
                       "measure": p.get("semantic_role") == "measure",
                       "numeric": p.get("semantic_role") != "key" and not p.get("identifier") and (
                           p.get("semantic_role") == "measure" or str(p.get("range", "")).split(":")[-1].lower()
                           in ("integer", "int", "long", "short", "decimal", "double", "float"))}
                      for e, (cid, pid, p) in origin.items()), key=lambda c: not c["measure"])

    metrics = {_snake(m["name"]): m for m in onto.get("metrics", []) if m.get("base_class") == fact}
    measures, seen = [], set()

    def add(pick, name, expr, why, display=None, comment=None, synonyms=None, keep=False):
        k = re.sub(r"\s+", "", expr).lower()            # extra ideas only when no measure computes it yet
        if keep or k not in seen:
            seen.add(k)
            measures.append({"pick": pick, "name": name, "display_name": display or _title(name), "expr": expr,
                             "why": why, "via": via(expr), "comment": comment, "synonyms": synonyms})

    known = {"source"} | {p.rpartition(".")[2].lower() for p, _ in _join_paths(base.get("joins"))}
    for m in base["measures"]:
        src = metrics.get(m["name"]) or metrics.get(m["name"].removesuffix("_measure"))
        why = (("certified metric" if src.get("status") == "certified" else "metric in the model") if src
               else f"number of {fact} rows" if m["name"] == "row_count"
               else f"{m['expr'].split('(')[0].upper()} of a measure column")
        bad = (WINDOW_HINT if WINDOW.search(m["expr"]) else
               f"it reads {', '.join(_foreign(m['expr'], known))}, a table {fact} has no join to"
               if _foreign(m["expr"], known) else "")
        add(not bad, m["name"], m["expr"], why + (f" · left unticked: {bad}" if bad else ""),
            m.get("display_name"), m.get("comment"), m.get("synonyms"), keep=True)
    add(False, "row_count", "COUNT(1)", f"number of {fact} rows", "Row Count")
    for j in base.get("joins", []):
        keys = re.findall(r"\bsource\.(\w+)\s*=", j["on"])
        if len(keys) == 1:
            add(False, f"{j['name']}_count", f"COUNT(DISTINCT {keys[0]})", f"distinct {j['name']} (via {keys[0]})",
                f"Number Of {_title(j['name'])}")
    filters = []
    for s in snips:
        expr = _qualify(s.get("text") or "", alias_of)
        if not expr:
            continue
        people = len(s.get("users") or {})
        why = (f"learned: used {s.get('uses') or 0}× by {people} {'person' if people == 1 else 'people'}"
               + (f" · authority {s['authority']:.2f}" if s.get("authority") else ""))
        if s.get("kind") == "expression" and is_aggregate(expr):
            add(False, _snake(s.get("name") or "") or f"learned_{len(measures)}", expr, why)
        elif s.get("kind") == "filter" and not via(expr) and not is_aggregate(expr):
            filters.append({"expr": expr, "why": why})   # the YAML filter runs on the source's own rows

    joins = []
    for j in base.get("joins", []):
        nested = [p for p, _ in _join_paths(j.get("joins"), f"{j['name']}.")]
        joins.append({"name": j["name"], "why": f"many-to-one on {j['on']}" + (" · key join" if j.get("rely") else "")
                      + (f" · brings {', '.join(nested)}" if nested else "")})
    return {"base": base, "joins": joins, "fields": fields, "measures": measures, "filters": filters,
            "values": values, "columns": columns}


def compose(base: dict, joins: list[str], fields: list[dict], measures: list[dict], filter_: str = "",
            comment: str = "") -> dict:
    """The metric view the builder's picks describe: base's source, the ticked top-level joins (nested
    joins come with them), the ticked fields and measures, and an optional filter."""
    def clean(rows):
        out = []
        for r in rows:
            name, expr = str(r.get("name") or "").strip(), str(r.get("expr") or "").strip()
            if not (r.get("pick") and name and expr):
                continue
            if not re.match(r"^[A-Za-z_]\w*$", name):
                name = _snake(name) or "unnamed"
            d = {"name": name, "expr": expr,
                 "display_name": _display(str(r.get("display_name") or "").strip() or _title(name))}
            if r.get("comment"):
                d["comment"] = r["comment"]
            if r.get("synonyms"):
                d["synonyms"] = _synonyms(r["synonyms"])
            out.append(d)
        return out

    view = {"version": base.get("version", "1.1")}
    if comment.strip():
        view["comment"] = comment.strip()
    view["source"] = base["source"]
    if filter_.strip():
        view["filter"] = filter_.strip()
    kept = [j for j in base.get("joins", []) if j["name"] in joins]
    if kept:
        view["joins"] = kept
    f = clean(fields)
    if f:
        view["fields"] = f
    view["measures"] = clean(measures)
    return view


def check(view: dict, base: dict | None = None) -> list[str]:
    """What Databricks would reject in this metric view (empty list = fine)."""
    out = []
    fields, measures = list(view.get("fields") or []), list(view.get("measures") or [])
    if not measures:
        out.append("Tick or add at least one measure.")
    seen, dups = set(), []
    for x in fields + measures:
        if x["name"].lower() in seen and x["name"] not in dups:
            dups.append(x["name"])
        seen.add(x["name"].lower())
    if dups:
        out.append("Names must be unique across fields and measures (ignoring case): " + ", ".join(dups))
    out += [f"Measure `{m['name']}` must aggregate (SUM, COUNT, AVG …): {m['expr']}"
            for m in measures if not is_aggregate(m["expr"])]
    out += [f"Measure `{m['name']}`: {WINDOW_HINT}" for m in measures if WINDOW.search(m["expr"])]
    out += [f"Field `{f['name']}` aggregates, so make it a measure: {f['expr']}"
            for f in fields if is_aggregate(f["expr"])]
    for i, m in enumerate(measures):
        earlier = {x["name"].lower() for x in measures[:i]}
        bad = [r for r in re.findall(r"\bMEASURE\(\s*`?([^`)]+?)`?\s*\)", m["expr"], re.I)
               if r.strip().lower() not in earlier]
        if bad:
            out.append(f"Measure `{m['name']}` uses MEASURE({', '.join(bad)}): tick that measure and keep it above.")
    if view.get("filter") and is_aggregate(view["filter"]):
        out.append("The filter can't aggregate: it keeps or drops source rows.")
    kept = {p for p, _ in _join_paths(view.get("joins"))}
    every = list(_join_paths((base or view).get("joins")))
    gone = [p.rpartition(".")[2] for p, _ in every if p not in kept]
    known = {"source"} | {p.rpartition(".")[2].lower() for p, _ in every}
    for x in fields + measures + ([{"name": "filter", "expr": view["filter"]}] if view.get("filter") else []):
        hit = [a for a in gone if re.search(rf"\b{re.escape(a)}\.", x["expr"])]
        if hit:
            out.append(f"`{x['name']}` uses {', '.join(hit)}, which is not joined: tick the join or untick it.")
        if _foreign(x["expr"], known):
            out.append(f"`{x['name']}` reads {', '.join(_foreign(x['expr'], known))}, a table this view doesn't "
                       "join: untick it or rewrite it.")
    return out


class _Dumper(yaml.SafeDumper):
    pass


def to_yaml(mv: dict) -> str:
    # PyYAML quotes the key 'on' automatically (YAML 1.1 would read it as a boolean)
    return yaml.dump(mv, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)


def view_name(onto: dict, fact: str) -> str:
    return f"{_snake(fact)}_metrics"


def ddl(mv_yaml: str, target: str) -> str:
    return f"CREATE OR REPLACE VIEW {target}\nWITH METRICS\nLANGUAGE YAML\nAS $$\n{mv_yaml}$$"


def build_all(onto: dict) -> dict[str, dict]:
    return {fact: build_metric_view(onto, fact) for fact in fact_classes(onto)}
