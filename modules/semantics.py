"""
Classify the ontology for analytics:

  class role      : fact | dimension | bridge | entity
  property role   : key | time | measure | dimension
  aggregation     : SUM | AVG | MIN | MAX | COUNT_DISTINCT (measures only)

and create base metrics (row count + one per measure) for every fact.
Everything is tagged provenance "heuristic" and can be changed in the app.
"""
from __future__ import annotations

import re

AMOUNT = re.compile(r"amount|amt|revenue|sales|cost|price|qty|quantity|units|total|value|"
                    r"spend|profit|margin|tax|discount|freight|fee|balance|budget|salary|count|cnt|"
                    r"volume|weight|duration|hours|minutes|points|score", re.I)
AVERAGE = re.compile(r"price|rate|pct|percent|ratio|avg|average|score|rating|unit_|_per_|age$", re.I)
NOT_MEASURE = re.compile(r"(^|_)(id|key|sk|code|no|num|nbr|year|yr|month|mon|day|week|qtr|quarter|"
                         r"zip|postal|phone|flag|flg|ind|is|has|seq|line|version|ver|level|rank|order)($|_)",
                         re.I)
NUMERIC_RANGES = ("xsd:integer", "xsd:long", "xsd:decimal", "xsd:double")
TIME_RANGES = ("xsd:date", "xsd:dateTime", "xsd:time")


def _is_fact_name(table: str) -> bool:
    return bool(re.match(r"^(fact|f|fct)_", table, re.I) or re.search(r"_(fact|facts|fct)$", table, re.I))


def _is_dim_name(table: str) -> bool:
    return bool(re.match(r"^(dim|d|lkp|ref)_", table, re.I) or re.search(r"_(dim|lookup|lkp|master)$", table, re.I))


def classify(onto: dict, overwrite: bool = False, add_metrics: bool = True) -> dict:
    classes, ops = onto["classes"], onto["object_properties"]
    outgoing = {c: 0 for c in classes}
    incoming = {c: 0 for c in classes}
    for p in ops.values():
        if p.get("cardinality") in ("many-to-one", "one-to-one") and p.get("join"):
            outgoing[p["domain"]] = outgoing.get(p["domain"], 0) + 1
            incoming[p["range"]] = incoming.get(p["range"], 0) + 1
    fk_cols = {}
    for p in ops.values():
        for ref in (p.get("join") or {}).get("from", []):
            fk_cols.setdefault(p["domain"], set()).add(ref.rpartition(".")[2].lower())

    for cid, c in classes.items():
        table = c.get("source_table", cid)
        # ---- property roles ------------------------------------------------
        n_measures = 0
        for pid, p in c["data_properties"].items():
            if p.get("semantic_role") and not overwrite:
                n_measures += p["semantic_role"] == "measure"
                continue
            col = (p.get("source_column") or pid).lower()
            rng = p.get("range")
            if p.get("identifier") or col in fk_cols.get(cid, set()):
                role = "key"
            elif rng in TIME_RANGES or re.search(r"(^|_)(date|dt|ts|time|timestamp)$", col):
                role = "time"
            elif (rng in NUMERIC_RANGES and not NOT_MEASURE.search(col) and not p.get("allowed_values")
                  and (AMOUNT.search(col) or _is_fact_name(table) or outgoing.get(cid, 0) >= 2)):
                role = "measure"
            else:
                role = "dimension"
            p["semantic_role"] = role
            if role == "measure":
                n_measures += 1
                p.setdefault("aggregation", "AVG" if AVERAGE.search(col) else "SUM")
            else:
                p.pop("aggregation", None)

        # ---- class role ----------------------------------------------------
        if c.get("role") and not overwrite:
            continue
        if _is_fact_name(table) or (outgoing.get(cid, 0) >= 2 and n_measures >= 1) \
                or (outgoing.get(cid, 0) >= 1 and n_measures >= 1 and incoming.get(cid, 0) == 0):
            role = "fact"
        elif _is_dim_name(table) or incoming.get(cid, 0) >= 1:
            role = "dimension"
        else:
            role = "entity"
        c["role"] = role

    # bridge tables were collapsed into many-to-many relationships; nothing to classify
    if add_metrics:
        add_base_metrics(onto)
    return onto


def add_base_metrics(onto: dict) -> None:
    metrics = onto.setdefault("metrics", [])
    names = {m["name"] for m in metrics}
    for cid, c in onto["classes"].items():
        if c.get("role") != "fact":
            continue
        base = re.sub(r"\W+", "_", c.get("label", cid)).strip("_").lower()
        candidates = [{"name": f"{base}_count", "description": f"Number of {c.get('label', cid)} records",
                       "base_class": cid, "sql": "COUNT(1)", "provenance": "heuristic"}]
        for pid, p in c["data_properties"].items():
            if p.get("semantic_role") != "measure":
                continue
            agg = p.get("aggregation", "SUM")
            col = p.get("source_column", pid)
            expr = f"COUNT(DISTINCT {col})" if agg == "COUNT_DISTINCT" else f"{agg}({col})"
            candidates.append({"name": f"{agg.lower().replace('count_distinct', 'distinct')}_{col}".lower(),
                               "description": f"{agg.title().replace('_', ' ')} of {p.get('label', pid)}",
                               "base_class": cid, "sql": expr, "provenance": "heuristic",
                               "synonyms": [p.get("label", pid)]})
        for m in candidates:
            if m["name"] not in names:
                metrics.append(m)
                names.add(m["name"])
