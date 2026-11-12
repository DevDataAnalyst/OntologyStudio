"""
Unity Catalog: browse objects, extract any selection of tables/views (across
catalogs and schemas), profile them, and read lineage + query-history joins.

Every function takes `run(sql, params) -> DataFrame` (see db_connection.make_runner),
so it runs with the viewer's permissions. Named parameters (:p0, :p1 …) are used
for all values; identifiers are validated and back-quoted.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .schema_model import Column, DatabaseSchema, ForeignKey, Table

MAX_TABLES = 100
_IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")
_COMPLEX = re.compile(r"ARRAY|MAP|STRUCT|VARIANT|BINARY|GEOMETRY|GEOGRAPHY", re.I)


def _check(*names: str) -> None:
    for n in names:
        if not _IDENT.match(n or ""):
            raise ValueError(f"Invalid Unity Catalog name: {n!r}")


def bq(*parts: str) -> str:
    return ".".join(f"`{p}`" for p in parts)


def _in(values, prefix="p"):
    """Build an IN-list of named markers + params dict."""
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return ", ".join(f":{k}" for k in params), params


# --------------------------------------------------------------------------- #
# Browse
# --------------------------------------------------------------------------- #
def list_catalogs(run) -> list[str]:
    df = run("SELECT catalog_name FROM system.information_schema.catalogs ORDER BY 1")
    return [c for c in df["catalog_name"] if c != "system"] + ["system"]


def list_schemas(run, catalog: str) -> list[str]:
    _check(catalog)
    df = run(f"SELECT schema_name FROM {bq(catalog)}.information_schema.schemata "
             f"WHERE schema_name <> 'information_schema' ORDER BY 1")
    return df["schema_name"].tolist()


def list_tables(run, catalog: str, schema: str):
    _check(catalog, schema)
    return run(f"SELECT table_name, table_type, comment FROM {bq(catalog)}.information_schema.tables "
               f"WHERE table_schema = :s ORDER BY table_name", {"s": schema})


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #
def extract_objects(run, full_names: list[str], profile: bool = True, enum_max: int = 12,
                    workers: int = 8, progress=None) -> DatabaseSchema:
    """full_names: ['catalog.schema.table', ...] (max 100)."""
    full_names = list(dict.fromkeys(full_names))[:MAX_TABLES]
    groups: dict[tuple, list[str]] = defaultdict(list)
    for fq in full_names:
        cat, sch, tbl = fq.split(".")
        _check(cat, sch, tbl)
        groups[(cat, sch)].append(tbl)

    schemas = sorted({s for _, s in groups})
    ds = DatabaseSchema(name=", ".join(f"{c}.{s}" for c, s in groups),
                        source_type="unity_catalog", dialect="databricks")
    tables: dict[str, Table] = {}            # full name -> Table
    fk_rows = []                             # (fq_table, constraint, catalog)

    for (cat, sch), names in groups.items():
        isc = f"{bq(cat)}.information_schema"
        marks, params = _in(names)
        params["s"] = sch
        tdf = run(f"SELECT table_name, table_type, comment FROM {isc}.tables "
                  f"WHERE table_schema = :s AND table_name IN ({marks})", params)
        cdf = run(f"SELECT table_name, column_name, full_data_type, is_nullable, column_default, comment "
                  f"FROM {isc}.columns WHERE table_schema = :s AND table_name IN ({marks}) "
                  f"ORDER BY table_name, ordinal_position", params)
        kdf = run(f"SELECT tc.table_name, tc.constraint_name, tc.constraint_type, k.column_name, "
                  f"k.ordinal_position FROM {isc}.table_constraints tc "
                  f"JOIN {isc}.key_column_usage k ON k.constraint_catalog = tc.constraint_catalog "
                  f"AND k.constraint_schema = tc.constraint_schema AND k.constraint_name = tc.constraint_name "
                  f"WHERE tc.table_schema = :s AND tc.table_name IN ({marks}) "
                  f"ORDER BY tc.table_name, tc.constraint_name, k.ordinal_position", params)
        rdf = run(f"SELECT r.constraint_name, r.unique_constraint_catalog AS rc, "
                  f"r.unique_constraint_schema AS rs, r.unique_constraint_name AS rn "
                  f"FROM {isc}.referential_constraints r WHERE r.constraint_schema = :s", {"s": sch})

        for r in tdf.itertuples(index=False):
            fq = f"{cat}.{sch}.{r.table_name}"
            short = r.table_name if len(schemas) == 1 and len(groups) == 1 else r.table_name
            tables[fq] = Table(name=short, full_name=fq, comment=r.comment or None,
                               table_type=str(r.table_type),
                               is_view="VIEW" in str(r.table_type).upper())
        for r in cdf.itertuples(index=False):
            if t := tables.get(f"{cat}.{sch}.{r.table_name}"):
                t.columns.append(Column(name=r.column_name, sql_type=str(r.full_data_type).upper(),
                                        nullable=str(r.is_nullable).upper() == "YES",
                                        default=r.column_default, comment=r.comment or None))
        refs = {r.constraint_name: (r.rc, r.rs, r.rn) for r in rdf.itertuples(index=False)}
        grouped: dict[tuple, list] = defaultdict(list)
        ctype = {}
        for r in kdf.itertuples(index=False):
            grouped[(r.table_name, r.constraint_name)].append(r.column_name)
            ctype[(r.table_name, r.constraint_name)] = str(r.constraint_type).upper()
        for (tname, cname), cols in grouped.items():
            t = tables.get(f"{cat}.{sch}.{tname}")
            kind = ctype[(tname, cname)]
            if not t:
                continue
            if kind == "PRIMARY KEY":
                t.primary_key = cols
            elif kind == "UNIQUE":
                t.unique_sets.append(cols)
            elif kind == "FOREIGN KEY" and cname in refs:
                fk_rows.append((t, cols, refs[cname]))

    # resolve FK targets (they may live in another schema/catalog)
    pk_cache: dict[tuple, tuple[str, list[str]]] = {}
    for t, cols, (rc, rs, rn) in fk_rows:
        key = (rc, rs, rn)
        if key not in pk_cache:
            try:
                _check(rc, rs)
                df = run(f"SELECT table_name, column_name FROM {bq(rc)}.information_schema.key_column_usage "
                         f"WHERE constraint_schema = :s AND constraint_name = :n ORDER BY ordinal_position",
                         {"s": rs, "n": rn})
                pk_cache[key] = (f"{rc}.{rs}.{df['table_name'].iloc[0]}", df["column_name"].tolist())
            except Exception as e:  # noqa: BLE001
                ds.warnings.append(f"{t.full_name}: could not resolve FK target {rn} ({e})")
                continue
        ref_fq, ref_cols = pk_cache[key]
        t.foreign_keys.append(ForeignKey(cols, ref_fq, ref_cols, name="declared"))
        if ref_fq not in tables:
            ds.warnings.append(f"{t.full_name} references {ref_fq}, which is not in the selection.")

    # make short names unique across schemas
    counts = Counter(t.name.lower() for t in tables.values())
    for fq, t in tables.items():
        if counts[t.name.lower()] > 1:
            _, sch, tbl = fq.split(".")
            t.name = f"{sch}_{tbl}"
    ds.tables = list(tables.values())

    missing = set(full_names) - set(tables)
    if missing:
        ds.warnings.append(f"Not found or no permission: {', '.join(sorted(missing))}")

    if profile:
        todo = [t for t in ds.tables if not t.is_view]

        def work(t):
            # runs in a worker thread: SQL only — never touch Streamlit here
            try:
                _profile_table(run, t, enum_max)
                return None
            except Exception as e:  # noqa: BLE001
                return f"{t.full_name}: profiling skipped ({str(e)[:160]})"

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, t) for t in todo]
            # progress is reported from the calling (Streamlit script) thread
            for i, fut in enumerate(as_completed(futures), 1):
                err = fut.result()
                if err:
                    ds.warnings.append(err)
                if progress:
                    progress(i / max(1, len(todo)), f"Profiled {i}/{len(todo)} tables")
    return ds.finalize()


def _profile_table(run, t: Table, enum_max: int) -> None:
    fq = bq(*t.full_name.split("."))
    cols = [c for c in t.columns if not _COMPLEX.search(c.sql_type)][:120]
    if not cols:
        t.row_count = int(run(f"SELECT /* ontology-studio */ COUNT(*) AS n FROM {fq}").iloc[0, 0])
        return
    sel = ", ".join([f"COUNT(*) AS `__n`"]
                    + [f"approx_count_distinct(`{c.name}`) AS `d{i}`" for i, c in enumerate(cols)]
                    + [f"COUNT(`{c.name}`) AS `nn{i}`" for i, c in enumerate(cols)])
    stats = run(f"SELECT /* ontology-studio */ {sel} FROM {fq}").iloc[0]
    t.row_count = int(stats["__n"])
    if not t.row_count:
        return
    for i, c in enumerate(cols):
        c.distinct_count = int(stats[f"d{i}"])
        c.non_null_count = int(stats[f"nn{i}"])
    sample = run(f"SELECT /* ontology-studio */ {', '.join(f'`{c.name}`' for c in cols)} FROM {fq} LIMIT 200")
    for c in cols:
        c.samples = sample[c.name].dropna().drop_duplicates().head(5).tolist()
    enum_cols = [c for c in cols if c.distinct_count and c.distinct_count <= enum_max
                 and re.search(r"STRING|CHAR", c.sql_type)]
    if enum_cols:
        sel = ", ".join(f"array_sort(collect_set(`{c.name}`)) AS `e{i}`" for i, c in enumerate(enum_cols))
        row = run(f"SELECT /* ontology-studio */ {sel} FROM {fq}").iloc[0]
        for i, c in enumerate(enum_cols):
            vals = list(row[f"e{i}"]) if row[f"e{i}"] is not None else []
            if 0 < len(vals) <= enum_max:
                c.distinct_values = vals


# --------------------------------------------------------------------------- #
# Lineage (system.access.table_lineage / column_lineage)
# --------------------------------------------------------------------------- #
def read_lineage(run, full_names: list[str], days: int = 180) -> dict:
    marks, params = _in(full_names)
    params["d"] = int(days)
    out = {"tables": [], "columns": [], "warnings": []}
    try:
        df = run(
            "SELECT source_table_full_name AS src, target_table_full_name AS tgt, "
            "first(source_type) AS src_type, first(target_type) AS tgt_type, "
            "COUNT(*) AS events, CAST(MAX(event_time) AS STRING) AS last_seen "
            "FROM system.access.table_lineage "
            "WHERE event_date >= date_sub(current_date(), :d) "
            "AND source_table_full_name IS NOT NULL AND target_table_full_name IS NOT NULL "
            "AND source_table_full_name <> target_table_full_name "
            f"AND (source_table_full_name IN ({marks}) OR target_table_full_name IN ({marks})) "
            "GROUP BY source_table_full_name, target_table_full_name", params)
        out["tables"] = df.to_dict("records")
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"Table lineage unavailable ({str(e)[:160]}). "
                               "Needs SELECT on system.access.table_lineage.")
        return out
    try:
        df = run(
            "SELECT source_table_full_name AS src, source_column_name AS src_col, "
            "target_table_full_name AS tgt, target_column_name AS tgt_col, COUNT(*) AS events "
            "FROM system.access.column_lineage "
            "WHERE event_date >= date_sub(current_date(), :d) "
            "AND source_column_name IS NOT NULL AND target_column_name IS NOT NULL "
            f"AND (source_table_full_name IN ({marks}) OR target_table_full_name IN ({marks})) "
            "GROUP BY 1, 2, 3, 4", params)
        out["columns"] = df.to_dict("records")
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"Column lineage unavailable ({str(e)[:160]}).")
    return out


# --------------------------------------------------------------------------- #
# Query history: joins people actually run (system.query.history)
# --------------------------------------------------------------------------- #
def read_query_joins(run, full_names: list[str], days: int = 90, limit: int = 3000) -> dict:
    """Returns {"joins": Counter{(fqA, colA, fqB, colB): n}, "warnings": [...], "queries": n}."""
    shorts = sorted({fq.split(".")[-1].lower() for fq in full_names})
    pattern = "(" + "|".join(re.escape(s) for s in shorts) + ")"
    out = {"joins": Counter(), "warnings": [], "queries": 0}
    try:
        df = run(
            "SELECT statement_text FROM system.query.history "
            "WHERE start_time >= current_timestamp() - make_dt_interval(:d) "
            "AND execution_status = 'FINISHED' AND statement_type = 'SELECT' "
            "AND lower(statement_text) LIKE '%join%' AND lower(statement_text) RLIKE :pat "
            f"LIMIT {int(limit)}", {"d": int(days), "pat": pattern})
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"Query history unavailable ({str(e)[:160]}). "
                               "Needs SELECT on system.query.history.")
        return out
    texts = [t for t in df["statement_text"].tolist() if t]
    if texts and all(str(t).strip().lower() == "<redacted>" for t in texts[:20]):
        out["warnings"].append("Query text is redacted. Only account admins and members of the "
                               "databricks_pii_access group can read it, so join mining was skipped.")
        return out
    out["queries"] = len(texts)
    out["joins"] = mine_joins(texts, full_names)
    return out


def mine_joins(sql_texts, full_names: list[str], dialect: str = "databricks") -> Counter:
    """Parse SQL and count equality join conditions between the selected tables."""
    import logging

    import sqlglot
    from sqlglot import exp
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

    by_short: dict[str, list[str]] = defaultdict(list)
    for fq in full_names:
        by_short[fq.split(".")[-1].lower()].append(fq)
    known = {fq.lower(): fq for fq in full_names}

    def resolve(tbl: exp.Table) -> str | None:
        parts = [p for p in (tbl.catalog, tbl.db, tbl.name) if p]
        full = ".".join(parts).lower()
        if full in known:
            return known[full]
        cands = [fq for fq in by_short.get(tbl.name.lower(), [])
                 if not tbl.db or fq.lower().split(".")[1] == tbl.db.lower()]
        return cands[0] if len(cands) == 1 else None

    counts: Counter = Counter()
    for text in sql_texts:
        try:
            trees = sqlglot.parse(str(text), read=dialect)
        except Exception:  # noqa: BLE001
            continue
        for tree in trees:
            if tree is None:
                continue
            for select in tree.find_all(exp.Select):
                aliases: dict[str, str] = {}
                for tbl in select.find_all(exp.Table):
                    fq = resolve(tbl)
                    if fq:
                        aliases[(tbl.alias_or_name or tbl.name).lower()] = fq
                        aliases.setdefault(tbl.name.lower(), fq)
                for join in select.args.get("joins") or []:
                    on = join.args.get("on")
                    if on is None:
                        continue
                    for eq in on.find_all(exp.EQ):
                        l, r = eq.left, eq.right
                        if not (isinstance(l, exp.Column) and isinstance(r, exp.Column)):
                            continue
                        ta, tb = aliases.get(l.table.lower()), aliases.get(r.table.lower())
                        if ta and tb and ta != tb:
                            a, b = sorted([(ta, l.name), (tb, r.name)])
                            counts[(a[0], a[1], b[0], b[1])] += 1
    return counts
