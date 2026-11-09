"""
Relationship discovery when foreign keys are not declared (the normal case in
Unity Catalog, where PK/FK constraints are informational and often missing).

Evidence, combined into one confidence score (0–1) per candidate join:
  declared FK            -> 1.00 (always accepted)
  naming                 -> fact.customer_key = dim_customer.customer_key, order.cust_id -> customers.id …
  type compatibility     -> penalty when numeric vs string
  value overlap          -> share of distinct child values found in the parent key (sampled SQL check)
  query history          -> how often people actually join these columns (system.query.history)

Candidates at/above the threshold become relationships; the rest are kept as
*suggestions* that the user accepts or rejects in the network diagram.
"""
from __future__ import annotations

import math
import re

from .schema_model import DatabaseSchema, ForeignKey, Table

_KEY_SUFFIX = r"(?:_?(?:id|key|sk|fk|code|no|num|nbr))$"
_NUMERIC = re.compile(r"INT|DEC|NUM|DOUBLE|FLOAT|REAL|LONG|SHORT|BYTE", re.I)
_TEXT = re.compile(r"CHAR|STRING|TEXT|UUID", re.I)


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
    return cands


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
    notes = []
    ranked = sorted(cands.values(), key=lambda c: -(c["name_score"] + 0.1 * c.get("query_count", 0)))
    for cand in ranked[:max_checks]:
        A, B = ds.table(cand["from_table"]), ds.table(cand["to_table"])
        if not A or not B or A.is_view or B.is_view:
            continue
        qa, qb = quote(cand["from_cols"][0]), quote(cand["to_cols"][0])
        sql = (f"SELECT COUNT(*) AS n, SUM(CASE WHEN b.v IS NOT NULL THEN 1 ELSE 0 END) AS m FROM "
               f"(SELECT DISTINCT {qa} AS v FROM {table_ref(A)} WHERE {qa} IS NOT NULL LIMIT {sample}) a "
               f"LEFT JOIN (SELECT DISTINCT {qb} AS v FROM {table_ref(B)}) b ON a.v = b.v")
        try:
            row = run_sql(sql).iloc[0]
            n, m = int(row["n"] or 0), int(row["m"] or 0)
        except Exception as e:  # noqa: BLE001
            notes.append(f"overlap check failed for {A.name}.{qa} → {B.name}: {str(e)[:100]}")
            continue
        if n:
            cand["coverage"] = round(m / n, 3)
            cand["evidence"].append(f"{cand['coverage']:.0%} of {n} distinct values found in "
                                    f"{B.name}.{cand['to_cols'][0]}")
    return notes


# --------------------------------------------------------------------------- #
# 5. Score + split into accepted relationships and suggestions
# --------------------------------------------------------------------------- #
def score(c: dict) -> float:
    s = c.get("name_score", 0.0)
    if c.get("query_count"):
        s = max(s, 0.45) + min(0.3, 0.1 * math.log2(1 + c["query_count"]))
    cov = c.get("coverage")
    if cov is not None:
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
    for i, c in enumerate(sorted(by_column.values(), key=lambda x: -x["confidence"])):
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
