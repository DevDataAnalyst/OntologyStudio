"""
Relationship discovery when foreign keys are not declared (the normal case in
Unity Catalog, where PK/FK constraints are informational and often missing).

Evidence, combined into one confidence score (0–1) per candidate join:
  declared FK            -> 1.00 (always accepted)
  naming                 -> fact.customer_key = dim_customer.customer_key, order.cust_id -> customers.id …
  same key               -> customer_ext.customer_id = customers.customer_id: one row per customer in both
                            (Auto Ontology's vertical split), a one-to-one join to the larger table
  type compatibility     -> penalty when numeric vs string
  value overlap          -> share of distinct child values found in the parent key (sampled SQL check)
  query history          -> how often people actually join these columns (system.query.history)

Candidates at/above the threshold become relationships; the rest are kept as
*suggestions* that the user accepts or rejects in the network diagram.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

from . import profiling
from .schema_model import Column, DatabaseSchema, ForeignKey, Table

_KEY_SUFFIX = r"(?:_?(?:id|key|sk|fk|code|no|num|nbr))$"
_NUMERIC = re.compile(r"INT|DEC|NUM|DOUBLE|FLOAT|REAL|LONG|SHORT|BYTE", re.I)
_TEXT = re.compile(r"CHAR|STRING|TEXT|UUID", re.I)
_GENERIC_KEYS = {"id", "pk", "key", "code", "uuid", "guid", "rowid"}       # every surrogate key is called so
_LABEL_WORDS = {"name", "title", "label", "description", "desc", "comment", "note", "notes"}
# layer, variant and copy markers: customers, stg_customers and customers_2024 are copies, not parts, of one entity
_COPY = re.compile(r"^(?:dim|fact|fct|tbl|stg|vw|lkp|ref|raw|src|bronze|silver|gold|old|new|tmp|temp|bak|backup|"
                   r"archive|hist|history)_|_(?:dim|fact|fct|tbl|lkp|lookup|old|new|tmp|temp|bak|backup|archive|arch|"
                   r"hist|history|snapshot|snap|copy|staging|stg|deleted|v?\d+)$", re.I)


def _copy_stem(t: Table) -> str:
    name, prev = t.name.lower(), None
    while name != prev:
        name, prev = _COPY.sub("", name), name
    return _singular(name)


def tkey(t: Table) -> str:
    return t.full_name or t.name


def _singular(w: str) -> str:
    from .ontology_builder import singular
    return singular(w)


def _entity_words(t: Table) -> set[str]:
    """Names a table's entity is referred to by: dim_customer -> {customer, customers, cust}."""
    base = re.sub(r"^(dim|d|fact|f|fct|tbl|t|stg|vw|v)_", "", t.name.lower())
    base = re.sub(r"_(dim|fact|tbl|table|master|mstr|lkp|lookup|ref)$", "", base)
    words = {base, _singular(base)}
    words |= {w[:4] for w in list(words) if len(w) > 5}          # customer -> cust
    return {w for w in words if w}


def ambiguous(c: Column) -> bool:
    """Whether matching this column's values to another key proves nothing (Auto Ontology's value-search guard):
    a label column, or values all shorter than 3 characters, which sit inside any surrogate key (1, 2, 3 ...).
    With no values to look at (personal data is never sampled) a column with under 100 distinct values counts."""
    words = set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", c.name).lower()))
    if words & _LABEL_WORDS:
        return True
    vals = [v for v in [*(c.samples or []), c.min_value, c.max_value] if v is not None]
    if vals:
        return all(len(str(v)) < 3 for v in vals)
    return (c.distinct_count or 0) < 100


def _type_family(sql_type: str) -> str:
    if _NUMERIC.search(sql_type or ""):
        return "num"
    if _TEXT.search(sql_type or ""):
        return "text"
    return "other"


# --------------------------------------------------------------------------- #
# 1. Primary keys (needed as join targets)
# --------------------------------------------------------------------------- #
def infer_primary_keys(ds: DatabaseSchema) -> list[str]:
    notes = []
    for t in ds.tables:
        if t.primary_key or t.is_view or not t.row_count:
            continue
        words = _entity_words(t)
        best, best_score = None, 0
        for i, c in enumerate(t.columns):
            if c.distinct_count is None or c.non_null_count is None:
                continue
            unique = (c.non_null_count == t.row_count
                      and c.distinct_count >= 0.98 * t.row_count)   # approx_count_distinct tolerance
            if not unique:
                continue
            n = c.name.lower()
            score = 1
            if re.search(_KEY_SUFFIX, n):
                score += 2
            if n in ("id", "key") or any(n.startswith(w) for w in words):
                score += 2
            if i == 0:
                score += 1
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= 3:
            t.primary_key = [best.name]
            best.primary_key = best.unique = True
            best.nullable = False
            t.pk_inferred = True
            notes.append(f"{tkey(t)}: primary key inferred as {best.name} (unique, non-null)")
    return notes


