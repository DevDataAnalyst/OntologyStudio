"""
Convert between Apache Ossie semantic models and the studio's ontology dict.

to_ontology(doc)    Ossie model (0.2.0.dev0 / 0.3.0.dev0 flat shape) -> ontology:
                      dataset       -> class (source -> source_fqn / source_table)
                      field         -> data property (datatype -> XSD range, is_time -> time role,
                                       computed expressions kept as `expression`)
                      primary_key   -> identifier;  unique_keys -> unique_keys
                      relationship  -> many-to-one object property (+ inverse), join on the
                                       fields' source columns
                      metrics       -> metrics (dataset- and model-scoped), SQL rewritten to source
                                       columns in Databricks SQL so metric views and Ask can use it
                    Ossie-only details (all dialect variants, ai_context, labels, vendor
                    extensions) are kept under each element's `ossie` key for the round trip.

from_ontology(onto) ontology -> Ossie 0.3.0.dev0 document. Join columns that are not data
                    properties become declared fields (Ossie keys and relationships name fields),
                    many-to-many relationships become their junction dataset plus two
                    relationships, and studio-only details (roles, aggregations, value meanings,
                    rules, layout) travel in nonportable_extensions under vendor ONTOLOGY_STUDIO.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from functools import lru_cache

from ..ontology_builder import PREFIXES, camel, label, pascal, plural, refresh_index, singular
from .validator import CURRENT_VERSION, STUDIO_VENDOR, SQLGLOT_DIALECTS, TEMPORAL_TYPES

try:
    import sqlglot
    from sqlglot import exp
    SQLGLOT = True
except ImportError:  # pragma: no cover
    SQLGLOT = False

DT_TO_XSD = {"String": "xsd:string", "Integer": "xsd:integer", "Decimal": "xsd:decimal", "Float": "xsd:double",
             "Boolean": "xsd:boolean", "Date": "xsd:date", "Time": "xsd:time", "DateTime": "xsd:dateTime",
             "DateTimeTz": "xsd:dateTime", "Opaque": "rdfs:Literal"}
XSD_TO_DT = {"xsd:string": "String", "xsd:integer": "Integer", "xsd:long": "Integer", "xsd:int": "Integer",
             "xsd:short": "Integer", "xsd:decimal": "Decimal", "xsd:double": "Float", "xsd:float": "Float",
             "xsd:boolean": "Boolean", "xsd:date": "Date", "xsd:time": "Time", "xsd:dateTime": "DateTime",
             "xsd:dateTimeStamp": "DateTimeTz", "xsd:duration": "Opaque", "xsd:base64Binary": "Opaque",
             "rdf:JSON": "Opaque", "rdfs:Literal": "Opaque"}
PREFERRED_DIALECTS = ["OSSIE_SQL_2026", "ANSI_SQL", "DATABRICKS", "SNOWFLAKE", "BIGQUERY", "POSTGRES", "TRINO",
                      "REDSHIFT", "CLICKHOUSE"]
EXT_KEYS = ("nonportable_extensions", "custom_extensions")
AGG_NAMES = {"Sum": "SUM", "Avg": "AVG", "Min": "MIN", "Max": "MAX"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _list(v) -> list:
    return v if isinstance(v, list) else []


def _dicts(v) -> list:
    return [x for x in _list(v) if isinstance(x, dict)]


def _jsonable(v):
    """JSON-safe copy (Decimal, dates and bytes from profiling become strings)."""
    return json.loads(json.dumps(v, default=str))


def _ext_entries(el: dict) -> list[dict]:
    """Extension entries of an element (either spelling), data parsed to a dict when possible."""
    out = []
    for key in EXT_KEYS:
        for e in _dicts(el.get(key)):
            data = e.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    pass
            out.append({"vendor_name": e.get("vendor_name"), "data": data})
    return out


def _studio(el: dict) -> dict:
    for e in _ext_entries(el):
        if e.get("vendor_name") == STUDIO_VENDOR and isinstance(e.get("data"), dict):
            return e["data"]
    return {}


def _foreign_exts(el: dict) -> list[dict]:
    """Extensions of other vendors, as 0.3.0.dev0 entries (data as an object)."""
    out = []
    for e in _ext_entries(el):
        if e.get("vendor_name") == STUDIO_VENDOR:
            continue
        data = e.get("data")
        out.append({"vendor_name": e.get("vendor_name"), "data": data if isinstance(data, dict) else {"value": data}})
    return out


def _ai(el: dict) -> dict:
    ai = el.get("ai_context")
    if isinstance(ai, str):
        return {"instructions": ai}
    return dict(ai) if isinstance(ai, dict) else {}


def _synonyms(el: dict) -> list[str]:
    return [s for s in _list(_ai(el).get("synonyms")) if isinstance(s, str) and s.strip()]


def pick_expression(expr) -> tuple[str | None, str | None]:
    """The SQL to use inside the studio: `sql`, else the most portable SQL dialect entry."""
    if not isinstance(expr, dict):
        return None, None
    if isinstance(expr.get("sql"), str) and expr["sql"].strip():
        return expr["sql"], "OSSIE_SQL_2026"
    entries = [d for d in _dicts(expr.get("dialects")) if isinstance(d.get("expression"), str)]
    for want in PREFERRED_DIALECTS:
        for d in entries:
            if d.get("dialect") == want:
                return d["expression"], want
    return (entries[0]["expression"], entries[0].get("dialect")) if entries else (None, None)


def _bare_column(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if _IDENT.match(t):
        return t
    m = re.match(r'^"((?:[^"]|"")+)"$', t) or re.match(r"^`((?:[^`]|``)+)`$", t)
    return m.group(1) if m else None


def _read(dialect: str | None):
    return SQLGLOT_DIALECTS.get(dialect) if dialect in SQLGLOT_DIALECTS else None


def _parse(text: str, read=None):
    if not SQLGLOT or not text:
        return None
    try:
        return sqlglot.parse_one(text, read=read)
    except Exception:  # noqa: BLE001
        try:
            sel = sqlglot.parse_one(f"SELECT {text}", read=read)
            return sel.expressions[0] if sel.expressions else None
        except Exception:  # noqa: BLE001
            return None


def _map_columns(tree, fn):
    """Replace column nodes (collected first, so replacements are not visited again)."""
    tree = tree.copy()
    for col in list(tree.find_all(exp.Column)):
        if isinstance(col.this, exp.Star):
            continue
        new = fn(col)
        if new is None or new is col:
            continue
        if col is tree:
            tree = new
        else:
            col.replace(new)
    return tree


@lru_cache(maxsize=2048)
def quote_ident(name: str) -> str:
    """An identifier as portable SQL: bare when it parses back as itself, else double-quoted."""
    if _IDENT.match(name) and SQLGLOT:
        try:
            t = sqlglot.parse_one(name)
            if isinstance(t, exp.Column) and t.name == name and not t.table:
                return name
        except Exception:  # noqa: BLE001
            pass
    elif _IDENT.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _col_node(name: str, table: str | None = None):
    return exp.column(name, table=table, quoted=None) if table else exp.column(name)


# --------------------------------------------------------------------------- #
# Ossie -> ontology
# --------------------------------------------------------------------------- #
def to_ontology(doc: dict, file_name: str = "", base_iri: str = "http://example.org/ontology/",
                prefix: str = "ex") -> dict:
    """Build a studio ontology from one flat Ossie model (run upgrade() on 0.1.1 files first)."""
    from .. import semantics

    warnings: list[str] = []
    studio = _studio(doc)
    name = str(doc.get("name") or "OssieModel")
    datasets = [d for d in _dicts(doc.get("datasets")) if isinstance(d.get("name"), str) and d["name"]]

    classes: dict = {}
    cid_of: dict[str, str] = {}             # dataset name (casefold) -> class id
    fields_of: dict[str, dict] = {}         # class id -> {field casefold: (pid, column, expression)}
    for ds in datasets:
        cid = ds["name"]
        if cid in classes:
            n = 2
            while f"{cid}_{n}" in classes:
                n += 1
            warnings.append(f"Dataset name '{cid}' repeats; the second one became '{cid}_{n}'.")
            cid = f"{cid}_{n}"
        cid_of.setdefault(ds["name"].casefold(), cid)
        classes[cid] = _class_from_dataset(ds, cid)
        fields_of[cid] = {pid.casefold(): (pid, p.get("source_column"), p.get("expression"))
                          for pid, p in classes[cid]["data_properties"].items()}

    def table_of(cid):
        return classes[cid].get("source_table", cid)

    def phys(cid, field_name) -> str:
        hit = fields_of.get(cid, {}).get(str(field_name).casefold())
        if hit and hit[1]:
            return hit[1]
        return str(field_name)

    # ---- relationships -> object properties ---------------------------------
    ops: dict = {}

    def unique_pid(cand: str) -> str:
        base, i = cand, 2
        while cand in ops:
            cand, i = f"{base}{i}", i + 1
        return cand

    key_fields: dict[str, set] = {cid: set() for cid in classes}
    for r in _dicts(doc.get("relationships")):
        dom, rng = cid_of.get(str(r.get("from")).casefold()), cid_of.get(str(r.get("to")).casefold())
        if not dom or not rng:
            warnings.append(f"Relationship '{r.get('name')}' skipped: dataset '{r.get('from') if not dom else r.get('to')}'"
                            " is not in the model.")
            continue
        fcols, tcols = _list(r.get("from_columns")), _list(r.get("to_columns"))
        if not fcols or len(fcols) != len(tcols):
            warnings.append(f"Relationship '{r.get('name')}' skipped: from_columns and to_columns do not pair up.")
            continue
        sx = _studio(r)
        dkeys = [{str(x).casefold() for x in k} for k in
                 [classes[dom].get("identifier") or []] + list(classes[dom].get("unique_keys") or []) if k]
        one = any({str(c).casefold() for c in fcols} == k for k in dkeys)
        card = sx.get("cardinality") if sx.get("cardinality") in ("many-to-one", "one-to-one") else \
            ("one-to-one" if one else "many-to-one")
        fwd = unique_pid(str(r.get("name") or f"{dom}_to_{rng}"))
        if sx.get("inverse"):
            inv_name = str(sx["inverse"])
        elif dom == rng:
            inv_name = f"{fwd}Of"
        else:
            stem = singular(dom)
            inv_name = "has" + pascal(stem if card == "one-to-one" else plural(stem))
        if inv_name in ops or inv_name == fwd:            # e.g. several facts -> one dimension
            inv_name = camel(rng) + inv_name[:1].upper() + inv_name[1:]
        inv = unique_pid(inv_name)
        stash = {k: copy.deepcopy(v) for k, v in r.items()
                 if k not in ("name", "from", "to") + EXT_KEYS}
        if _foreign_exts(r):
            stash["nonportable_extensions"] = _foreign_exts(r)
        fwd_prop = {"label": label(fwd), "domain": dom, "range": rng, "cardinality": card, "functional": True,
                    "required": bool(sx.get("required", False)), "inverse": inv,
                    "join": {"from": [f"{table_of(dom)}.{phys(dom, c)}" for c in fcols],
                             "to": [f"{table_of(rng)}.{phys(rng, c)}" for c in tcols]},
                    "provenance": sx.get("provenance", "ossie"), "ossie": stash}
        if _ai(r).get("instructions"):
            fwd_prop["description"] = _ai(r)["instructions"]
        if isinstance(sx.get("confidence"), (int, float)):
            fwd_prop["confidence"] = sx["confidence"]
        ops[fwd] = fwd_prop
        ops[inv] = {"label": label(inv), "domain": rng, "range": dom,
                    "cardinality": "one-to-one" if card == "one-to-one" else "one-to-many",
                    "functional": card == "one-to-one", "inverse": fwd, "provenance": fwd_prop["provenance"]}
        key_fields[dom].update(str(c).casefold() for c in fcols)
        key_fields[rng].update(str(c).casefold() for c in tcols)

    # ---- metrics --------------------------------------------------------------
    metrics, names = [], set()
    rel_pairs = [(p["domain"], p["range"]) for p in ops.values() if p.get("join")]

    def add_metric(m: dict, base: str | None, sql: str | None, scope: str, ds_name: str | None):
        nm = str(m.get("name"))
        if nm in names:
            nm = f"{ds_name}_{nm}" if ds_name else f"{nm}_2"
        names.add(nm)
        st = {"scope": scope, "name": m.get("name"), "sql": sql,
              **{k: copy.deepcopy(v) for k, v in m.items() if k not in ("name", "description") + EXT_KEYS}}
        if ds_name:
            st["dataset"] = ds_name
        if _foreign_exts(m):
            st["nonportable_extensions"] = _foreign_exts(m)
        entry = {"name": nm, "description": m.get("description") or "", "base_class": base,
                 "sql": sql or "", "provenance": _studio(m).get("provenance", "ossie"), "ossie": st}
        if _synonyms(m):
            entry["synonyms"] = _synonyms(m)
        metrics.append(entry)

    measure_hits: list[tuple[str, str, str]] = []      # (class, field casefold, aggregation)
    for ds in datasets:
        cid = cid_of[ds["name"].casefold()]
        for m in _dicts(ds.get("metrics")):
            text, dialect = pick_expression(m.get("expression"))
            sql, hits = _dataset_metric_sql(text, dialect, cid, fields_of)
            measure_hits += hits
            if text and sql is None:
                warnings.append(f"Metric '{ds['name']}.{m.get('name')}' kept as written ({dialect}); it could not be "
                                "translated to the source columns.")
            add_metric(m, cid, sql if sql is not None else text, "dataset", ds["name"])
    for m in _dicts(doc.get("metrics")):
        text, dialect = pick_expression(m.get("expression"))
        refs = _referenced_datasets(text, dialect, cid_of)
        base = _choose_base(refs, rel_pairs)
        sql, hits = _model_metric_sql(text, dialect, base, cid_of, fields_of, table_of)
        measure_hits += hits
        if text and sql is None:
            warnings.append(f"Metric '{m.get('name')}' kept as written ({dialect}); it could not be translated.")
        add_metric(m, base, sql if sql is not None else text, "model", None)

    # ---- roles we know from the model (classify() fills in the rest) ----------
    for cid, c in classes.items():
        for pid, p in c["data_properties"].items():
            if p.get("semantic_role"):
                continue
            if p.get("identifier") or pid.casefold() in key_fields.get(cid, set()):
                p["semantic_role"] = "key"
    for cid, fkey, agg in measure_hits:
        hit = fields_of.get(cid, {}).get(fkey)
        if not hit:
            continue
        p = classes[cid]["data_properties"][hit[0]]
        if p.get("semantic_role") in (None, "dimension", "measure") and not p.get("identifier"):
            p["semantic_role"] = "measure"
            p.setdefault("aggregation", agg)

    slug = re.sub(r"\W+", "-", name).strip("-").lower() or "ontology"
    meta = studio.get("ontology") if isinstance(studio.get("ontology"), dict) else {}
    iri = meta.get("iri") or base_iri.rstrip("/#") + "/" + slug + "#"
    pfx = meta.get("prefix") or prefix
    model_stash = {k: copy.deepcopy(v) for k, v in doc.items()
                   if k in ("version", "name", "description", "ai_context")}
    if _foreign_exts(doc):
        model_stash["nonportable_extensions"] = _foreign_exts(doc)
    if file_name:
        model_stash["file"] = file_name
    n_dp = sum(len(c["data_properties"]) for c in classes.values())
    onto = {
        "ontology": {
            "name": name, "iri": iri, "prefix": pfx, "version": str(meta.get("version") or "1.0.0"),
            **({"description": doc["description"]} if isinstance(doc.get("description"), str) else {}),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_by": "ossie-import",
            "source": {"type": "ossie", "name": file_name or name, "dialect": "databricks",
                       "ossie_version": doc.get("version")},
            "stats": {"classes": len(classes), "data_properties": n_dp, "object_properties": len(ops),
                      "junction_tables_collapsed": 0},
        },
        "prefixes": {pfx: iri, **PREFIXES, "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"},
        "classes": classes,
        "object_properties": ops,
        "value_meanings": _jsonable(_list(studio.get("value_meanings"))),
        "rules": _jsonable(_list(studio.get("rules"))),
        "metrics": metrics,
        "suggested_relationships": [],
        "lineage": {"tables": [], "columns": []},
        "ossie": model_stash,
    }
    if isinstance(studio.get("layout"), dict):
        onto["layout"] = _jsonable(studio["layout"])
    if warnings:
        onto["warnings"] = warnings
    semantics.classify(onto, add_metrics=False)
    for cid, c in classes.items():              # a role saved by the studio wins over the heuristic
        sx_role = c.pop("_studio_role", None)
        if sx_role:
            c["role"] = sx_role
    return refresh_index(onto)


def _class_from_dataset(ds: dict, cid: str) -> dict:
    sx = _studio(ds)
    src = str(ds.get("source") or cid).strip()
    is_query = bool(re.search(r"\s", src)) or src.startswith("(")
    entry: dict = {"label": label(cid)}
    if isinstance(ds.get("description"), str) and ds["description"]:
        entry["description"] = ds["description"]
    if is_query:
        entry["source_table"] = cid
        entry["source_kind"] = "query"
        entry["source_query"] = src
    else:
        parts = [x.strip('`"[]') for x in src.split(".")]
        entry["source_table"] = parts[-1]
        if len(parts) >= 2:
            entry["source_fqn"] = ".".join(parts)
    syn = _synonyms(ds)
    if syn:
        entry["synonyms"] = syn
    pk = [str(x) for x in _list(ds.get("primary_key"))]
    pk_fold = {x.casefold() for x in pk}
    props: dict = {}
    for f in _dicts(ds.get("fields")):
        if not isinstance(f.get("name"), str) or not f["name"] or f["name"] in props:
            continue
        pid = f["name"]
        fx = _studio(f)
        text, dialect = pick_expression(f.get("expression"))
        col = _bare_column(text)
        dt = f.get("datatype")
        p: dict = {"label": label(pid), "range": DT_TO_XSD.get(dt, "xsd:string")}
        if isinstance(f.get("description"), str) and f["description"]:
            p["description"] = f["description"]
        if pid.casefold() in pk_fold:
            p["identifier"] = True
            p["required"] = True
        p["source_column"] = col or pid
        if not col and text:
            p["expression"] = _to_databricks(text, dialect)
        dim = f.get("dimension") if isinstance(f.get("dimension"), dict) else {}
        is_time = dim.get("is_time") if isinstance(dim.get("is_time"), bool) else dt in TEMPORAL_TYPES
        if is_time:
            p["semantic_role"] = "time"
        syn = _synonyms(f)
        if syn:
            p["synonyms"] = syn
        p["provenance"] = "ossie"
        for k in ("semantic_role", "aggregation", "allowed_values", "samples", "sql_type", "required", "unique",
                  "provenance"):
            if k in fx:
                p[k] = _jsonable(fx[k])
        stash = {k: copy.deepcopy(v) for k, v in f.items() if k not in ("name", "description") + EXT_KEYS}
        if _foreign_exts(f):
            stash["nonportable_extensions"] = _foreign_exts(f)
        p["ossie"] = stash
        props[pid] = p
    entry["source_columns"] = list(dict.fromkeys(p["source_column"] for p in props.values()
                                                 if not p.get("expression")))
    ident = [next((pid for pid in props if pid.casefold() == k.casefold()), k) for k in pk]
    if ident:
        entry["identifier"] = ident
    uks = [[str(x) for x in k] for k in _list(ds.get("unique_keys")) if isinstance(k, list) and k]
    if uks:
        entry["unique_keys"] = uks
    entry["provenance"] = sx.get("provenance", "ossie")
    for k in ("instance_count", "identifier_inferred"):
        if k in sx:
            entry[k] = sx[k]
    if sx.get("role") in ("fact", "dimension", "entity", "bridge"):
        entry["_studio_role"] = sx["role"]
    entry["data_properties"] = props
    stash = {k: copy.deepcopy(v) for k, v in ds.items()
             if k in ("ai_context", "source", "primary_key", "unique_keys")}
    if _foreign_exts(ds):
        stash["nonportable_extensions"] = _foreign_exts(ds)
    entry["ossie"] = stash
    return entry


def _to_databricks(text: str, dialect: str | None) -> str:
    tree = _parse(text, _read(dialect))
    if tree is None:
        return text
    try:
        return tree.sql(dialect="databricks")
    except Exception:  # noqa: BLE001
        return text


def _field_node(hit, table: str | None):
    """Replacement for a reference to a field: its source column, or its expression."""
    pid, col, expr_sql = hit
    if expr_sql:
        tree = _parse(expr_sql, "databricks")
        if tree is None:
            return None
        if table:
            tree = _map_columns(tree, lambda c: None if c.table else _col_node(c.name, table))
        return exp.Paren(this=tree)
    return _col_node(col or pid, table)


def _measure_hits(tree, resolve) -> list[tuple[str, str, str]]:
    """(class, field, aggregation) for aggregates applied directly to one field."""
    hits = []
    for agg in tree.find_all(exp.AggFunc):
        name = type(agg).__name__
        arg = agg.this
        distinct = isinstance(arg, exp.Distinct)
        if distinct:
            arg = arg.expressions[0] if arg.expressions else None
        if not isinstance(arg, exp.Column):
            continue
        target = resolve(arg)
        if not target:
            continue
        if name == "Count" and distinct:
            hits.append((*target, "COUNT_DISTINCT"))
        elif name in AGG_NAMES:
            hits.append((*target, AGG_NAMES[name]))
    return hits


def _dataset_metric_sql(text, dialect, cid, fields_of):
    """Dataset-scoped metric -> SQL over the class's source columns."""
    tree = _parse(text, _read(dialect))
    if tree is None:
        return None, []
    fields = fields_of.get(cid, {})

    def resolve(col):
        parts = [p.name for p in col.parts]
        if len(parts) == 1 and parts[0].casefold() in fields:
            return cid, parts[0].casefold()
        return None

    hits = _measure_hits(tree, resolve)

    def fn(col):
        parts = [p.name for p in col.parts]
        if len(parts) == 2 and parts[0].casefold() == "source":
            return _col_node(parts[1])
        if len(parts) == 1 and parts[0].casefold() in fields:
            return _field_node(fields[parts[0].casefold()], None)
        return None

    try:
        return _map_columns(tree, fn).sql(dialect="databricks"), hits
    except Exception:  # noqa: BLE001
        return None, hits


