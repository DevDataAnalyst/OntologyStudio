"""
SQL helpers for BusinessObjects expressions and report SQL (sqlglot-based).

* BO @functions (@Aggregate_Aware, @Prompt, @Select, @Where, @Variable) are resolved before parsing, and each
  resolution is reported so it can become a provenance rule or a finding.
* Object expressions are transpiled from the Universe's source dialect (e.g. Oracle) to Databricks SQL, with
  table references re-pointed at Ossie dataset names.
* Oracle has no BOOLEAN: flags stored as NUMBER(1) are compared with 1/0. When the Unity Catalog column is
  BOOLEAN, `flag = 1` becomes `flag = TRUE` (rule `bool_flag`).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

AGG_FUNCS = {"SUM", "COUNT", "AVG", "MIN", "MAX", "MEDIAN", "STDDEV", "VARIANCE", "COUNT_IF", "APPROX_COUNT_DISTINCT"}
_AA = re.compile(r"@Aggregate_Aware\s*\(", re.I)
_PROMPT = re.compile(r"@Prompt\s*\(\s*'([^']*)'([^)]*)\)", re.I)
_SELECT = re.compile(r"@Select\s*\(\s*([^)]+)\)", re.I)
_WHERE = re.compile(r"@Where\s*\(\s*([^)]+)\)", re.I)
_VARIABLE = re.compile(r"@Variable\s*\(\s*'([^']*)'\s*\)", re.I)


def _split_args(s: str) -> list[str]:
    out, depth, cur, q = [], 0, "", False
    for ch in s:
        if ch == "'":
            q = not q
        if not q and ch == "(":
            depth += 1
        elif not q and ch == ")":
            depth -= 1
        if ch == "," and depth == 0 and not q:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _matching_paren(s: str, start: int) -> int:
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def resolve(sql: str, objects_by_path: dict[str, dict] | None = None, prompt_values: dict | None = None,
            prompt_mode: str = "value") -> tuple[str, list[dict]]:
    """Resolve BO @functions. Returns (sql, notes); each note is {"function", "detail", "rule_id"}.

    prompt_mode: "value" substitutes the captured/default prompt value, "param" writes :name placeholders.
    """
    notes: list[dict] = []
    objects_by_path = objects_by_path or {}
    prompt_values = prompt_values or {}
    s = sql or ""
    while True:
        m = _AA.search(s)
        if not m:
            break
        end = _matching_paren(s, m.end() - 1)
        args = _split_args(s[m.end():end])
        chosen = args[-1] if args else ""
        notes.append({"function": "@Aggregate_Aware", "detail": {"candidates": args, "chosen": chosen},
                      "rule_id": "aa_most_detailed"})
        s = s[:m.start()] + chosen + s[end + 1:]
    for m in list(_SELECT.finditer(s)):
        ref = m.group(1).strip().strip("'")
        obj = objects_by_path.get(ref.replace("/", "\\"))
        notes.append({"function": "@Select", "detail": ref, "rule_id": "select_inline" if obj else None})
        if obj:
            s = s.replace(m.group(0), f"({obj.get('select')})")
    for m in list(_WHERE.finditer(s)):
        ref = m.group(1).strip().strip("'")
        obj = objects_by_path.get(ref.replace("/", "\\"))
        notes.append({"function": "@Where", "detail": ref, "rule_id": "where_inline" if obj else None})
        if obj:
            s = s.replace(m.group(0), f"({obj.get('where')})")
    for m in list(_PROMPT.finditer(s)):
        label = m.group(1)
        key = re.sub(r"\W+", "_", label.lower()).strip("_")
        vals = prompt_values.get(label, prompt_values.get(key))
        if prompt_mode == "param" or vals is None:
            rep = f":{key}"
        else:
            vals = vals if isinstance(vals, list) else [vals]
            rep = ", ".join(str(v) if isinstance(v, (int, float)) else f"'{v}'" for v in vals)
        notes.append({"function": "@Prompt", "detail": {"label": label, "param": key, "value": vals},
                      "rule_id": "prompt_param"})
        s = s.replace(m.group(0), rep)
    for m in list(_VARIABLE.finditer(s)):
        notes.append({"function": "@Variable", "detail": m.group(1), "rule_id": None})
    return s, notes


def parse(sql: str, dialect: str = "oracle"):
    """Parse a statement or expression; returns (tree, error)."""
    try:
        return sqlglot.parse_one(sql, read=dialect), None
    except Exception as e:  # noqa: BLE001
        return None, str(e).splitlines()[0]


def columns(tree) -> list[tuple[str, str]]:
    """(TABLE, COLUMN) pairs, upper-cased, for qualified column references."""
    out = []
    for c in tree.find_all(exp.Column):
        if c.table:
            out.append((c.table.upper(), c.name.upper()))
    return list(dict.fromkeys(out))


def tables(tree) -> list[str]:
    return list(dict.fromkeys(t for t, _ in columns(tree)))


def aggregations(tree) -> list[str]:
    """Aggregate function names used in an expression (outermost first)."""
    names = []
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, exp.AggFunc):
            n = node.key.upper()
            if isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct):
                n = "COUNT_DISTINCT"
            names.append(n)
    return names


def is_bare_column(tree) -> bool:
    return isinstance(tree, exp.Column)


def _aliases(tree) -> dict[str, str]:
    """Table alias -> table name for every aliased table in the statement (upper case)."""
    out = {}
    for t in tree.find_all(exp.Table):
        if t.alias:
            out[t.alias.upper()] = t.name.upper()
    return out


def _fix_bool(tree, bool_cols: set[tuple[str, str]]):
    """`flag = 1` -> `flag = TRUE` for columns that are BOOLEAN in Unity Catalog. Aliases (`FISCAL_PERIODS fp`)
    are resolved to their table so `fp.IS_CLOSED = 1` is fixed too."""
    aliases = _aliases(tree)

    def fix(node):
        if isinstance(node, (exp.EQ, exp.NEQ)):
            col, lit = node.this, node.expression
            if isinstance(lit, exp.Column):
                col, lit = lit, col
            if (isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and not lit.is_string
                    and (aliases.get(col.table.upper(), col.table.upper()), col.name.upper()) in bool_cols
                    and lit.this in ("0", "1")):
                truth = exp.Boolean(this=lit.this == "1")
                return node.__class__(this=col.copy(), expression=truth)
        return node
    return tree.transform(fix)


def _decode_to_case(tree):
    """Oracle DECODE(x, s1, r1, ..., default) -> CASE WHEN x = s1 THEN r1 ... ELSE default END.
    Oracle's DECODE matches NULL to NULL; CASE with `=` does not. So a NULL search value becomes `x IS NULL`, and
    a search value that isn't a literal (a column, an expression) uses null-safe `x <=> s`."""
    def cond(subject, search):
        if isinstance(search, exp.Null):
            return exp.Is(this=subject.copy(), expression=exp.Null())
        if isinstance(search, (exp.Literal, exp.Boolean)):
            return exp.EQ(this=subject.copy(), expression=search.copy())
        return exp.NullSafeEQ(this=subject.copy(), expression=search.copy())

    def conv(node):
        cls = getattr(exp, "DecodeCase", None)
        if cls is not None and isinstance(node, cls):
            args = list(node.expressions)
            subject, rest = args[0], args[1:]
            ifs = [exp.If(this=cond(subject, rest[i]), true=rest[i + 1].copy())
                   for i in range(0, len(rest) - 1, 2)]
            default = rest[-1].copy() if len(rest) % 2 == 1 else None
            return exp.Case(ifs=ifs, default=default)
        return node
    return tree.transform(conv)