# --------------------------------------------------------------------------- #
# 2. Candidates from naming
# --------------------------------------------------------------------------- #
def name_candidates(ds: DatabaseSchema) -> dict:
    cands: dict[tuple, dict] = {}
    targets = [t for t in ds.tables if len(t.primary_key) == 1]
    declared = {(tkey(t), tuple(c.lower() for c in fk.columns)) for t in ds.tables
                for fk in t.foreign_keys}
    for a in ds.tables:
        for c in a.columns:
            n = c.name.lower()
            if (c.primary_key and len(a.primary_key) == 1) or (tkey(a), (n,)) in declared:
                continue
            stem = re.sub(_KEY_SUFFIX, "", n)
            for b in targets:
                pk = b.column(b.primary_key[0])
                pkn = pk.name.lower()
                score, why = 0.0, ""
                if n == pkn and pkn not in ("id", "key", "code"):
                    score, why = 0.6, f"same column name as {b.name} key"
                elif stem and stem in _entity_words(b) and re.search(_KEY_SUFFIX, n):
                    score, why = 0.55, f"'{c.name}' names entity '{b.name}'"
                elif n.endswith("_" + pkn) and len(pkn) > 3:
                    score, why = 0.4, f"'{c.name}' ends with key '{pk.name}'"
                if not score:
                    continue
                if a is b and not why.startswith("'"):
                    continue
                fa, fb = _type_family(c.sql_type), _type_family(pk.sql_type)
                if fa != fb and "other" not in (fa, fb):
                    score -= 0.3
                    why += " (type mismatch)"
                key = (tkey(a), c.name, tkey(b), pk.name)
                cands[key] = {"from_table": tkey(a), "from_cols": [c.name], "to_table": tkey(b),
                              "to_cols": [pk.name], "name_score": score, "evidence": [why]}
    _same_key_candidates(ds, targets, cands)
    return cands


def _same_key_candidates(ds: DatabaseSchema, targets: list[Table], cands: dict) -> None:
    """Tables keyed by the same specific column (customer_id in customers and customer_scores) hold one row per
    entity each: join every one to the largest of them, one-to-one. A generic key name (id, key, code ...) is
    never enough, copies of one table (stg_customers, customers_2024) are left apart, and short or label-like key
    values earn no credit from the overlap check (ambiguous)."""
    linked = set()
    for t in ds.tables:
        for fk in t.foreign_keys:
            if (r := ds.table(fk.ref_table)) is not None:
                linked.add(frozenset((tkey(t), tkey(r))))
    groups: dict[str, list[Table]] = defaultdict(list)
    for t in targets:
        if not t.is_view and t.primary_key[0].lower() not in _GENERIC_KEYS:
            groups[t.primary_key[0].lower()].append(t)
    for ts in groups.values():
        if len(ts) < 2:
            continue
        base = min(ts, key=lambda t: (-(t.row_count or 0), tkey(t).lower()))   # most rows, then first by name
        kb = base.column(base.primary_key[0])
        for a in ts:
            if a is base or frozenset((tkey(a), tkey(base))) in linked or _copy_stem(a) == _copy_stem(base):
                continue
            ka = a.column(a.primary_key[0])
            cand = {"from_table": tkey(a), "from_cols": [ka.name], "to_table": tkey(base), "to_cols": [kb.name],
                    "name_score": 0.6, "evidence": [f"same key as {base.name}: one row per {ka.name} in both"]}
            fa, fb = _type_family(ka.sql_type), _type_family(kb.sql_type)
            if fa != fb and "other" not in (fa, fb):
                cand["name_score"] -= 0.3
                cand["evidence"][0] += " (type mismatch)"
            if ambiguous(ka) or ambiguous(kb):
                cand["ambiguous"] = True
            cands[(tkey(a), ka.name, tkey(base), kb.name)] = cand


# --------------------------------------------------------------------------- #
# 3. Query-history evidence
# --------------------------------------------------------------------------- #
def add_query_evidence(ds: DatabaseSchema, cands: dict, joins) -> None:
    for (ta, ca, tb, cb), n in (joins or {}).items():
        A, B = ds.table(ta), ds.table(tb)
        if not A or not B:
            continue
        # orient: the side whose column is the (single) primary key is the target
        a_pk = [x.lower() for x in A.primary_key] == [ca.lower()]
        b_pk = [x.lower() for x in B.primary_key] == [cb.lower()]
        if a_pk and not b_pk:
            A, ca, B, cb = B, cb, A, ca
        elif not b_pk and not a_pk:
            ca_d = (A.column(ca).distinct_count if A.column(ca) else None) or 0
            cb_d = (B.column(cb).distinct_count if B.column(cb) else None) or 0
            if ca_d > cb_d:      # fewer distinct values on the many side
                A, ca, B, cb = B, cb, A, ca
        key = (tkey(A), ca, tkey(B), cb)
        cand = cands.setdefault(key, {"from_table": tkey(A), "from_cols": [ca], "to_table": tkey(B),
                                      "to_cols": [cb], "name_score": 0.0, "evidence": []})
        cand["query_count"] = cand.get("query_count", 0) + n
        cand["evidence"].append(f"joined in {n} recent quer{'y' if n == 1 else 'ies'}")


