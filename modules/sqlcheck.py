"""
SQL checker: is a query safe, grounded in the model and plausible on the data, before anyone
trusts its numbers. Used by Ask (its repair loop) and by the Check SQL workbench.

Adapted from auto-ontology-mcp (github.com/gogoymh/auto-ontology-mcp, Apache-2.0: sqlcheck.py,
validation.py, structure.py), which ports NVIDIA Auto Ontology's text-to-SQL checks (Apache-2.0).
See NOTICE. Rewritten for the studio's ontology dict (Apache Ossie) and Unity Catalog's
catalog.schema.table names.

validate(sql, onto, dialect, run=None, question=None)
    -> {"valid", "errors", "warnings", "notes", "grounding", "probes"}
  static : one read-only SELECT over the model's tables; unknown tables and columns (did you mean);
           joins the model does not know, has rejected or only proposes; fan-out under SUM/AVG/COUNT;
           a many-side column next to a GROUP BY; GROUP BY a primary key; integer division;
           placeholder queries
  probes : with run(sql) -> DataFrame: near-miss text values, percent vs fraction thresholds, value
           containment of unknown joins, a dry run
lookup_values(...)  real values stored in a column
run_capped(...)     run one query with a row cap
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Callable

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

SETOP = getattr(exp, "SetOperation", exp.Union)
WRITES = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
                                         "AlterTable", "Command", "TruncateTable", "Grant", "Copy", "LoadData")
               if hasattr(exp, n))
# functions that reach outside the query (files, HTTP, models) or stall the engine
BLOCKED_FUNCS = {"read_files", "http_request", "ai_query", "remote_query", "read_kafka", "read_kinesis",
                 "read_pubsub", "read_pulsar", "read_statestore", "sleep", "pg_sleep", "load_extension",
                 "readfile", "writefile", "dblink", "lo_import", "lo_export"}
TRUNCATING = {"sqlite", "postgres", "redshift", "tsql"}          # integer / integer drops the fraction
PERCENT_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", re.I)
CLOSE = 0.6
LOW_CARD = 200


def glot(dialect: str | None) -> str | None:
    return None if dialect in (None, "", "auto") else dialect


def _coarse(sql_type: str | None, xsd: str | None = None) -> str:
    t = (sql_type or "").lower()
    if not t:
        t = {"xsd:integer": "int", "xsd:long": "int", "xsd:decimal": "decimal", "xsd:double": "double",
             "xsd:boolean": "bool", "xsd:dateTime": "timestamp", "xsd:date": "date"}.get(xsd or "", "")
    if not t:
        return ""
    if any(k in t for k in ("int", "serial")):
        return "BIGINT"
    if any(k in t for k in ("real", "float", "double", "numeric", "decimal", "money", "number")):
        return "DOUBLE"
    if "bool" in t:
        return "BOOLEAN"
    if "timestamp" in t or "datetime" in t:
        return "TIMESTAMP"
    if t.startswith("date"):
        return "DATE"
    return "TEXT"


def _family(t: str) -> str:
    return "number" if t in ("BIGINT", "DOUBLE") else t


# --------------------------------------------------------------------------- #
# The model as a physical catalog
# --------------------------------------------------------------------------- #
def catalog(onto: dict) -> dict:
    """Tables, columns, keys and joins of the model, keyed the way queries name them.

    tables[key] = {key, name, fqn, cid, cols: {lower: col}, pk: [lower]}; key = lower(catalog.schema.table)
    when every table has a three-part source (Unity Catalog), else lower(table name).
    rels[frozenset({(key, col), (key, col)})] = {pid, one, many} for every modeled join pair."""
    raw = []
    classes = onto.get("classes", {})
    for cid, c in classes.items():
        if c.get("source_kind") == "query":
            continue
        fqn = c.get("source_fqn") if str(c.get("source_fqn") or "").count(".") == 2 else None
        cols = {}
        for pid, p in (c.get("data_properties") or {}).items():
            if p.get("expression"):
                continue
            name = str(p.get("source_column") or pid)
            cols[name.lower()] = {"name": name, "type": _coarse(p.get("sql_type"), p.get("range")), "pid": pid,
                                  "unique": bool(p.get("unique") or p.get("identifier")),
                                  "values": p.get("allowed_values"), "samples": p.get("samples") or []}
        for name in c.get("source_columns") or []:
            cols.setdefault(str(name).lower(), {"name": str(name), "type": "", "pid": None, "unique": False})
        pk = []
        for pid in c.get("identifier") or []:
            col = str(((c.get("data_properties") or {}).get(pid) or {}).get("source_column") or pid).lower()
            pk.append(col)
            if col in cols and len(c.get("identifier") or []) == 1:
                cols[col]["unique"] = True
        raw.append({"name": str(c.get("source_table") or cid), "fqn": fqn, "cid": cid, "cols": cols, "pk": pk})
    names = {r["name"].lower() for r in raw}
    for p in onto.get("object_properties", {}).values():          # junction tables of many-to-many joins
        j = p.get("join") or {}
        via = j.get("via")
        if not via or via.lower() in names:
            continue
        names.add(via.lower())
        dom = classes.get(p.get("domain")) or {}
        fqn = ".".join(dom["source_fqn"].split(".")[:2] + [via]) if str(dom.get("source_fqn") or "").count(".") == 2 \
            else None
        cols = {r.rpartition(".")[2].lower(): {"name": r.rpartition(".")[2], "type": "", "pid": None, "unique": False}
                for r in j.get("from", []) + j.get("to", [])}
        raw.append({"name": via, "fqn": fqn, "cid": None, "cols": cols, "pk": []})
    three = bool(raw) and all(r["fqn"] for r in raw)
    tables, alias, by_class = {}, {}, {}

    def name_it(a, key):
        a = a.lower()
        alias[a] = key if alias.get(a, key) == key else None          # None = ambiguous

    for r in raw:
        key = (r["fqn"] if three else r["name"]).lower()
        tables[key] = {"key": key, "name": r["name"], "fqn": r["fqn"], "cid": r["cid"], "cols": r["cols"],
                       "pk": r["pk"]}
        if r["cid"]:
            by_class[r["cid"]] = key
        for a in {r["name"]} | ({r["fqn"], ".".join(r["fqn"].split(".")[1:]), r["fqn"].split(".")[-1]}
                                if r["fqn"] else set()):
            name_it(a, key)

    def col_ref(ref: str):
        tbl, _, col = str(ref).rpartition(".")
        k = alias.get(tbl.lower())
        return (k, col.lower()) if k else None

    rels, by_pair, defaults = {}, {}, {}

    def add(a, b, one, many, pid, status="accepted", role=None, default=False):
        if not a or not b:
            return
        key = frozenset({a, b})
        rels.setdefault(key, {"pid": pid, "one": one, "many": many, "status": status, "role": role,
                              "text": f"{tables[a[0]]['name']}.{a[1]} = {tables[b[0]]['name']}.{b[1]}"})
        if status == "accepted":
            by_pair.setdefault(frozenset({a[0], b[0]}), []).append(rels[key]["text"])
        if default:
            defaults[frozenset({a[0], b[0]})] = pid

    for pid, p in onto.get("object_properties", {}).items():
        j = p.get("join") or {}
        if not j or p.get("cardinality") == "one-to-many":
            continue
        if j.get("via"):
            jt = alias.get(j["via"].lower())
            for refs, cls in ((j.get("from", []), p.get("domain")), (j.get("to", []), p.get("range"))):
                k = by_class.get(cls)
                for ref, pkc in zip(refs, (tables[k]["pk"] if k else [])):
                    add(col_ref(ref), (k, pkc), k, jt, pid)
            continue
        for fr, to in zip(j.get("from", []), j.get("to", [])):
            a, b = col_ref(fr), col_ref(to)
            one_one = p.get("cardinality") == "one-to-one"
            add(a, b, None if one_one or not b else b[0], None if one_one or not a else a[0], pid,
                role=p.get("role"), default=bool(p.get("default")))
    for s in onto.get("suggested_relationships") or []:            # reviewed or pending suggestions
        if s.get("status") not in ("proposed", "rejected"):
            continue
        for fc, tc in zip(s.get("from_cols") or [], s.get("to_cols") or []):
            a, b = col_ref(f"{s.get('from_table')}.{fc}"), col_ref(f"{s.get('to_table')}.{tc}")
            add(a, b, b[0] if b else None, a[0] if a else None, s.get("id"), s["status"])
    for (ka, ca), (kb, cb) in [tuple(k) for k in rels if len(k) == 2]:   # FK columns take their key's type
        A, B = tables[ka]["cols"].get(ca), tables[kb]["cols"].get(cb)
        if A and B and bool(A["type"]) != bool(B["type"]):
            (A if not A["type"] else B)["type"] = A["type"] or B["type"]
    return {"tables": tables, "alias": alias, "by_class": by_class, "three": three, "rels": rels,
            "by_pair": by_pair, "defaults": defaults}


def table_ref(t: dict) -> exp.Table:
    """Quoted SQL name of a catalog table (catalog.schema.table on Unity Catalog)."""
    parts = t["fqn"].split(".") if t["fqn"] else [t["name"]]
    ids = [exp.to_identifier(p, quoted=True) for p in parts]
    return exp.Table(this=ids[-1], db=ids[-2] if len(ids) > 1 else None, catalog=ids[-3] if len(ids) > 2 else None)


def _col(name: str) -> exp.Column:
    return exp.column(exp.to_identifier(name, quoted=True))


def _lit(v) -> exp.Expression:
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


def _rows(run, query: exp.Expression, dialect) -> list[list]:
    df = run(query.sql(dialect=glot(dialect)))
    return df.values.tolist() if hasattr(df, "values") else list(df or [])


# --------------------------------------------------------------------------- #
# Guard: one read-only SELECT over the model's tables
# --------------------------------------------------------------------------- #
class Refused(ValueError):
    pass


def parse(sql: str, dialect: str | None) -> exp.Expression:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise Refused("The query is empty.")
    try:
        trees = [t for t in sqlglot.parse(text, read=glot(dialect)) if t is not None
                 and not isinstance(t, getattr(exp, "Semicolon", ()))]
    except Exception as e:  # noqa: BLE001
        raise Refused(f"The query does not parse as {dialect or 'SQL'}: {str(e).splitlines()[0][:300]}") from e
    if len(trees) != 1:
        raise Refused(f"Write exactly one statement, not {len(trees)}.")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, SETOP, exp.Subquery)):
        raise Refused(f"Only SELECT queries are allowed (got {type(tree).__name__.upper()}).")
    for node in tree.walk():
        if isinstance(node, WRITES):
            raise Refused(f"{type(node).__name__.upper()} is not allowed in a read-only query.")
        if isinstance(node, exp.Select) and node.args.get("into") is not None:
            raise Refused("SELECT … INTO creates a table and is not allowed.")
        if isinstance(node, exp.Func):
            name = (str(node.this) if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name.rpartition(".")[2] in BLOCKED_FUNCS:
                raise Refused(f"Function {name}() is not allowed.")
    return tree


def _resolve_tables(tree: exp.Expression, cat: dict) -> tuple[exp.Expression, list[str], set[str]]:
    """A copy whose table names are the catalog's own (so qualify can read the schema), the errors for
    tables outside the model, and the keys of the tables it reads."""
    work = tree.copy()
    ctes = {c.alias_or_name.lower() for c in work.find_all(exp.CTE)}
    errors, used = [], set()
    known = sorted({t["fqn"] or t["name"] for t in cat["tables"].values()})
    for t in list(work.find_all(exp.Table)):
        if not isinstance(t.this, exp.Identifier):
            errors.append(f"Only the model's tables can be read; {t.this.sql()[:60]} is a table function.")
            continue
        parts = [p for p in (t.catalog, t.db, t.name) if p]
        if len(parts) == 1 and parts[0].lower() in ctes:
            continue
        key = cat["alias"].get(".".join(parts).lower())
        if not key:
            close = difflib.get_close_matches(".".join(parts), known, n=3, cutoff=0.5) or \
                difflib.get_close_matches(t.name, [k.split(".")[-1] for k in known], n=3, cutoff=0.5)
            errors.append(f"no such table: {'.'.join(parts)} (not in the model)."
                          + (f" Did you mean {', '.join(close)}?" if close else
                             f" The model's tables: {', '.join(known[:30])}."))
            continue
        used.add(key)
        tb = cat["tables"][key]
        segs = tb["fqn"].split(".") if cat["three"] else [tb["name"]]
        t.set("this", exp.to_identifier(segs[-1]))                   # quoted only when the name needs it
        t.set("db", exp.to_identifier(segs[-2]) if len(segs) > 1 else None)
        t.set("catalog", exp.to_identifier(segs[-3]) if len(segs) > 2 else None)
    return work, errors, used


def _schema(cat: dict, dialect) -> MappingSchema:
    mapping: dict = {}
    for t in cat["tables"].values():
        cols = {c["name"]: c["type"] or "TEXT" for c in t["cols"].values()}
        if cat["three"]:
            c_, s_, n_ = t["fqn"].split(".")
            mapping.setdefault(c_, {}).setdefault(s_, {})[n_] = cols
        else:
            mapping[t["name"]] = cols
    return MappingSchema(mapping, dialect=glot(dialect), normalize=True)


# --------------------------------------------------------------------------- #
# Column lineage (through CTEs, subqueries and unions) and what the query does
# --------------------------------------------------------------------------- #
def _key_of(t: exp.Table, cat: dict) -> str | None:
    return cat["alias"].get(".".join(p for p in (t.catalog, t.db, t.name) if p).lower())


def _trace(scope: Scope, col: exp.Column, cat: dict, depth: int = 0):
    if depth > 8:
        return None
    src = scope.sources.get(col.table)
    if isinstance(src, exp.Table):
        k = _key_of(src, cat)
        return (k, col.name.lower()) if k else None
    if isinstance(src, Scope):
        e = src.expression
        if isinstance(e, SETOP):
            branches = getattr(src, "set_operation_scopes", None) or getattr(src, "union_scopes", None) or []
            got = {_trace_branch(b, col, cat, depth + 1) for b in branches}
            return got.pop() if len(got) == 1 else None
        if isinstance(e, exp.Select):
            for proj in e.expressions:
                if proj.alias_or_name == col.name:
                    inner = proj.this if isinstance(proj, exp.Alias) else proj
                    return _trace(src, inner, cat, depth + 1) if isinstance(inner, exp.Column) else None
    return None


def _trace_branch(scope: Scope, col: exp.Column, cat: dict, depth: int):
    e = scope.expression
    if isinstance(e, SETOP):
        branches = getattr(scope, "set_operation_scopes", None) or getattr(scope, "union_scopes", None) or []
        got = {_trace_branch(b, col, cat, depth + 1) for b in branches}
        return got.pop() if len(got) == 1 else None
    if isinstance(e, exp.Select):
        for proj in e.expressions:
            if proj.alias_or_name == col.name:
                inner = proj.this if isinstance(proj, exp.Alias) else proj
                return _trace(scope, inner, cat, depth + 1) if isinstance(inner, exp.Column) else None
    return None


def _value(node):
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        try:
            return float(node.this) if any(ch in node.this for ch in ".eE") else int(node.this)
        except ValueError:
            return None
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        v = _value(node.this)
        return -v if isinstance(v, (int, float)) else None
    return None


def _bare(node):
    while isinstance(node, (exp.Lower, exp.Upper, exp.Trim, exp.Cast, exp.Paren)):
        node = node.this
    return node if isinstance(node, exp.Column) else None


def _filter(scope, node, cat, d):
    if isinstance(node, exp.In):
        col, vals, op = _bare(node.this), [_value(v) for v in node.expressions], "IN"
        if col is None or not vals or any(v is None for v in vals):
            return None
    else:
        col = _bare(node.left) or _bare(node.right)
        v = _value(node.right if _bare(node.left) else node.left)
        if col is None or v is None:
            return None
        vals, op = [v], type(node).__name__.upper()
    phys = _trace(scope, col, cat)
    return {"col": phys, "op": op, "values": vals, "sql": node.sql(dialect=d)} if phys else None


def _groups(select: exp.Select) -> list[exp.Expression]:
    g = select.args.get("group")
    out = []
    for item in (g.expressions if g else []):
        if isinstance(item, exp.Literal) and not item.is_string and item.this.isdigit():
            i = int(item.this) - 1
            if 0 <= i < len(select.expressions):
                p = select.expressions[i]
                item = p.this if isinstance(p, exp.Alias) else p
        out.append(item)
    return out


def _analyze(tree, cat, d) -> dict:
    a = {"tables": set(), "columns": set(), "joins": [], "text": [], "numbers": [], "aggs": [], "scopes": []}
    try:
        scopes = list(traverse_scope(tree))
    except Exception:  # noqa: BLE001
        scopes = []
    a["scopes"] = scopes
    for scope in scopes:
        for src in scope.sources.values():
            if isinstance(src, exp.Table) and _key_of(src, cat):
                a["tables"].add(_key_of(src, cat))
        for c in scope.columns:
            phys = _trace(scope, c, cat)
            if phys:
                a["columns"].add(phys)
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            continue
        roots = [j.args["on"] for j in sel.args.get("joins") or [] if j.args.get("on") is not None]
        roots += [sel.args[k].this for k in ("where", "having") if sel.args.get(k) is not None]
        for root in roots:
            for node in root.find_all(exp.EQ, exp.NEQ, exp.In, exp.Like, exp.ILike, exp.GT, exp.GTE, exp.LT,
                                      exp.LTE, exp.Between):
                if node.find_ancestor(exp.Select) is not sel:
                    continue
                if isinstance(node, exp.EQ) and isinstance(node.left, exp.Column) and isinstance(node.right, exp.Column):
                    x, y = _trace(scope, node.left, cat), _trace(scope, node.right, cat)
                    if x and y and x[0] != y[0]:
                        a["joins"].append((x, y))
                    continue
                if isinstance(node, (exp.EQ, exp.NEQ, exp.Like, exp.ILike, exp.In)):
                    f = _filter(scope, node, cat, d)
                    if f:
                        (a["text"] if all(isinstance(v, str) for v in f["values"]) else a["numbers"]).append(f)
                    continue
                if isinstance(node, exp.Between):
                    col, lo, hi = _bare(node.this), _value(node.args.get("low")), _value(node.args.get("high"))
                    if col is not None and all(isinstance(v, (int, float)) for v in (lo, hi)):
                        phys = _trace(scope, col, cat)
                        if phys:
                            a["numbers"].append({"col": phys, "op": "BETWEEN", "values": [lo, hi],
                                                 "sql": node.sql(dialect=d)})
                    continue
                col, v = _bare(node.left), _value(node.right)
                if col is None:
                    col, v = _bare(node.right), _value(node.left)
                if col is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
                    phys = _trace(scope, col, cat)
                    if phys:
                        a["numbers"].append({"col": phys, "op": type(node).__name__.upper(), "values": [v],
                                             "sql": node.sql(dialect=d)})
        for proj in sel.expressions:          # SUM(CASE WHEN status = 'x' ...) picks rows like WHERE does
            for node in proj.find_all(exp.EQ, exp.NEQ, exp.Like, exp.ILike, exp.In):
                if node.find_ancestor(exp.Select) is sel:
                    f = _filter(scope, node, cat, d)
                    if f and all(isinstance(v, str) for v in f["values"]):
                        a["text"].append(f)
        group_tables = {p[0] for g in _groups(sel) for c in g.find_all(exp.Column) if (p := _trace(scope, c, cat))}
        for agg in sel.find_all(exp.Sum, exp.Avg, exp.Count):
            if agg.find_ancestor(exp.Select) is not sel:
                continue
            arg, distinct = agg.this, False
            if isinstance(arg, exp.Distinct):
                distinct, arg = True, (arg.expressions[0] if arg.expressions else None)
            cols = [] if arg is None else list(arg.find_all(exp.Column))
            cond = {id(c) for i in ([] if arg is None else arg.find_all(exp.If)) for c in i.this.find_all(exp.Column)}
            a["aggs"].append({
                "sql": agg.sql(dialect=d), "distinct": distinct,
                "cols": [p for p in (_trace(scope, c, cat) for c in cols) if p],
                "values": [p for p in (_trace(scope, c, cat) for c in cols if id(c) not in cond) if p],
                "scope": {_key_of(s, cat) for s in scope.sources.values() if isinstance(s, exp.Table)} - {None},
                "groups": group_tables})
    return a


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _name(cat, phys) -> str:
    return f"{cat['tables'][phys[0]]['name']}.{cat['tables'][phys[0]]['cols'].get(phys[1], {}).get('name', phys[1])}"


MISSING_COLUMN = re.compile(r"Unknown column: ([^\s,]+)|Column '([^']+)' could not be resolved", re.I)


def _explain(message: str, cat: dict, onto: dict) -> str:
    m = MISSING_COLUMN.search(message)
    if not m:
        return f"The query could not be matched to the model: {message}"
    missing = (m.group(1) or m.group(2)).split(".")[-1].strip('`"')
    owners = [t["name"] for t in cat["tables"].values() if missing.lower() in t["cols"]]
    if owners:
        return (f"Column {missing} belongs to {', '.join(owners)}, which is not joined where the query uses it "
                "(or its table alias is wrong).")
    names = sorted({c["name"] for t in cat["tables"].values() for c in t["cols"].values()})
    close = difflib.get_close_matches(missing, names, n=4, cutoff=0.6)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    labels = [f"'{p.get('label') or pid}' is {c.get('source_table') or cid}.{p.get('source_column') or pid}"
              for cid, c in onto.get("classes", {}).items() for pid, p in (c.get("data_properties") or {}).items()
              if missing.lower().replace("_", "") in {str(p.get("label") or "").lower().replace(" ", ""), pid.lower()}
              or missing.lower() in [s.lower() for s in p.get("synonyms") or []]]
    if labels:
        hint += " In the model, " + "; ".join(labels[:3]) + "."
    return f"Column {missing} is not in the model's tables.{hint}"


def _check_joins(a, cat, run, d, errors, warnings, notes, budget) -> set:
    """Returns {(one_key, many_key)} for the one-to-many joins the query makes."""
    fanned, seen = set(), set()
    for x, y in a["joins"]:
        key = frozenset({x, y})
        if key in seen:
            continue
        seen.add(key)
        label = f"{_name(cat, x)} = {_name(cat, y)}"
        rel = cat["rels"].get(key)
        if rel:
            if rel["status"] == "rejected":
                errors.append(f"Join {label} was reviewed and rejected in the model.")
            elif rel["status"] == "proposed":
                warnings.append(f"Join {label} is only a suggested (unreviewed) relationship.")
            dflt = cat["defaults"].get(frozenset({x[0], y[0]}))
            if dflt and rel["status"] == "accepted" and rel["pid"] != dflt:
                notes.append(f"Join {label} uses {rel['pid']}" + (f" (role {rel['role']})" if rel.get("role") else "")
                             + f", not the default join {dflt}. Check that the question means this role.")
            if rel["one"] and rel["many"]:
                fanned.add((rel["one"], rel["many"]))
            continue
        cx, cy = cat["tables"][x[0]]["cols"].get(x[1]), cat["tables"][y[0]]["cols"].get(y[1])
        if not cx or not cy:
            continue
        known = cat["by_pair"].get(frozenset({x[0], y[0]}))
        if known:
            errors.append(f"Join {label} is not how the model joins these tables ({'; '.join(known)}). Matching "
                          "values between unrelated keys are a coincidence; use the modeled join.")
        else:
            warnings.append(f"Join {label} is not a relationship in the model.")
        if cx["type"] and cy["type"] and _family(cx["type"]) != _family(cy["type"]):
            errors.append(f"Join {label} compares {cx['type']} with {cy['type']}.")
            continue
        if run is not None and budget[0] > 0:
            budget[0] -= 1
            ratio = _containment(run, cat, x, y, d)
            if ratio is not None and ratio < 0.5:
                errors.append(f"Join {label}: only {ratio:.0%} of {_name(cat, x)} values exist in {_name(cat, y)}; "
                              "this join is very likely wrong.")
            elif ratio is not None:
                notes.append(f"Join {label}: {ratio:.0%} of sampled values match.")
        if cx.get("unique") and not cy.get("unique"):
            fanned.add((x[0], y[0]))
        elif cy.get("unique") and not cx.get("unique"):
            fanned.add((y[0], x[0]))
    return fanned


def _containment(run, cat, x, y, d) -> float | None:
    tx, ty = cat["tables"][x[0]], cat["tables"][y[0]]
    cx, cy = tx["cols"][x[1]]["name"], ty["cols"][y[1]]["name"]
    src = (exp.select(_col(cx).as_("v")).distinct().from_(table_ref(tx))
           .where(exp.Not(this=exp.Is(this=_col(cx), expression=exp.Null()))).limit(300).subquery("s"))
    tcol = exp.column(exp.to_identifier(cy, quoted=True), table="t")
    q = (exp.select(exp.Count(this=exp.Star()).as_("total"), exp.Count(this=tcol).as_("hits")).from_(src)
         .join(table_ref(ty).as_("t"), on=exp.EQ(this=tcol, expression=exp.column("v", table="s")), join_type="left"))
    try:
        total, hits = (_rows(run, q, d) or [[0, 0]])[0][:2]
    except Exception:  # noqa: BLE001
        return None
    return (hits or 0) / total if total else None


def _check_fanout(a, cat, fanned, warnings) -> None:
    if not fanned:
        return
    pairs = {(o, m) for o, m in fanned}
    for g in a["aggs"]:
        if g["distinct"] or not g["cols"]:
            continue
        values = g["values"] or g["cols"]
        for one, many in pairs:
            if any(v[0] == many for v in values) or not {one, many} <= g["scope"]:
                continue
            for v in values:
                if v[0] == one:
                    o, m = cat["tables"][one]["name"], cat["tables"][many]["name"]
                    warnings.append(f"Fan-out: {g['sql']} aggregates {_name(cat, v)}, but {o} is joined one-to-many "
                                    f"to {m}, so each {o} row counts once per matching {m} row. Aggregate {m} "
                                    f"first (subquery or CTE) or aggregate at {o} grain.")
                    break
    # chasm trap: aggregated rows repeat for every row of a sibling branch hanging off a shared parent
    edges: dict[str, set] = {}
    for x, y in a["joins"]:
        edges.setdefault(x[0], set()).add(y[0])
        edges.setdefault(y[0], set()).add(x[0])
    done = set()
    for g in a["aggs"]:
        start = {v[0] for v in (g["values"] or g["cols"])}
        if g["distinct"] or not start or not start <= g["scope"] or g["sql"] in done:
            continue
        reached, queue, branches = set(start), list(start), {}
        while queue:
            u = queue.pop()
            for v in sorted(edges.get(u, ())):
                if v not in g["scope"] or v in reached:
                    continue
                if (u, v) not in pairs:
                    reached.add(v)
                    queue.append(v)
                elif u not in start and not (_reach(edges, v, u, g["scope"]) & g["groups"]):
                    branches.setdefault(u, []).append(v)
        for u, vs in branches.items():
            done.add(g["sql"])
            agg_t = ", ".join(sorted(cat["tables"][t]["name"] for t in start))
            sib = " and ".join(cat["tables"][v]["name"] for v in vs)
            parent = cat["tables"][u]["name"]
            warnings.append(f"Fan-out: {g['sql']} aggregates {agg_t} rows, but {parent} is also joined one-to-many to "
                            f"{sib}, so each {agg_t} row repeats for every matching {sib} row. They are related "
                            f"only through {parent}: aggregate each branch in its own subquery, then join on {parent}.")


def _reach(edges, start, parent, scope) -> set:
    seen, queue = {start}, [start]
    while queue:
        for n in edges.get(queue.pop(), ()):
            if n != parent and n in scope and n not in seen:
                seen.add(n)
                queue.append(n)
    return seen


def _check_shape(a, cat, fanned, d, errors, warnings) -> None:
    sel = a["tree"]
    while isinstance(sel, exp.Subquery):
        sel = sel.this
    if isinstance(sel, exp.Select):                                   # placeholder queries
        where = sel.args.get("where")
        if where is not None and _always_false(where.this):
            errors.append(f"WHERE {where.this.sql(dialect=d)} is never true, so the query returns nothing "
                          "whatever the data holds. If the data cannot answer the question, say so.")
        projs = [p.this if isinstance(p, exp.Alias) else p for p in sel.expressions]
        if projs and all(isinstance(p, exp.Null) for p in projs):
            errors.append("Every selected value is NULL, so the query does not read the data.")
    one_many = {(cat["tables"][o]["name"].lower(), cat["tables"][m]["name"].lower()) for o, m in fanned}
    for scope in a["scopes"]:
        s = scope.expression
        if not isinstance(s, exp.Select):
            continue
        aggs = [x for x in s.find_all(exp.AggFunc) if x.find_ancestor(exp.Select) is s]
        groups = _groups(s)
        srcs = list(scope.sources.values())
        if aggs and not s.args.get("joins") and len(srcs) == 1 and isinstance(srcs[0], exp.Table):
            for g in groups:                                          # GROUP BY the only table's key
                p = _trace(scope, g, cat) if isinstance(g, exp.Column) else None
                if p and cat["tables"][p[0]]["pk"] == [p[1]]:
                    warnings.append(f"GROUP BY {_name(cat, p)} groups by the primary key, so every group is one row "
                                    f"and {aggs[0].sql(dialect=d)} aggregates a single value.")
                    break
        if groups and one_many:                                       # many-side column next to the groups
            gsql = {g.sql(dialect=d) for g in groups}
            gtab = {cat["tables"][p[0]]["name"].lower() for g in groups for c in g.find_all(exp.Column)
                    if (p := _trace(scope, c, cat))}
            for proj in s.expressions:
                inner = proj.this if isinstance(proj, exp.Alias) else proj
                if inner.sql(dialect=d) in gsql:
                    continue
                for c in inner.find_all(exp.Column):
                    up, covered = c.parent, False
                    while up is not None and up is not s:
                        if isinstance(up, (exp.AggFunc, exp.Window)) or up.sql(dialect=d) in gsql:
                            covered = True
                            break
                        up = up.parent
                    p = None if covered or c.sql(dialect=d) in gsql else _trace(scope, c, cat)
                    tname = cat["tables"][p[0]]["name"].lower() if p else None
                    if p and tname not in gtab and any(m == tname and o in gtab for o, m in one_many):
                        warnings.append(f"{_name(cat, p)} is selected without an aggregate, but the query groups by "
                                        f"another table it has several rows for; aggregate it or add it to GROUP BY.")
        if (d or "") in TRUNCATING:                                   # integer division
            for div in s.find_all(exp.Div):
                if div.find_ancestor(exp.Select) is s and _is_int(div.left, scope, cat) and _is_int(div.right, scope, cat):
                    warnings.append(f"Integer division: both sides of {div.sql(dialect=d)} are integers, so the "
                                    f"fraction is dropped (7 / 2 = 3). Cast one side: CAST({div.left.sql(dialect=d)} "
                                    f"AS REAL) / {div.right.sql(dialect=d)}.")


def _always_false(node) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Boolean):
        return node.this is False
    if isinstance(node, exp.Literal) and not node.is_string:
        return node.this in ("0", "0.0")
    if isinstance(node, (exp.EQ, exp.NEQ)) and isinstance(node.left, exp.Literal) and isinstance(node.right, exp.Literal):
        same = node.left.this == node.right.this and node.left.is_string == node.right.is_string
        return (not same) if isinstance(node, exp.EQ) else same
    if isinstance(node, exp.And):
        return _always_false(node.left) or _always_false(node.right)
    return False


def _is_int(node, scope, cat) -> bool:
    if isinstance(node, (exp.Paren, exp.Neg)):
        return _is_int(node.this, scope, cat)
    if isinstance(node, exp.Literal):
        return not node.is_string and not any(ch in node.this for ch in ".eE")
    if isinstance(node, exp.Count):
        return True
    if isinstance(node, (exp.Sum, exp.Min, exp.Max)):
        return _is_int(node.this, scope, cat)
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        return _is_int(node.left, scope, cat) and _is_int(node.right, scope, cat)
    if isinstance(node, exp.Column):
        p = _trace(scope, node, cat)
        return bool(p) and cat["tables"][p[0]]["cols"].get(p[1], {}).get("type") == "BIGINT"
    return False


def _check_text(a, cat, run, d, warnings, budget) -> None:
    done = set()
    for f in a["text"]:
        col = cat["tables"][f["col"][0]]["cols"].get(f["col"][1])
        if not col or f["op"] == "NEQ":
            continue
        for v in f["values"][:10]:
            if (f["col"], f["op"], v) in done:
                continue
            done.add((f["col"], f["op"], v))
            name = _name(cat, f["col"])
            if col.get("values") and f["op"] in ("EQ", "IN"):            # a profiled, complete value list
                if v in col["values"]:
                    continue
                near = _near(v, [str(x) for x in col["values"]])
                warnings.append(f"No row has {name} = '{v}'." + (f" Close real values: {', '.join(map(repr, near))}."
                                                                 if near else f" Its values: {', '.join(map(repr, col['values'][:12]))}."))
                continue
            if run is None or budget[0] <= 0:
                continue
            budget[0] -= 1
            t = table_ref(cat["tables"][f["col"][0]])
            cond = (exp.Like if f["op"] == "LIKE" else exp.ILike if f["op"] == "ILIKE" else exp.EQ)(
                this=_col(col["name"]), expression=_lit(v))
            try:
                if _rows(run, exp.select(exp.Literal.number(1)).from_(t).where(cond).limit(1), d):
                    continue
            except Exception:  # noqa: BLE001
                continue
            if f["op"] in ("LIKE", "ILIKE"):
                low = exp.Like(this=exp.Lower(this=exp.cast(_col(col["name"]), "STRING")), expression=_lit(v.lower()))
                try:
                    hits = [r[0] for r in _rows(run, exp.select(_col(col["name"])).distinct().from_(t).where(low).limit(5), d)]
                except Exception:  # noqa: BLE001
                    hits = []
                warnings.append(f"{name} {f['op']} '{v}' matches nothing" + (
                    f", but matches case-insensitively: {', '.join(map(repr, hits))}." if hits else "."))
                continue
            near = _near_values(run, t, col["name"], v, d, budget)
            warnings.append(f"No row has {name} = '{v}'." + (f" Close real values: {', '.join(map(repr, near))}."
                                                             if near else " No similar value was found either."))


def _near(v: str, values: list[str]) -> list[str]:
    low = {x.casefold().strip(): x for x in values}
    if v.casefold().strip() in low:
        return [low[v.casefold().strip()]]
    words = re.findall(r"\w+", v.casefold())
    containing = [x for x in values if words and all(w in x.casefold() for w in words)]
    return containing[:3] or difflib.get_close_matches(v, values, n=3, cutoff=CLOSE) or \
        [x for x in values if v.casefold() in x.casefold()][:3]


def _near_values(run, t, name, v, d, budget) -> list:
    try:
        rows = _rows(run, exp.select(_col(name)).distinct().from_(t)
                     .where(exp.Not(this=exp.Is(this=_col(name), expression=exp.Null()))).limit(LOW_CARD + 1), d)
    except Exception:  # noqa: BLE001
        rows = []
    values = [r[0] for r in rows if isinstance(r[0], str)]
    if values and len(rows) <= LOW_CARD:
        return _near(v, values)
    if budget[0] <= 0:
        return []
    budget[0] -= 1
    like = exp.Like(this=exp.Lower(this=exp.cast(_col(name), "STRING")), expression=_lit(f"%{v.strip().lower()}%"))
    try:
        return [r[0] for r in _rows(run, exp.select(_col(name)).distinct().from_(t).where(like).limit(3), d)]
    except Exception:  # noqa: BLE001
        return []


def _check_scale(a, cat, run, d, question, warnings, budget) -> None:
    if run is None:
        return
    ranges: dict = {}

    def rng(phys):
        if phys not in ranges:
            ranges[phys] = None
            if budget[0] > 0:
                budget[0] -= 1
                c = cat["tables"][phys[0]]["cols"][phys[1]]["name"]
                try:
                    lo, hi = (_rows(run, exp.select(exp.Min(this=_col(c)), exp.Max(this=_col(c)))
                                    .from_(table_ref(cat["tables"][phys[0]])), d) or [[None, None]])[0][:2]
                    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (lo, hi)):
                        ranges[phys] = (float(lo), float(hi))
                except Exception:  # noqa: BLE001
                    pass
        return ranges[phys]

    percents = {float(m.group(1)) for m in PERCENT_IN_TEXT.finditer(question or "")} - {0.0}
    seen = set()
    for f in a["numbers"]:
        if f["col"][1] not in cat["tables"][f["col"][0]]["cols"] or f["col"] in seen:
            continue
        r = rng(f["col"]) if f["op"] in ("GT", "GTE", "LT", "LTE", "BETWEEN") or percents else None
        if r is None:
            continue
        seen.add(f["col"])
        lo, hi = r
        name = _name(cat, f["col"])
        for v in f["values"]:
            if not isinstance(v, (int, float)):
                continue
            if f["op"] in ("GT", "GTE", "LT", "LTE", "BETWEEN") and not lo <= v <= hi:
                if lo <= v / 100 <= hi and hi <= 1.5:
                    warnings.append(f"{f['sql']}: {name} ranges {lo:g} to {hi:g}, so {v:g} looks like a percent "
                                    f"against a 0 to 1 fraction. Did you mean {v / 100:g}?")
                elif lo <= v * 100 <= hi and hi > 1.5 and v < 1:
                    warnings.append(f"{f['sql']}: {name} ranges {lo:g} to {hi:g}, so {v:g} looks like a fraction "
                                    f"against a percent column. Did you mean {v * 100:g}?")
                else:
                    warnings.append(f"{f['sql']}: {v:g} is outside the range of {name} ({lo:g} to {hi:g}).")
                continue
            for p in percents:                    # "below 0.18%" written as 0.18 against a 0 to 1 column
                if lo >= 0 and hi <= 1.5 and abs(v - p) < 1e-9 and abs(v - p / 100) > 1e-12:
                    warnings.append(f"{f['sql']}: the question says {p:g}%, but {name} holds fractions from {lo:g} "
                                    f"to {hi:g}, so {p:g}% is {p / 100:g}. Did you mean {p / 100:g}?")


def grounding(a, cat, onto, sql) -> dict:
    classes = {cat["tables"][k]["cid"] for k in a["tables"]} - {None}
    status = {cid: (onto["classes"][cid].get("certification") or {}).get("status", "draft") for cid in classes}
    norm = re.sub(r"\s+|`|\"", "", sql.lower())
    metrics = []
    for m in onto.get("metrics", []):
        body = re.sub(r"\s+|`|\"", "", str(m.get("sql") or "").lower())
        bare = re.sub(r"\b\w+\.(?=\w)", "", body)
        if m.get("base_class") in classes and body and (body in norm or (bare and bare in re.sub(r"\b\w+\.(?=\w)", "", norm))):
            metrics.append({"name": m["name"], "status": m.get("status", "draft")})
    unmodeled = sorted({_name(cat, p) for p in a["columns"] if p[0] in cat["tables"]
                        and not cat["tables"][p[0]]["cols"].get(p[1], {}).get("pid")
                        and not any(p in k for k in cat["rels"])})
    return {"classes": sorted(classes), "draft_classes": sorted(c for c, s in status.items() if s != "certified"),
            "deprecated_classes": sorted(c for c, s in status.items() if s == "deprecated"),
            "metrics": metrics, "unmodeled_columns": unmodeled}


def validate(sql: str, onto: dict, dialect: str | None = "databricks", run: Callable | None = None,
             question: str | None = None, dry_run: bool = True, probe_budget: int = 12, cat: dict | None = None) -> dict:
    """Check one query against the model and, with run(sql) -> DataFrame, against the data."""
    out = {"valid": False, "errors": [], "warnings": [], "notes": [], "grounding": {}, "probes": 0}
    d = glot(dialect)
    try:
        tree = parse(sql, d)
    except Refused as e:
        out["errors"].append(str(e))
        return out
    cat = cat or catalog(onto)
    work, errors, used = _resolve_tables(tree, cat)
    out["errors"] += errors
    if errors:
        return out
    budget = [probe_budget]
    try:
        q = qualify(work.copy(), schema=_schema(cat, d), dialect=d, validate_qualify_columns=True,
                    quote_identifiers=False, identify=False)
    except Exception as e:  # noqa: BLE001 - unknown or ambiguous columns, or syntax sqlglot cannot resolve
        msg = str(e).splitlines()[0]
        m = MISSING_COLUMN.search(msg)
        name = (m.group(1) or m.group(2)).split(".")[-1].strip('`"').lower() if m else ""
        if m and name not in {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}:
            out["errors"].append(_explain(msg, cat, onto))
            return out
        if m:                                    # Databricks lateral column alias: SELECT a AS x, x * 2 AS y
            msg = f"the query reuses the column alias {name} in the same SELECT"
        if "ambiguous" in msg.lower():
            out["errors"].append(f"{msg}. Qualify the column with its table alias.")
            return out
        out["notes"].append(f"Some column checks were skipped: {msg[:200]}")
        q = work
    a = _analyze(q, cat, d)
    a["tree"] = q
    fanned = _check_joins(a, cat, run, d, out["errors"], out["warnings"], out["notes"], budget)
    _check_fanout(a, cat, fanned, out["warnings"])
    _check_shape(a, cat, fanned, d, out["errors"], out["warnings"])
    _check_text(a, cat, run, d, out["warnings"], budget)
    _check_scale(a, cat, run, d, question, out["warnings"], budget)
    if run is not None and dry_run and not out["errors"]:
        try:
            run(exp.select("*").from_(tree.copy().subquery("q")).limit(0).sql(dialect=d))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"The database rejected the query: {str(e).splitlines()[0][:400]}")
    g = out["grounding"] = grounding(a, cat, onto, sql)
    if g["deprecated_classes"]:
        out["warnings"].append("Reads deprecated classes: " + ", ".join(g["deprecated_classes"]) + ".")
    dep = [m["name"] for m in g["metrics"] if m["status"] == "deprecated"]
    if dep:
        out["warnings"].append("Uses deprecated metric definitions: " + ", ".join(dep) + ".")
    if g["unmodeled_columns"]:
        out["notes"].append("Columns outside the semantic layer: " + ", ".join(g["unmodeled_columns"][:10]) + ".")
    out["warnings"] = list(dict.fromkeys(out["warnings"]))
    out["probes"] = probe_budget - budget[0]
    out["valid"] = not out["errors"]
    return out


# --------------------------------------------------------------------------- #
# Values and capped runs
# --------------------------------------------------------------------------- #
def lookup_values(run, onto: dict, table: str, column: str, contains: str | None = None, limit: int = 20,
                  dialect: str | None = "databricks", close: bool = True) -> dict:
    """Real values in table (a class or table name).column: matches for a word (case-insensitive), close
    spellings when nothing matches, or the most common values with their row counts."""
    cat = catalog(onto)
    key = cat["by_class"].get(table) or cat["alias"].get(str(table).lower())
    if not key or str(column).lower() not in cat["tables"][key]["cols"]:
        raise KeyError(f"{table}.{column} is not in the model")
    t, name, d = cat["tables"][key], cat["tables"][key]["cols"][str(column).lower()]["name"], glot(dialect)
    limit = max(1, min(int(limit), 100))
    c, n = _col(name), exp.column("n")
    base = exp.select(c.as_("value"), exp.Count(this=exp.Star()).as_("n")).from_(table_ref(t)).group_by(c)
    out = {"column": f"{t['name']}.{name}", "contains": contains or None}
    if contains:
        cond = exp.Like(this=exp.Lower(this=exp.cast(c, "STRING")), expression=_lit(f"%{contains.strip().lower()}%"))
        rows = _rows(run, base.where(cond).order_by(exp.Ordered(this=n, desc=True)).limit(limit), d)
        out["matches"] = [{"value": r[0], "rows": int(r[1])} for r in rows]
        if not rows and close:
            vals = [r[0] for r in _rows(run, exp.select(c).distinct().from_(table_ref(t)).where(
                exp.Not(this=exp.Is(this=c, expression=exp.Null()))).limit(2000), d) if isinstance(r[0], str)]
            out["close_values"] = difflib.get_close_matches(contains, vals, n=8, cutoff=0.5)
        return out
    rows = _rows(run, base.order_by(exp.Ordered(this=n, desc=True)).limit(limit + 1), d)
    out["top_values"] = [{"value": r[0], "rows": int(r[1])} for r in rows[:limit]]
    out["more_values_exist"] = len(rows) > limit
    return out


def joins_in(sql: str, onto: dict, dialect: str | None = "databricks", cat: dict | None = None) -> list:
    """The column-to-column joins a query makes, as (frozenset of two (table key, column) pairs, 'a.x = b.y')."""
    cat, d = cat or catalog(onto), glot(dialect)
    try:
        work, errors, _ = _resolve_tables(parse(sql, d), cat)
        if errors:
            return []
        q = qualify(work.copy(), schema=_schema(cat, d), dialect=d, validate_qualify_columns=False,
                    quote_identifiers=False, identify=False)
    except Exception:  # noqa: BLE001 - a query that cannot be read has no joins to learn from
        return []
    return [(frozenset({x, y}), f"{_name(cat, x)} = {_name(cat, y)}") for x, y in _analyze(q, cat, d)["joins"]]


def find_value(run, onto: dict, columns: list[tuple[str, str]], word: str, dialect: str | None = "databricks",
               limit: int = 5) -> list[dict]:
    """Which of these (class, column) pairs store `word` as a whole word: one UNION ALL query."""
    cat, d = catalog(onto), glot(dialect)
    w = re.sub(r"[%_\\']", "", str(word)).strip().lower()
    branches = []
    for cid, col in columns:
        t = cat["tables"].get(cat["by_class"].get(cid) or "")
        c = (t or {}).get("cols", {}).get(str(col).lower())
        if not c or not w:
            continue
        low = exp.Lower(this=exp.cast(_col(c["name"]), "STRING"))
        cond = exp.or_(exp.EQ(this=low, expression=_lit(w)), *[exp.Like(this=low, expression=_lit(p))
                                                               for p in (f"{w} %", f"% {w}", f"% {w} %")])
        branches.append(exp.select(exp.Literal.string(cid).as_("cls"), exp.Literal.string(t["name"]).as_("tbl"),
                                   exp.Literal.string(c["name"]).as_("col"), exp.cast(_col(c["name"]), "STRING")
                                   .as_("value")).from_(table_ref(t)).where(cond))
    if not branches:
        return []
    q = branches[0]
    for b in branches[1:]:
        q = exp.union(q, b, distinct=False)
    rows = _rows(run, exp.select("*").from_(q.subquery("v")).limit(limit), d)
    return [{"class": r[0], "column": f"{r[1]}.{r[2]}", "value": r[3]} for r in rows]


def run_capped(run, sql: str, dialect: str | None = "databricks", cap: int = 200):
    """Run one read-only query with at most `cap` rows. Returns (DataFrame, truncated)."""
    d = glot(dialect)
    tree = parse(sql, d)
    lim = tree.args.get("limit") if isinstance(tree, exp.Select) else None
    n = _value(lim.expression) if lim is not None else None
    if isinstance(tree, exp.Select) and not (isinstance(n, int) and n <= cap):
        tree = tree.copy().limit(cap + 1)
    elif not isinstance(tree, exp.Select):
        tree = exp.select("*").from_(tree.copy().subquery("q")).limit(cap + 1)
    df = run(tree.sql(dialect=d))
    return df.head(cap), len(df) > cap