def to_databricks(expr: str, src_dialect: str, table_names: dict[str, str] | None = None,
                  bool_cols: set[tuple[str, str]] | None = None, lower_columns: bool = True) -> tuple[str | None, list[str], str | None]:
    """Transpile one expression. Returns (sql, rule_ids, error).

    table_names maps BO table/alias names to the names to use (Ossie dataset names); unmapped tables stay as is.
    """
    tree, err = parse(expr, src_dialect)
    if tree is None:
        return None, [], err
    rules = []
    if src_dialect.lower() != "databricks":
        rules.append(f"transpile_{src_dialect.lower()}")
        before = tree.sql()
        tree = _decode_to_case(tree)
        if tree.sql() != before:
            rules.append("decode_to_case")
    if bool_cols:
        before = tree.sql()
        tree = _fix_bool(tree, bool_cols)
        if tree.sql() != before:
            rules.append("bool_flag")
    table_names = {k.upper(): v for k, v in (table_names or {}).items()}

    def rename(node):
        if isinstance(node, exp.Column) and node.table:
            t = table_names.get(node.table.upper(), node.table)
            name = node.name.lower() if lower_columns else node.name
            return exp.column(name, table=t)
        return node
    tree = tree.transform(rename)
    out = tree.sql(dialect="databricks")
    return out, rules, None