def _referenced_datasets(text, dialect, cid_of) -> list[str]:
    tree = _parse(text, _read(dialect))
    if tree is None:
        return []
    out = []
    for col in tree.find_all(exp.Column):
        cid = cid_of.get((col.table or "").casefold())
        if cid and cid not in out:
            out.append(cid)
    return out


def _choose_base(refs: list[str], rel_pairs: list[tuple[str, str]]) -> str | None:
    """The class a model-scoped metric hangs off: the referenced dataset on the many side."""
    if not refs:
        return None
    if len(refs) == 1:
        return refs[0]
    def score(c):
        return (sum(1 for d, r in rel_pairs if d == c and r in refs), sum(1 for d, _ in rel_pairs if d == c))
    return max(refs, key=lambda c: (score(c), -refs.index(c)))


def _model_metric_sql(text, dialect, base, cid_of, fields_of, table_of):
    """Model-scoped metric (dataset.field references) -> SQL over source tables/columns;
    the base class's columns stay unqualified."""
    tree = _parse(text, _read(dialect))
    if tree is None:
        return None, []

    def resolve(col):
        cid = cid_of.get((col.table or "").casefold())
        if cid and col.name.casefold() in fields_of.get(cid, {}):
            return cid, col.name.casefold()
        return None

    hits = _measure_hits(tree, resolve)

    def fn(col):
        target = resolve(col)
        if not target:
            return None
        cid, fkey = target
        return _field_node(fields_of[cid][fkey], None if cid == base else table_of(cid))

    try:
        return _map_columns(tree, fn).sql(dialect="databricks"), hits
    except Exception:  # noqa: BLE001
        return None, hits


