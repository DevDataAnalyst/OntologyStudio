"""
Turn a `DatabaseSchema` into an ontology dict and serialise it to YAML.

Mapping rules (relational -> ontology):
  table                       -> class
  column                      -> data property (range = XSD datatype)
  primary key                 -> class identifier
  NOT NULL / UNIQUE           -> required / unique (min/max cardinality)
  foreign key                 -> object property (many-to-one, or one-to-one if FK is unique)
                                 + inverse object property (one-to-many)
  pure junction table         -> many-to-many object property (no class)
  low-cardinality text column -> enumeration (allowed_values), when data is available
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from .schema_model import DatabaseSchema, ForeignKey, Table

XSD = "http://www.w3.org/2001/XMLSchema#"
PREFIXES = {
    "xsd": XSD,
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
}


@dataclass
class BuildOptions:
    ontology_name: str = "MyOntology"
    base_iri: str = "http://example.org/ontology/"
    prefix: str = "ex"
    version: str = "1.0.0"
    naming: str = "semantic"            # semantic (Customer / firstName) | raw (keep SQL names)
    singularize: bool = True
    collapse_junction_tables: bool = True
    include_fk_columns: bool = False    # also emit FK columns as data properties
    include_views: bool = False
    infer_relationships: bool = False   # guess FKs from <table>_id column names
    enum_detection: bool = True
    enum_max: int = 12
    include_sql_metadata: bool = True   # source_table / source_column / sql_type
    include_samples: bool = True        # up to 5 example values per column (helps LLMs / Text-to-SQL)
    include_provenance: bool = True     # where each item came from: schema | profile | inferred | llm | docs/<file>
    strip_prefixes: bool = True         # dim_customer -> Customer, fact_sales -> Sale (semantic naming only)


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #
def _words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]


def singular(word: str) -> str:
    lw = word.lower()
    if lw.endswith(("ss", "us", "is", "data", "status", "news")):
        return word
    if lw.endswith("ies") and len(word) > 4:
        return word[:-3] + ("Y" if word[-1].isupper() else "y")
    if lw.endswith(("ches", "shes", "xes", "zes", "sses")):
        return word[:-2]
    if lw.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def plural(word: str) -> str:
    lw = word.lower()
    if lw.endswith("y") and len(word) > 1 and lw[-2] not in "aeiou":
        return word[:-1] + "ies"
    if lw.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:].lower() if not w.isupper() or len(w) > 3 else w
                   for w in _words(name)) or name


def camel(name: str) -> str:
    p = pascal(name)
    return p[:1].lower() + p[1:]


def label(name: str) -> str:
    return " ".join(w.lower() for w in _words(name))


# --------------------------------------------------------------------------- #
# Datatype mapping
# --------------------------------------------------------------------------- #
_TYPE_RULES = [
    (r"\bBOOL", "xsd:boolean"),
    (r"\bBIT\b", "xsd:boolean"),
    (r"TINYINT\(1\)", "xsd:boolean"),
    (r"BIGINT|INT8|\bLONG\b", "xsd:long"),
    (r"INT|SERIAL", "xsd:integer"),
    (r"DEC|NUMERIC|NUMBER|MONEY", "xsd:decimal"),
    (r"FLOAT|DOUBLE|REAL", "xsd:double"),
    (r"TIMESTAMP|DATETIME", "xsd:dateTime"),
    (r"^DATE\b|\bDATE$", "xsd:date"),
    (r"^TIME\b|\bTIME$", "xsd:time"),
    (r"INTERVAL", "xsd:duration"),
    (r"BLOB|BINARY|BYTEA|IMAGE|VARBINARY", "xsd:base64Binary"),
    (r"UUID|UNIQUEIDENTIFIER", "xsd:string"),
    (r"JSON|ARRAY|MAP|STRUCT|VARIANT", "rdf:JSON"),
]


def xsd_type(sql_type: str) -> str:
    t = (sql_type or "").upper().strip()
    for pattern, xsd in _TYPE_RULES:
        if re.search(pattern, t):
            return xsd
    return "xsd:string"


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def _abbreviates(short: str, full: str) -> bool:
    """True if `short` is an in-order abbreviation of `full` (dept -> Department)."""
    s, f = short.lower().replace("_", ""), full.lower().replace("_", "")
    if not s or not f or s[0] != f[0]:
        return False
    it = iter(f)
    return all(ch in it for ch in s)


def _is_junction(t: Table) -> bool:
    if len(t.foreign_keys) != 2:
        return False
    fk_cols = {c.lower() for fk in t.foreign_keys for c in fk.columns}
    others = [c for c in t.columns if c.name.lower() not in fk_cols]
    # pure junction: nothing but the two FKs (optionally a surrogate PK)
    return not others or (len(others) == 1 and others[0].primary_key)


def _infer_fks(schema: DatabaseSchema) -> int:
    """Guess FKs from naming (<entity>_id / <entity>Id -> table of that entity)."""
    lookup: dict[str, Table] = {}
    for t in schema.tables:
        for key in {t.name.lower(), singular(t.name).lower()}:
            lookup[key] = t
    added = 0
    for t in schema.tables:
        existing = {c.lower() for fk in t.foreign_keys for c in fk.columns}
        for c in t.columns:
            m = re.match(r"^(.*?)[_]?id$", c.name, flags=re.I)
            if not m or not m.group(1) or c.name.lower() in existing:
                continue
            target = lookup.get(m.group(1).rstrip("_").lower())
            if target is None or target is t and c.primary_key:
                continue
            if len(target.primary_key) == 1:
                t.foreign_keys.append(ForeignKey([c.name], target.name, list(target.primary_key),
                                                 name="inferred"))
                added += 1
    return added


def build_ontology(schema: DatabaseSchema, opts: BuildOptions) -> dict:
    return refresh_index(_build(schema, opts))


def _build(schema: DatabaseSchema, opts: BuildOptions) -> dict:
    semantic = opts.naming == "semantic"
    warnings = list(schema.warnings)
    if opts.infer_relationships:
        n = _infer_fks(schema)
        if n:
            warnings.append(f"{n} relationship(s) inferred from column names — review them.")

    tables = [t for t in schema.tables if opts.include_views or not t.is_view]

    def cname(t: Table) -> str:
        if not semantic:
            return t.name
        base = t.name
        if opts.strip_prefixes:
            stripped = re.sub(r"^(dim|fact|fct|tbl|stg|vw|lkp|ref|d|f|t|v)_", "", base, flags=re.I)
            stripped = re.sub(r"_(dim|fact|fct|tbl|lkp|lookup)$", "", stripped, flags=re.I)
            base = stripped or base
        return pascal(singular(base) if opts.singularize else base)

    def pname(col: str) -> str:
        return camel(col) if semantic else col

    class_names: dict[str, str] = {}
    taken: set[str] = set()
    for t in tables:                      # stripping prefixes can collide (dim_x / fact_x)
        nm = cname(t)
        if nm in taken:
            nm = pascal(t.name) if semantic else t.name
        while nm in taken:
            nm += "_"
        taken.add(nm)
        class_names[t.name.lower()] = nm
    junctions = {t.name.lower() for t in tables if opts.collapse_junction_tables and _is_junction(t)}

    classes: dict = {}
    object_props: dict = {}
    used_prop_ids: set[str] = set()

    def unique_id(candidate: str, domain: str) -> str:
        if candidate in used_prop_ids:
            candidate = camel(domain) + candidate[:1].upper() + candidate[1:]
        base, i = candidate, 2
        while candidate in used_prop_ids:
            candidate, i = f"{base}{i}", i + 1
        used_prop_ids.add(candidate)
        return candidate

    # ---- classes + data properties ---------------------------------------
    for t in tables:
        if t.name.lower() in junctions:
            continue
        cls = class_names[t.name.lower()]
        fk_cols = {c.lower() for fk in t.foreign_keys for c in fk.columns}
        data_props: dict = {}
        for c in t.columns:
            if c.name.lower() in fk_cols and not opts.include_fk_columns and not c.primary_key:
                continue
            p: dict = {"label": label(c.name), "range": xsd_type(c.sql_type)}
            if c.comment:
                p["description"] = c.comment
            p["required"] = not c.nullable
            if c.unique:
                p["unique"] = True
            if c.primary_key:
                p["identifier"] = True
            p["cardinality"] = {"min": 0 if c.nullable else 1, "max": 1}
            if c.default is not None:
                d = str(c.default).strip()
                p["default"] = d[1:-1] if len(d) > 1 and d[0] == d[-1] == "'" else d
            enum_decl = re.match(r"^\s*(ENUM|SET)\s*\((.*)\)\s*$", c.sql_type or "", flags=re.I | re.S)
            prov = "schema"
            if enum_decl:
                p["allowed_values"] = re.findall(r"'((?:[^']|'')*)'", enum_decl.group(2))
            if (opts.enum_detection and c.distinct_values and not c.primary_key
                    and not c.unique and p["range"] in ("xsd:string",)
                    and "allowed_values" not in p
                    and len(c.distinct_values) <= opts.enum_max
                    and (t.row_count or 0) >= 2 * len(c.distinct_values)):
                p["allowed_values"] = c.distinct_values
                prov = "schema+profile"
            if opts.include_samples and c.samples and "allowed_values" not in p and not c.primary_key:
                p["samples"] = c.samples
            if opts.include_provenance:
                p["provenance"] = "schema+comment" if c.comment and prov == "schema" else prov
            if opts.include_sql_metadata:
                p["source_column"] = c.name
                p["sql_type"] = c.sql_type or None
            data_props[pname(c.name)] = p

        entry: dict = {"label": label(cls)}
        if t.comment:
            entry["description"] = t.comment
        if opts.include_sql_metadata:
            entry["source_table"] = t.name
            if t.full_name:
                entry["source_fqn"] = t.full_name
            if t.is_view:
                entry["source_kind"] = "view"
            entry["source_columns"] = [c.name for c in t.columns]
        if t.primary_key:
            entry["identifier"] = [pname(c) for c in t.primary_key]
        keys = [[pname(c) for c in us] for us in t.unique_sets if len(us) > 1]
        if keys:
            entry["unique_keys"] = keys
        if t.row_count is not None:
            entry["instance_count"] = t.row_count
        if opts.include_provenance:
            entry["provenance"] = "schema+comment" if t.comment else "schema"
        if getattr(t, "pk_inferred", False):
            entry["identifier_inferred"] = True
        meta = {k: getattr(t, k) for k in ("table_type", "owner", "created", "last_altered", "storage_format",
                                           "size_bytes") if getattr(t, k, None) not in (None, "")}
        if meta:
            entry["table_meta"] = meta
        prof = {}
        fk_target = {c.lower(): f"{fk.ref_table}.{(fk.ref_columns or [''])[i] if i < len(fk.ref_columns or []) else ''}"
                     for fk in t.foreign_keys for i, c in enumerate(fk.columns)}
        for c in t.columns:                 # every column, FK columns included: the data dictionary needs them
            d = {"sql_type": c.sql_type or None, "nullable": c.nullable,
                 "primary_key": c.primary_key or None, "foreign_key": fk_target.get(c.name.lower()),
                 "distinct": c.distinct_count, "non_null": c.non_null_count,
                 "min": c.min_value, "max": c.max_value, "comment": c.comment}
            prof[c.name] = {k: v for k, v in d.items() if v is not None}
        entry["column_profile"] = prof
        entry["data_properties"] = data_props
        classes[cls] = entry

    # ---- object properties from FKs ---------------------------------------
    for t in tables:
        if t.name.lower() in junctions:
            continue
        domain = class_names[t.name.lower()]
        for fk in t.foreign_keys:
            target_t = schema.table(fk.ref_table)
            if target_t is None or target_t.name.lower() not in class_names:
                continue
            rng = class_names[target_t.name.lower()]
            cols = [t.column(c) for c in fk.columns]
            required = all(c is not None and not c.nullable for c in cols)
            one_to_one = (
                (len(fk.columns) == 1 and cols[0] is not None and cols[0].unique)
                or sorted(x.lower() for x in fk.columns) == sorted(x.lower() for x in t.primary_key)
            )
            # forward name: customer_id -> customer ; manager_emp_id -> managerEmp
            stem = re.sub(r"[_]?(id|key|fk|code|no)$", "", fk.columns[0], flags=re.I) \
                if len(fk.columns) == 1 else rng
            stem = stem or rng
            if _abbreviates(stem, rng) or _abbreviates(stem, target_t.name):
                stem = rng  # emp_id -> Employee, dept_id -> Department
            verbish = re.search(r"(by|to|from|of|for|with)$", stem, flags=re.I)
            fwd = unique_id((camel(stem) if verbish else "has" + pascal(stem))
                            if semantic else f"{t.name}_{stem}", domain)
            inv_name = ("has" + pascal(plural(domain)) if not one_to_one else "has" + domain) \
                if semantic else f"{target_t.name}_{t.name}"
            if domain == rng:  # self-reference: referred_by -> referredCustomers, manager_id -> managerOf
                if semantic and re.search(r"_?by$", stem, flags=re.I):
                    inv_name = camel(re.sub(r"_?by$", "", stem, flags=re.I)) + pascal(plural(domain))
                else:
                    inv_name = f"{camel(stem)}Of" if semantic else f"{stem}_of"
            inv = unique_id(inv_name, rng)

            fwd_prop = {
                "label": label(fwd),
                "domain": domain,
                "range": rng,
                "cardinality": "one-to-one" if one_to_one else "many-to-one",
                "functional": True,
                "required": required,
                "inverse": inv,
                "join": {
                    "from": [f"{t.name}.{c}" for c in fk.columns],
                    "to": [f"{target_t.name}.{c}" for c in (fk.ref_columns or target_t.primary_key)],
                },
            }
            if fk.name == "inferred":
                fwd_prop["inferred"] = True
            if fk.name and fk.name.startswith("discovered:"):
                fwd_prop["confidence"] = float(fk.name.split(":")[1])
            if opts.include_provenance:
                fwd_prop["provenance"] = ("inferred" if fk.name == "inferred" else
                                          "discovered" if (fk.name or "").startswith("discovered")
                                          else "schema")
            object_props[fwd] = fwd_prop
            object_props[inv] = {
                "label": label(inv),
                "domain": rng,
                "range": domain,
                "cardinality": "one-to-one" if one_to_one else "one-to-many",
                "functional": one_to_one,
                "inverse": fwd,
            }

    # ---- many-to-many from junction tables --------------------------------
    for t in tables:
        if t.name.lower() not in junctions:
            continue
        a, b = t.foreign_keys
        ta, tb = schema.table(a.ref_table), schema.table(b.ref_table)
        if not ta or not tb or ta.name.lower() not in class_names or tb.name.lower() not in class_names:
            continue
        ca, cb = class_names[ta.name.lower()], class_names[tb.name.lower()]
        fwd = unique_id("has" + pascal(plural(cb)) if semantic else f"{ta.name}_{tb.name}", ca)
        inv = unique_id("has" + pascal(plural(ca)) if semantic else f"{tb.name}_{ta.name}", cb)
        join = {"via": t.name,
                "from": [f"{t.name}.{c}" for c in a.columns],
                "to": [f"{t.name}.{c}" for c in b.columns]}
        object_props[fwd] = {"label": label(fwd), "domain": ca, "range": cb,
                             "cardinality": "many-to-many", "functional": False,
                             "inverse": inv, "join": join,
                             **({"provenance": "schema"} if opts.include_provenance else {})}
        object_props[inv] = {"label": label(inv), "domain": cb, "range": ca,
                             "cardinality": "many-to-many", "functional": False,
                             "inverse": fwd,
                             "join": {"via": t.name, "from": join["to"], "to": join["from"]}}

    # coded columns (short, cryptic values) get a value_meanings stub to be filled by docs / LLM / a human
    value_meanings = []
    for cid, c in classes.items():
        for pid, p in c["data_properties"].items():
            vals = p.get("allowed_values") or []
            if vals and all(len(str(v)) <= 3 for v in vals):
                value_meanings.append({
                    "class": cid, "property": pid,
                    "column": f'{c.get("source_table", cid)}.{p.get("source_column", pid)}',
                    "values": {str(v): "" for v in vals},   # "" = meaning not known yet
                    **({"provenance": "profile"} if opts.include_provenance else {}),
                })

    base = opts.base_iri.rstrip("/#") + "/" + re.sub(r"\W+", "-", opts.ontology_name).strip("-").lower() + "#"
    n_dp = sum(len(c["data_properties"]) for c in classes.values())
    return {
        "ontology": {
            "name": opts.ontology_name,
            "iri": base,
            "prefix": opts.prefix,
            "version": opts.version,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generated_by": "sql2ontology",
            "source": {"type": schema.source_type, "name": schema.name,
                       "dialect": schema.dialect},
            "stats": {"classes": len(classes), "data_properties": n_dp,
                      "object_properties": len(object_props),
                      "junction_tables_collapsed": len(junctions)},
        },
        "prefixes": {opts.prefix: base, **PREFIXES,
                     "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"},
        "classes": classes,
        "object_properties": object_props,
        "value_meanings": value_meanings,
        "rules": [],
        "metrics": [],
        "suggested_relationships": [],
        "lineage": {"tables": [], "columns": []},
        **({"warnings": warnings} if warnings else {}),
    } | {"synonyms_index": {}}


def refresh_index(onto: dict) -> dict:
    """Rebuild `synonyms_index`: business term -> Class / Class.property / metric / rule.
    Call after any enrichment so new synonyms are searchable."""
    idx: dict[str, set] = {}

    def add(term, target):
        if term:
            idx.setdefault(str(term).strip().lower(), set()).add(target)

    for cid, c in onto.get("classes", {}).items():
        add(c.get("label"), cid)
        for syn in c.get("synonyms", []):
            add(syn, cid)
        for pid, p in c.get("data_properties", {}).items():
            add(p.get("label"), f"{cid}.{pid}")
            for syn in p.get("synonyms", []):
                add(syn, f"{cid}.{pid}")
    for kind in ("metrics", "rules"):
        for m in onto.get(kind, []):
            add(m.get("name", "").replace("_", " "), f"{kind[:-1]}:{m.get('name')}")
            for syn in m.get("synonyms", []):
                add(syn, f"{kind[:-1]}:{m.get('name')}")
    for pg in onto.get("pages", []):
        if pg.get("status") != "deprecated":
            add(pg.get("title"), f"page:{pg.get('id')}")
            for syn in pg.get("synonyms") or []:
                add(syn, f"page:{pg.get('id')}")
    for vm in onto.get("value_meanings", []):
        for code, meaning in (vm.get("values") or {}).items():
            if meaning:
                add(meaning, f'{vm["class"]}.{vm["property"]}={code}')
    onto["synonyms_index"] = {k: sorted(v) if len(v) > 1 else next(iter(v))
                              for k, v in sorted(idx.items())}
    return onto


# --------------------------------------------------------------------------- #
# YAML + validation
# --------------------------------------------------------------------------- #
class _Dumper(yaml.SafeDumper):
    pass


def _str_repr(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_repr)


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)  # Decimal, datetime, bytes from profiled values


def to_yaml(onto: dict) -> str:
    return yaml.dump(_clean(onto), Dumper=_Dumper, sort_keys=False,
                     allow_unicode=True, width=100, default_flow_style=False)


def validate(onto: dict) -> list[str]:
    """Structural checks on the generated ontology. Returns a list of problems."""
    issues = []
    classes = onto.get("classes", {})
    ops = onto.get("object_properties", {})
    for pid, p in ops.items():
        for side in ("domain", "range"):
            if p.get(side) not in classes:
                issues.append(f"object property '{pid}': {side} '{p.get(side)}' is not a class")
        inv = p.get("inverse")
        if inv and ops.get(inv, {}).get("inverse") != pid:
            issues.append(f"object property '{pid}': inverse '{inv}' does not point back")
    for vm in onto.get("value_meanings", []):
        missing = [k for k, v in (vm.get("values") or {}).items() if not v]
        if missing:
            issues.append(f'value meanings for {vm["class"]}.{vm["property"]} still empty: '
                          f'{", ".join(missing[:6])} — upload docs or fill them in')
    for cid, c in classes.items():
        if not c.get("identifier"):
            issues.append(f"class '{cid}' has no identifier (table has no primary key)")
        for pid in c.get("identifier", []):
            if pid not in c.get("data_properties", {}):
                issues.append(f"class '{cid}': identifier '{pid}' is not a data property")
    return issues


# --------------------------------------------------------------------------- #
# Suggestions + lineage (attached after discovery)
# --------------------------------------------------------------------------- #
def _class_lookup(onto: dict) -> dict[str, str]:
    """Map table short name / full name (lower) -> class id."""
    out = {}
    for cid, c in onto["classes"].items():
        for k in (c.get("source_table"), c.get("source_fqn"), cid):
            if k:
                out[str(k).lower()] = cid
    return out


def attach_suggestions(onto: dict, suggestions: list[dict]) -> dict:
    look = _class_lookup(onto)
    out = []
    for s in suggestions:
        d, r = look.get(s["from_table"].lower()), look.get(s["to_table"].lower())
        if d and r:
            out.append({**s, "domain": d, "range": r})
    onto["suggested_relationships"] = out
    return onto


def attach_lineage(onto: dict, lineage: dict) -> dict:
    """lineage = uc.read_lineage(...) -> class-level edges; tables outside the selection
    are kept by full name and drawn as external nodes."""
    look = _class_lookup(onto)
    tables = []
    for e in lineage.get("tables", []):
        src, tgt = look.get(str(e["src"]).lower(), e["src"]), look.get(str(e["tgt"]).lower(), e["tgt"])
        if src == tgt:
            continue
        tables.append({"source": src, "target": tgt, "events": int(e.get("events") or 0),
                       "last_seen": e.get("last_seen"),
                       "source_external": src not in onto["classes"],
                       "target_external": tgt not in onto["classes"]})
    cols = []
    for e in lineage.get("columns", []):
        src, tgt = look.get(str(e["src"]).lower(), e["src"]), look.get(str(e["tgt"]).lower(), e["tgt"])
        cols.append({"source": f"{src}.{e['src_col']}", "target": f"{tgt}.{e['tgt_col']}",
                     "events": int(e.get("events") or 0)})
    onto["lineage"] = {"tables": tables, "columns": cols}
    return onto