def transpile_statement(sql: str, src_dialect: str, uc_tables: dict[str, str], schema: str | None = None,
                        bool_cols: set[tuple[str, str]] | None = None) -> tuple[str | None, str | None]:
    """Re-point a captured report statement at Unity Catalog and transpile it to Databricks SQL (for L1
    reconciliation). uc_tables maps BO table names (not aliases) to three-part UC names."""
    tree, err = parse(sql, src_dialect)
    if tree is None:
        return None, err
    tree = _decode_to_case(tree)
    if bool_cols:
        tree = _fix_bool(tree, bool_cols)
    uc = {k.upper(): v for k, v in uc_tables.items() if v}

    def repoint(node):
        if isinstance(node, exp.Table) and node.name.upper() in uc:
            alias = node.alias or node.name
            cat, sch, tbl = uc[node.name.upper()].split(".")
            return exp.Table(this=exp.to_identifier(tbl), db=exp.to_identifier(sch), catalog=exp.to_identifier(cat),
                             alias=exp.TableAlias(this=exp.to_identifier(alias)))
        return node
    tree = tree.transform(repoint)
    return tree.sql(dialect="databricks", pretty=True), None


def join_pairs(expr: str, dialect: str = "oracle") -> tuple[list[tuple[tuple[str, str], tuple[str, str]]], list[str], str | None]:
    """Split a join expression into equality column pairs and leftover predicates.
    Returns (pairs, extra_predicates, error); pairs are ((T1, C1), (T2, C2))."""
    tree, err = parse(expr, dialect)
    if tree is None:
        return [], [], err
    conj = list(tree.flatten()) if isinstance(tree, exp.And) else [tree]
    pairs, extra = [], []
    for c in conj:
        if (isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and isinstance(c.expression, exp.Column)
                and c.this.table and c.expression.table):
            pairs.append(((c.this.table.upper(), c.this.name.upper()),
                          (c.expression.table.upper(), c.expression.name.upper())))
        else:
            extra.append(c.sql(dialect=dialect))
    return pairs, extra, None


def normalize_hash(expr: str, dialect: str = "databricks") -> str:
    """A canonical text for comparing two expressions (used for duplicate detection)."""
    tree, _ = parse(expr, dialect)
    if tree is None:
        return re.sub(r"\s+", "", (expr or "").lower())
    return tree.sql(dialect="databricks", normalize=True, comments=False).lower().replace(" ", "")


def rewrite(expr: str, src_dialect: str, resolver, bool_cols: set[tuple[str, str]] | None = None,
            out_dialect: str = "databricks") -> tuple[str | None, list[str], str | None]:
    """Transpile an expression and rewrite every qualified column with resolver(TABLE, COLUMN) -> (qualifier, name)
    (qualifier None = unqualified). Returns (sql, rule_ids, error). resolver may raise KeyError for an unknown
    column; the error is returned."""
    tree, err = parse(expr, src_dialect)
    if tree is None:
        return None, [], err
    rules = []
    if src_dialect.lower() != "databricks":
        rules.append(f"transpile_{src_dialect.lower()}")
        before = tree.sql()
        tree = _decode_to_case(tree)
        if tree.sql() != before:
            rules.append("decode_to_case")
    if bool_cols:
        before = tree.sql()
        tree = _fix_bool(tree, bool_cols)
        if tree.sql() != before:
            rules.append("bool_flag")
    missing = []

    def ren(node):
        if isinstance(node, exp.Column) and node.table:
            try:
                q, n = resolver(node.table.upper(), node.name.upper())
            except KeyError as e:
                missing.append(str(e).strip("'"))
                return node
            return exp.column(n, table=q) if q else exp.column(n)
        return node
    tree = tree.transform(ren)
    if missing:
        return None, rules, "unresolved column(s): " + ", ".join(dict.fromkeys(missing))
    return tree.sql(dialect=out_dialect), rules, None


def filter_form(agg_expr: str, condition: str) -> tuple[str, str]:
    """SUM(x) + condition -> (Databricks 'SUM(x) FILTER (WHERE c)', ANSI 'SUM(CASE WHEN c THEN x END)')."""
    tree, err = parse(agg_expr, "databricks")
    if tree is None or not isinstance(tree, exp.AggFunc):
        return f"{agg_expr} FILTER (WHERE {condition})", agg_expr
    inner = tree.this
    if isinstance(inner, exp.Distinct):
        arg = inner.expressions[0].sql(dialect="databricks")
        ansi = f"{tree.key.upper()}(DISTINCT CASE WHEN {condition} THEN {arg} END)"
    else:
        ansi = f"{tree.key.upper()}(CASE WHEN {condition} THEN {inner.sql(dialect='databricks')} END)"
    return f"{tree.sql(dialect='databricks')} FILTER (WHERE {condition})", ansi