# --------------------------------------------------------------------------- #
# ontology -> Ossie
# --------------------------------------------------------------------------- #
def from_ontology(onto: dict, include_studio: bool = True, include_layout: bool = False) -> tuple[dict, list[str]]:
    """Build an Ossie 0.3.0.dev0 document from a studio ontology. Returns (document, notes)."""
    notes: list[str] = []
    meta = onto.get("ontology") or {}
    mstash = onto.get("ossie") or {}
    classes = onto.get("classes") or {}
    ops = onto.get("object_properties") or {}
    src_dialect = (meta.get("source") or {}).get("dialect")
    read = {"databricks": "databricks", "sqlite": "sqlite", "mysql": "mysql", "postgres": "postgres",
            "tsql": "tsql", "oracle": "oracle", "snowflake": "snowflake", "bigquery": "bigquery",
            "redshift": "redshift", "duckdb": "duckdb", "teradata": "teradata"}.get(src_dialect)

    # ---- fields per class ------------------------------------------------------
    fields: dict[str, dict] = {}        # cid -> {field name: field dict}
    by_column: dict[str, dict] = {}     # cid -> {column casefold: field name}
    vm_index = {(v.get("class"), v.get("property")): v for v in _dicts(onto.get("value_meanings"))}
    for cid, c in classes.items():
        fields[cid], by_column[cid] = {}, {}
        for pid, p in (c.get("data_properties") or {}).items():
            fields[cid][pid] = _field_from_property(pid, p, vm_index.get((cid, pid)), include_studio)
            if not p.get("expression"):
                by_column[cid].setdefault(str(p.get("source_column") or pid).casefold(), pid)
    added_fields = 0

    def field_for(cid: str, column: str) -> str:
        nonlocal added_fields
        key = column.casefold()
        if key in by_column[cid]:
            return by_column[cid][key]
        if any(n.casefold() == key for n in fields[cid]):          # a property already uses the name
            return next(n for n in fields[cid] if n.casefold() == key)
        name = column
        f = {"name": name, "expression": {"sql": quote_ident(column)},
             "description": "Join key (declared so relationships can name it)"}
        if include_studio:
            f["nonportable_extensions"] = [{"vendor_name": STUDIO_VENDOR,
                                            "data": {"semantic_role": "key", "added_for": "relationship"}}]
        fields[cid][name] = f
        by_column[cid][key] = name
        added_fields += 1
        return name

    def table_class(table: str) -> str | None:
        t = table.casefold()
        for cid, c in classes.items():
            if str(c.get("source_table", "")).casefold() == t or cid.casefold() == t:
                return cid
        return None

    # ---- relationships -----------------------------------------------------------
    relationships, extra_datasets, seen = [], [], set()
    for pid, p in ops.items():
        j = p.get("join")
        if not j or pid in seen or p.get("cardinality") == "one-to-many":
            continue
        seen.update({pid, p.get("inverse")})
        dom, rng = p.get("domain"), p.get("range")
        if dom not in classes or rng not in classes:
            notes.append(f"Skipped relationship '{pid}': an end is not a class.")
            continue
        fcols = [str(x).rpartition(".")[2] for x in _list(j.get("from"))]
        tcols = [str(x).rpartition(".")[2] for x in _list(j.get("to"))]
        if p.get("cardinality") == "many-to-many" and j.get("via"):
            rels, ds = _junction(pid, p, fcols, tcols, classes, field_for, include_studio, notes)
            relationships += rels
            if ds and ds["name"] not in {d["name"] for d in extra_datasets}:
                extra_datasets.append(ds)
            continue
        if not fcols or len(fcols) != len(tcols):
            notes.append(f"Skipped relationship '{pid}': its join columns do not pair up.")
            continue
        rel = {"name": pid, "from": dom, "to": rng,
               "from_columns": [field_for(dom, c) for c in fcols],
               "to_columns": [field_for(rng, c) for c in tcols]}
        st = p.get("ossie") or {}
        if st.get("ai_context"):
            rel["ai_context"] = copy.deepcopy(st["ai_context"])
        elif p.get("description"):
            rel["ai_context"] = {"instructions": p["description"]}
        exts = copy.deepcopy(_list(st.get("nonportable_extensions")))
        if include_studio:
            sx = {"cardinality": p.get("cardinality"), "inverse": p.get("inverse"),
                  "provenance": p.get("provenance")}
            if p.get("confidence") is not None:
                sx["confidence"] = p["confidence"]
            if p.get("required"):
                sx["required"] = True
            exts.append({"vendor_name": STUDIO_VENDOR, "data": _jsonable({k: v for k, v in sx.items() if v is not None})})
        if exts:
            rel["nonportable_extensions"] = exts
        relationships.append(rel)

    # ---- metrics -----------------------------------------------------------------
    ds_metrics: dict[str, list] = {cid: [] for cid in classes}
    model_metrics = []
    for m in _dicts(onto.get("metrics")):
        out, scope, owner = _metric_to_ossie(m, classes, field_for, table_class, read, notes)
        if out is None:
            continue
        if include_studio:
            sx = {"base_class": m.get("base_class"), "provenance": m.get("provenance")}
            out.setdefault("nonportable_extensions", []).append(
                {"vendor_name": STUDIO_VENDOR, "data": _jsonable({k: v for k, v in sx.items() if v})})
        if scope == "dataset":
            if any(n.casefold() == str(out["name"]).casefold() for n in fields[owner]) or \
                    any(x["name"] == out["name"] for x in ds_metrics[owner]):
                notes.append(f"Metric '{out['name']}' renamed to '{out['name']}_metric' (it clashed with a field "
                             f"of {owner}; fields and dataset metrics share one namespace).")
                out["name"] = f"{out['name']}_metric"
            ds_metrics[owner].append(out)
        else:
            if any(x["name"] == out["name"] for x in model_metrics):
                out["name"] = f"{out['name']}_2"
            model_metrics.append(out)

    # ---- datasets ----------------------------------------------------------------
    datasets = []
    for cid, c in classes.items():
        st = c.get("ossie") or {}
        ds = {"name": cid,
              "source": c.get("source_query") or st.get("source") or c.get("source_fqn") or c.get("source_table") or cid}
        ident = [x if x in fields[cid] else field_for(cid, x) for x in _list(c.get("identifier"))]
        if ident:
            ds["primary_key"] = ident
        uks = [[x if x in fields[cid] else field_for(cid, x) for x in k] for k in _list(c.get("unique_keys")) if k]
        if uks:
            ds["unique_keys"] = uks
        if c.get("description"):
            ds["description"] = c["description"]
        ai = _merge_ai(st.get("ai_context"), c.get("synonyms"))
        if ai:
            ds["ai_context"] = ai
        datasets.append(ds)
    for ds, (cid, c) in zip(datasets, classes.items()):
        if fields[cid]:
            ds["fields"] = list(fields[cid].values())
        if ds_metrics[cid]:
            ds["metrics"] = ds_metrics[cid]
        exts = copy.deepcopy(_list((c.get("ossie") or {}).get("nonportable_extensions")))
        if include_studio:
            sx = {k: c.get(k) for k in ("role", "instance_count", "provenance", "identifier_inferred", "source_kind")
                  if c.get(k) is not None}
            if sx:
                exts.append({"vendor_name": STUDIO_VENDOR, "data": _jsonable(sx)})
        if exts:
            ds["nonportable_extensions"] = exts
    datasets += extra_datasets

    doc: dict = {"version": CURRENT_VERSION, "name": str(mstash.get("name") or meta.get("name") or "ontology")}
    desc = meta.get("description") or mstash.get("description")
    if desc:
        doc["description"] = desc
    if mstash.get("ai_context"):
        doc["ai_context"] = copy.deepcopy(mstash["ai_context"])
    doc["datasets"] = datasets
    if relationships:
        doc["relationships"] = relationships
    if model_metrics:
        doc["metrics"] = model_metrics
    exts = copy.deepcopy(_list(mstash.get("nonportable_extensions")))
    if include_studio:
        sx = {"ontology": {k: meta.get(k) for k in ("iri", "prefix", "version", "generated_by") if meta.get(k)}}
        if onto.get("value_meanings"):
            sx["value_meanings"] = onto["value_meanings"]
        if onto.get("rules"):
            sx["rules"] = onto["rules"]
        if include_layout and onto.get("layout"):
            sx["layout"] = onto["layout"]
        exts.append({"vendor_name": STUDIO_VENDOR, "data": _jsonable(sx)})
    if exts:
        doc["nonportable_extensions"] = exts
    if added_fields:
        notes.append(f"Declared {added_fields} join-key field(s): Ossie keys and relationships must name declared "
                     "fields.")
    pending = [s for s in _dicts(onto.get("suggested_relationships")) if s.get("status") == "proposed"]
    if pending:
        notes.append(f"{len(pending)} suggested relationship(s) still waiting for review were not exported.")
    if onto.get("lineage", {}).get("tables"):
        notes.append("Lineage is not part of the Ossie core model and was not exported.")
    return doc, notes