# --------------------------------------------------------------------------- #
# 4. Value-overlap verification
# --------------------------------------------------------------------------- #
def verify_overlap(run_sql, ds: DatabaseSchema, cands: dict, table_ref, quote,
                   max_checks: int = 80, sample: int = 5000) -> list[str]:
    """run_sql(sql) -> DataFrame; table_ref(Table) -> SQL name; quote(col) -> quoted column."""
    notes, fails = [], 0
    ranked = sorted(cands.values(), key=lambda c: -(c["name_score"] + 0.1 * c.get("query_count", 0)))
    for cand in ranked[:max_checks]:
        if fails >= profiling.MAX_TRANSIENT:            # the warehouse stopped answering: stop probing it
            notes.append(f"overlap checks failed {fails} times in a row (the SQL warehouse did not answer); "
                         "the remaining checks were skipped")
            break
        A, B = ds.table(cand["from_table"]), ds.table(cand["to_table"])
        if not A or not B or A.is_view or B.is_view:
            continue
        qa, qb = quote(cand["from_cols"][0]), quote(cand["to_cols"][0])
        sql = (f"SELECT /* ontology-studio */ COUNT(*) AS n, SUM(CASE WHEN b.v IS NOT NULL THEN 1 ELSE 0 END) AS m FROM "
               f"(SELECT DISTINCT {qa} AS v FROM {table_ref(A)} WHERE {qa} IS NOT NULL LIMIT {sample}) a "
               f"LEFT JOIN (SELECT DISTINCT {qb} AS v FROM {table_ref(B)}) b ON a.v = b.v")
        try:
            row = run_sql(sql).iloc[0]
            n, m = int(row["n"] or 0), int(row["m"] or 0)
        except Exception as e:  # noqa: BLE001
            notes.append(f"overlap check failed for {A.name}.{qa} → {B.name}: {str(e)[:100]}")
            fails += profiling.transient(e)
            continue
        fails = 0
        if n:
            cand["coverage"] = round(m / n, 3)
            cand["evidence"].append(f"{cand['coverage']:.0%} of {n} distinct values found in "
                                    f"{B.name}.{cand['to_cols'][0]}"
                                    + (" (short or label-like values: not counted as proof)"
                                       if cand.get("ambiguous") and cand["coverage"] >= 0.7 else ""))
    return notes


# --------------------------------------------------------------------------- #
# 5. Score + split into accepted relationships and suggestions
# --------------------------------------------------------------------------- #
def score(c: dict) -> float:
    s = c.get("name_score", 0.0)
    if c.get("query_count"):
        s = max(s, 0.45) + min(0.3, 0.1 * math.log2(1 + c["query_count"]))
    cov = c.get("coverage")
    if cov is not None and not (c.get("ambiguous") and cov >= 0.7):     # ambiguous overlap is no proof
        s += 0.35 if cov >= 0.98 else 0.25 if cov >= 0.9 else 0.1 if cov >= 0.7 else -0.5
    return round(max(0.0, min(0.99, s)), 2)


def apply(ds: DatabaseSchema, cands: dict, threshold: float = 0.75) -> list[dict]:
    """Adds accepted candidates as ForeignKeys; returns the remaining suggestions."""
    suggestions = []
    by_column: dict[tuple, dict] = {}
    for c in cands.values():                      # one best target per child column
        c["confidence"] = score(c)
        k = (c["from_table"], c["from_cols"][0].lower())
        if k not in by_column or c["confidence"] > by_column[k]["confidence"]:
            by_column[k] = c
    pairs: dict[frozenset, dict] = {}                # two key columns joined both ways are one relationship
    for c in by_column.values():
        k = frozenset(((c["from_table"], c["from_cols"][0].lower()), (c["to_table"], c["to_cols"][0].lower())))
        if k not in pairs or c["confidence"] > pairs[k]["confidence"]:
            pairs[k] = c
    for i, c in enumerate(sorted(pairs.values(), key=lambda x: -x["confidence"])):
        if c["confidence"] < 0.3:
            continue
        if c["confidence"] >= threshold:
            t = ds.table(c["from_table"])
            t.foreign_keys.append(ForeignKey(c["from_cols"], c["to_table"], c["to_cols"],
                                             name=f"discovered:{c['confidence']}"))
        else:
            suggestions.append({"id": f"s{i}", "status": "proposed", **{
                k: c[k] for k in ("from_table", "from_cols", "to_table", "to_cols",
                                  "confidence", "evidence")}})
    return suggestions


def discover(ds: DatabaseSchema, *, run_sql=None, table_ref=None, quote=None, joins=None,
             verify: bool = True, threshold: float = 0.75) -> tuple[list[dict], list[str]]:
    notes = infer_primary_keys(ds)
    cands = name_candidates(ds)
    add_query_evidence(ds, cands, joins)
    if verify and run_sql is not None:
        notes += verify_overlap(run_sql, ds, cands, table_ref, quote)
    suggestions = apply(ds, cands, threshold)
    return suggestions, notes
