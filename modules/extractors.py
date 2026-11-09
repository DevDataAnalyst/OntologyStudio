"""
Schema extractors. Each returns a `DatabaseSchema`.

- from_sqlite_file:     a binary SQLite database (.db / .sqlite / .sqlite3)
- from_sql_script:      a .sql DDL file or dump (any dialect sqlglot supports)
- from_unity_catalog:   a live Unity Catalog schema via information_schema
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile

from .schema_model import Column, DatabaseSchema, ForeignKey, Table

SQLITE_MAGIC = b"SQLite format 3\x00"


def is_sqlite_bytes(data: bytes) -> bool:
    return data[:16] == SQLITE_MAGIC


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# SQLite (binary file or in-memory connection)
# --------------------------------------------------------------------------- #
def _introspect_sqlite(conn: sqlite3.Connection, name: str, source_type: str,
                       profile: bool, enum_max: int) -> DatabaseSchema:
    schema = DatabaseSchema(name=name, source_type=source_type, dialect="sqlite")
    objs = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()

    for tname, ttype in objs:
        t = Table(name=tname, is_view=(ttype == "view"))
        for cid, cname, ctype, notnull, dflt, pk in conn.execute(
            f"PRAGMA table_info({_q(tname)})"
        ):
            t.columns.append(Column(
                name=cname, sql_type=(ctype or "").upper(), nullable=not notnull,
                primary_key=pk > 0, default=dflt,
            ))
        pk_cols = sorted(
            [(pk, c) for _, c, _, _, _, pk in conn.execute(f"PRAGMA table_info({_q(tname)})") if pk],
        )
        t.primary_key = [c for _, c in pk_cols]

        fks: dict[int, ForeignKey] = {}
        for row in conn.execute(f"PRAGMA foreign_key_list({_q(tname)})"):
            fid, _seq, ref_table, from_col, to_col = row[0], row[1], row[2], row[3], row[4]
            fk = fks.setdefault(fid, ForeignKey(columns=[], ref_table=ref_table, ref_columns=[]))
            fk.columns.append(from_col)
            if to_col:
                fk.ref_columns.append(to_col)
        t.foreign_keys = list(fks.values())

        for idx in conn.execute(f"PRAGMA index_list({_q(tname)})"):
            idx_name, is_unique, origin = idx[1], idx[2], idx[3] if len(idx) > 3 else ""
            if is_unique and origin != "pk":
                cols = [r[2] for r in conn.execute(f"PRAGMA index_info({_q(idx_name)})")]
                t.unique_sets.append(cols)

        if profile and not t.is_view:
            try:
                t.row_count = conn.execute(f"SELECT COUNT(*) FROM {_q(tname)}").fetchone()[0]
                if t.row_count:
                    for c in t.columns:
                        n = conn.execute(
                            f"SELECT COUNT(DISTINCT {_q(c.name)}) FROM {_q(tname)}"
                        ).fetchone()[0]
                        c.distinct_count = n
                        c.non_null_count = conn.execute(
                            f"SELECT COUNT({_q(c.name)}) FROM {_q(tname)}").fetchone()[0]
                        if n and not re.search(r"BLOB|BINARY", c.sql_type or ""):
                            c.samples = [
                                r[0] for r in conn.execute(
                                    f"SELECT DISTINCT {_q(c.name)} FROM {_q(tname)} "
                                    f"WHERE {_q(c.name)} IS NOT NULL LIMIT 5")
                            ]
                        if 0 < n <= enum_max:
                            c.distinct_values = [
                                r[0] for r in conn.execute(
                                    f"SELECT DISTINCT {_q(c.name)} FROM {_q(tname)} "
                                    f"WHERE {_q(c.name)} IS NOT NULL ORDER BY 1"
                                )
                            ]
            except sqlite3.Error as e:
                schema.warnings.append(f"{tname}: profiling skipped ({e})")
        schema.tables.append(t)
    return schema.finalize()


def from_sqlite_file(data: bytes, name: str, profile: bool = True,
                     enum_max: int = 12) -> DatabaseSchema:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _introspect_sqlite(conn, name, "sqlite", profile, enum_max)
        finally:
            conn.close()
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------- #
# SQL script (.sql)
# --------------------------------------------------------------------------- #
def from_sql_script(text: str, name: str, dialect: str | None = None,
                    profile: bool = True, enum_max: int = 12) -> DatabaseSchema:
    """
    Strategy:
      1. If no dialect (or 'sqlite') is given, try executing the script in an
         in-memory SQLite DB. This works for SQLite dumps and plain ANSI DDL,
         and gives data profiling for free when the dump contains INSERTs.
      2. Otherwise, or if that fails, parse the DDL with sqlglot (MySQL,
         Postgres, T-SQL, Oracle, Snowflake, Databricks, BigQuery, ...).
    """
    if dialect in (None, "", "auto", "sqlite"):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(text)
            schema = _introspect_sqlite(conn, name, "sql_script", profile, enum_max)
            if schema.tables:
                schema.dialect = "sqlite"
                return schema
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return _parse_with_sqlglot(text, name, None if dialect in (None, "", "auto") else dialect)


def _ident(node) -> str:
    return node.name if hasattr(node, "name") else str(node)


def _parse_with_sqlglot(text: str, name: str, dialect: str | None) -> DatabaseSchema:
    import sqlglot
    from sqlglot import exp

    import logging
    logging.getLogger("sqlglot").setLevel(logging.ERROR)

    schema = DatabaseSchema(name=name, source_type="sql_script", dialect=dialect or "generic")
    # T-SQL batch separators and psql meta-commands are not SQL
    text = re.sub(r"(?im)^\s*GO\s*;?\s*$", ";", text)
    text = re.sub(r"(?m)^\\.*$", "", text)
    statements = sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    comments: list = []
    pending_alters: list = []

    def ref_parts(ref):  # exp.Reference -> (table, [cols])
        target = ref.this
        if isinstance(target, exp.Schema):
            return _ident(target.this), [_ident(c) for c in target.expressions]
        return _ident(target), []

    for stmt in statements:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Alter):
            pending_alters.append(stmt)
            continue
        if isinstance(stmt, exp.Comment):
            comments.append(stmt)
            continue
        if isinstance(stmt, exp.Command):
            schema.warnings.append("Skipped unparseable statement: "
                                   + stmt.sql()[:80].replace("\n", " ") + "…")
            continue
        if not isinstance(stmt, exp.Create):
            continue
        kind = (stmt.args.get("kind") or "").upper()
        if kind not in ("TABLE", "VIEW"):
            continue
        target = stmt.this
        tbl_node = target.this if isinstance(target, exp.Schema) else target
        t = Table(name=_ident(tbl_node), is_view=(kind == "VIEW"))

        defs = target.expressions if isinstance(target, exp.Schema) else []
        for d in defs:
            if isinstance(d, exp.ColumnDef):
                col = Column(name=d.name, sql_type=(d.args["kind"].sql(dialect=dialect)
                                                    if d.args.get("kind") else ""))
                for cc in d.args.get("constraints") or []:
                    k = cc.args.get("kind")
                    if isinstance(k, exp.NotNullColumnConstraint):
                        col.nullable = bool(k.args.get("allow_null"))
                    elif isinstance(k, exp.PrimaryKeyColumnConstraint):
                        col.primary_key, col.nullable = True, False
                    elif isinstance(k, exp.UniqueColumnConstraint):
                        col.unique = True
                    elif isinstance(k, exp.DefaultColumnConstraint):
                        col.default = k.this.sql(dialect=dialect)
                    elif isinstance(k, exp.CommentColumnConstraint):
                        col.comment = k.this.name if hasattr(k.this, "name") else str(k.this)
                    elif isinstance(k, exp.Reference):
                        rt, rc = ref_parts(k)
                        t.foreign_keys.append(ForeignKey([col.name], rt, rc))
                t.columns.append(col)
            else:
                _apply_table_constraint(d, t, ref_parts, exp)

        for prop in (stmt.args.get("properties") or exp.Properties()).expressions:
            if isinstance(prop, exp.SchemaCommentProperty):
                t.comment = prop.this.name
        schema.tables.append(t)

    for alt in pending_alters:
        t = schema.table(_ident(alt.this))
        if not t:
            continue
        for action in alt.args.get("actions") or []:
            for node in [action, *action.find_all(exp.ForeignKey, exp.PrimaryKey)]:
                _apply_table_constraint(node, t, ref_parts, exp)

    for cm in comments:  # COMMENT ON TABLE x IS '...' / COMMENT ON COLUMN x.y IS '...'
        kind = str(cm.args.get("kind") or "").upper()
        text_val = cm.expression.name if cm.expression is not None else None
        target = cm.this
        if kind == "TABLE" and (t := schema.table(_ident(target))):
            t.comment = text_val
        elif kind == "COLUMN" and isinstance(target, exp.Column):
            t = schema.table(target.table)
            if t and (c := t.column(target.name)):
                c.comment = text_val

    if not schema.tables:
        schema.warnings.append("No CREATE TABLE statements could be parsed. "
                               "Try selecting the source dialect explicitly.")
    return schema.finalize()


def _apply_table_constraint(d, t: Table, ref_parts, exp) -> None:
    inner = d.expressions if isinstance(d, exp.Constraint) else [d]
    # T-SQL: CONSTRAINT pk PRIMARY KEY CLUSTERED (cols) / UNIQUE NONCLUSTERED (cols)
    kinds = {type(n).__name__ for n in inner}
    for n in inner:
        if type(n).__name__ in ("ClusteredColumnConstraint", "NonClusteredColumnConstraint"):
            cols = [c.this.name if isinstance(c, exp.Ordered) else _ident(c) for c in (n.this or [])]
            if "PrimaryKeyColumnConstraint" in kinds:
                t.primary_key = cols
            elif "UniqueColumnConstraint" in kinds:
                t.unique_sets.append(cols)
    for node in inner:
        if isinstance(node, exp.PrimaryKey):
            t.primary_key = [_ident(c) for c in node.expressions]
        elif isinstance(node, exp.ForeignKey):
            cols = [_ident(c) for c in node.expressions]
            ref = node.args.get("reference")
            if ref is not None:
                rt, rc = ref_parts(ref)
                if not any(fk.columns == cols and fk.ref_table == rt for fk in t.foreign_keys):
                    t.foreign_keys.append(ForeignKey(cols, rt, rc))
        elif isinstance(node, exp.UniqueColumnConstraint):
            s = node.this
            cols = [_ident(c) for c in s.expressions] if isinstance(s, exp.Schema) else []
            if cols:
                t.unique_sets.append(cols)


# --------------------------------------------------------------------------- #
# Unity Catalog — whole schema (kept for the CLI / backwards compatibility)
# --------------------------------------------------------------------------- #
def from_unity_catalog(run_query, catalog: str, schema_name: str, profile: bool = False,
                       enum_max: int = 12) -> DatabaseSchema:
    from . import uc
    tables = uc.list_tables(run_query, catalog, schema_name)["table_name"].tolist()
    return uc.extract_objects(run_query, [f"{catalog}.{schema_name}.{t}" for t in tables],
                              profile=profile, enum_max=enum_max)