def _merge_ai(stash_ai, synonyms) -> dict | str | None:
    syn = [s for s in _list(synonyms) if isinstance(s, str) and s.strip()]
    if isinstance(stash_ai, str):
        return {"instructions": stash_ai, "synonyms": syn} if syn else stash_ai
    ai = copy.deepcopy(stash_ai) if isinstance(stash_ai, dict) else {}
    if syn:
        ai["synonyms"] = list(dict.fromkeys(syn))
    elif "synonyms" in ai and synonyms is not None:
        ai.pop("synonyms")
    return ai or None


def _field_from_property(pid: str, p: dict, vm: dict | None, include_studio: bool) -> dict:
    st = p.get("ossie") or {}
    f: dict = {"name": pid}
    if isinstance(st.get("expression"), dict):
        f["expression"] = copy.deepcopy(st["expression"])
    elif p.get("expression"):
        f["expression"] = {"sql": _portable(p["expression"], "databricks")}
    else:
        f["expression"] = {"sql": quote_ident(str(p.get("source_column") or pid))}
    if "ossie" in p and "datatype" not in st and p.get("range") == "xsd:string":
        dt = None                      # imported without a datatype: do not invent one
    elif DT_TO_XSD.get(st.get("datatype")) == p.get("range"):
        dt = st.get("datatype")
    else:
        dt = XSD_TO_DT.get(p.get("range"))
    if dt:
        f["datatype"] = dt
    is_time = p.get("semantic_role") == "time"
    explicit = (st.get("dimension") or {}).get("is_time") if isinstance(st.get("dimension"), dict) else None
    if is_time != (dt in TEMPORAL_TYPES) or explicit is not None:
        f["dimension"] = {"is_time": is_time}
    if st.get("label"):
        f["label"] = st["label"]
    if p.get("description"):
        f["description"] = p["description"]
    ai = _merge_ai(st.get("ai_context"), p.get("synonyms"))
    meanings = {k: v for k, v in ((vm or {}).get("values") or {}).items() if v}
    if meanings:
        ai = ai if isinstance(ai, dict) else ({"instructions": ai} if ai else {})
        codes = "; ".join(f"{k} = {v}" for k, v in meanings.items())
        extra = f"Coded values: {codes}."
        ai["instructions"] = (ai.get("instructions", "") + " " + extra).strip() \
            if extra not in ai.get("instructions", "") else ai["instructions"]
    if ai:
        f["ai_context"] = ai
    exts = copy.deepcopy(_list(st.get("nonportable_extensions")))
    if include_studio:
        sx = {k: p.get(k) for k in ("semantic_role", "aggregation", "allowed_values", "sql_type", "required", "unique",
                                    "provenance") if p.get(k) not in (None, [], "")}
        if sx:
            exts.append({"vendor_name": STUDIO_VENDOR, "data": _jsonable(sx)})
    if exts:
        f["nonportable_extensions"] = exts
    return f


def _portable(sql: str, read: str | None) -> str:
    """SQL rewritten in sqlglot's ANSI-style default dialect (Ossie's portable form)."""
    tree = _parse(sql, read)
    if tree is None:
        return sql
    try:
        return tree.sql()
    except Exception:  # noqa: BLE001
        return sql


def _junction(pid, p, fcols, tcols, classes, field_for, include_studio, notes):
    """A many-to-many relationship -> its junction dataset + two many-to-one relationships."""
    via = str(p["join"]["via"])
    dom, rng = p["domain"], p["range"]
    name = via if via not in classes else f"{via}_link"
    fq = classes[dom].get("source_fqn")
    source = ".".join(fq.split(".")[:-1] + [via]) if fq and "." in fq else via
    jfields = [{"name": c, "expression": {"sql": quote_ident(c)}} for c in dict.fromkeys(fcols + tcols)]
    ds = {"name": name, "source": source, "description": f"Junction table behind {pid} ({dom} ↔ {rng})",
          "fields": jfields}
    if include_studio:
        ds["nonportable_extensions"] = [{"vendor_name": STUDIO_VENDOR, "data": {"role": "bridge"}}]
    rels = []
    for cols, target in ((fcols, dom), (tcols, rng)):
        key = _list(classes[target].get("identifier"))
        if not key or len(key) != len(cols):
            notes.append(f"Junction '{via}': {target} has no key matching {cols}; that half of {pid} was not exported.")
            continue
        key_fields = [field_for(target, _key_column(classes[target], k)) for k in key]
        rels.append({"name": f"{name}_to_{target}", "from": name, "to": target,
                     "from_columns": list(cols), "to_columns": key_fields})
    return rels, ds


def _key_column(c: dict, pid: str) -> str:
    p = (c.get("data_properties") or {}).get(pid) or {}
    return str(p.get("source_column") or pid)


def _qualifiers_ok(expression: dict, classes: dict) -> bool:
    """False when an imported model metric names a dataset that no longer exists (renamed class)."""
    names = {c.casefold() for c in classes}
    for text in [expression.get("sql")] + [d.get("expression") for d in _dicts(expression.get("dialects"))]:
        tree = _parse(text, None) if isinstance(text, str) else None
        if tree is None:
            continue
        for col in tree.find_all(exp.Column):
            if col.table and col.table.casefold() not in names and col.table.casefold() != "source":
                return False
    return True


def _metric_to_ossie(m, classes, field_for, table_class, read, notes):
    st = m.get("ossie") or {}
    base = m.get("base_class") if m.get("base_class") in classes else None
    sql = m.get("sql") or ""
    out: dict = {"name": str(st.get("name") or m.get("name"))}
    scope, owner = None, None
    if st.get("expression") and st.get("sql") == sql and _qualifiers_ok(st["expression"], classes):
        # untouched since import: keep the original expression (every dialect variant)
        out["expression"] = copy.deepcopy(st["expression"])
        if st.get("scope") == "dataset" and st.get("dataset"):
            owner = next((cid for cid in classes if cid.casefold() == str(st["dataset"]).casefold()), base)
            scope = "dataset" if owner else "model"
        else:
            scope = "model"
    else:
        out["name"] = str(m.get("name"))
        tree = _parse(sql, read)
        if tree is None:
            notes.append(f"Metric '{m.get('name')}' was skipped: its SQL does not parse.")
            return None, None, None
        refs = []

        def target(col):
            if col.table:
                cid = table_class(col.table)
                return cid
            return base

        for col in tree.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue
            cid = target(col)
            if cid is None:
                notes.append(f"Metric '{m.get('name')}' was skipped: '{col.sql()}' is not a column of a class.")
                return None, None, None
            refs.append(cid)
        others = {c for c in refs if c != base}
        if base and not others:
            scope, owner = "dataset", base
            new = _map_columns(tree, lambda c: _col_node(field_for(base, c.name)))
        else:
            scope = "model"
            new = _map_columns(tree, lambda c: _col_node(field_for(target(c), c.name), target(c)))
            if not refs:
                notes.append(f"Metric '{m.get('name')}' has no column references and no base class; exported as a "
                             "model-level metric.")
        out["expression"] = {"sql": new.sql()}
        agg = next(iter(new.find_all(exp.AggFunc)), None)
        if isinstance(agg, exp.Count):
            out["datatype"] = "Integer"
    if m.get("description"):
        out["description"] = m["description"]
    for k in ("datatype",):
        if st.get(k) and k not in out:
            out[k] = st[k]
    ai = _merge_ai(st.get("ai_context"), m.get("synonyms"))
    if ai:
        out["ai_context"] = ai
    exts = copy.deepcopy(_list(st.get("nonportable_extensions")))
    if exts:
        out["nonportable_extensions"] = exts
    order = ("name", "expression", "description", "datatype", "ai_context", "nonportable_extensions")
    out = {k: out[k] for k in order if k in out}
    return out, scope, owner
