"""
Power BI developer view of the ontology: everything needed to model a data object in Power BI.

Pure functions over the ontology dict (no Streamlit), so they are unit tested:

  table_card(onto, cid)        business name, grain, role, storage-mode advice, size, freshness, owner,
                               certification, lineage, data-quality and modeling notes
  column_rows(onto, cid)       per column: friendly name, SQL type -> Power BI type, format string,
                               summarize-by, hide, data category, null %, distinct, min/max, samples,
                               foreign key target, Q&A synonyms, notes
  relationships(onto)          Power BI relationships (*:1, single direction, active/inactive for
                               role-playing tables and dates), with notes for what Power BI can't do
  measures(onto)               metrics translated from SQL to DAX (SUM/SUMX/RELATED/DIVIDE/COUNTROWS...),
                               with format strings, display folders and optional time intelligence
  date_table_dax(onto)         a calendar table that covers the profiled date range
  power_query_m(onto, cid,..)  Power Query M for the Databricks connector: navigate, keep, rename, type
  integrity_sql(onto, rel)     SQL that counts fact rows with no match (they become (Blank) in visuals)
  dictionary_markdown(...)     the whole data dictionary as Markdown
  starter_kit(...)             a zip of the dictionary, CSVs, DAX and M scripts
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections import defaultdict

import sqlglot
from sqlglot import exp

# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
_ACRONYMS = {"id", "sk", "uom", "sku", "vat", "kpi", "url", "ytd", "qtd", "mtd", "po", "gl"}


def friendly(name: str) -> str:
    """CustomerID -> Customer ID, unit_price -> Unit Price, OrderDetail -> Order Detail."""
    s = re.sub(r"[_\-.]+", " ", str(name)).strip()
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    words = []
    for w in s.split():
        if w.lower() in _ACRONYMS:
            words.append(w.upper())
        elif w.islower():
            words.append(w.capitalize())
        else:
            words.append(w)
    return " ".join(words) or str(name)


def table_name(onto: dict, cid: str) -> str:
    """The Power BI table name we recommend for a class (also used by the DAX and M we generate)."""
    c = onto["classes"].get(cid) or {}
    return c.get("pbi_name") or friendly(cid)


def measure_name(name: str) -> str:
    n = str(name)
    for pre, rep in (("sum_", "Total "), ("avg_", "Average "), ("min_", "Min "), ("max_", "Max "),
                     ("distinct_", "Distinct "), ("count_", "Count of ")):
        if n.lower().startswith(pre):
            return rep + friendly(n[len(pre):])
    if n.lower().endswith("_count"):
        return friendly(n[:-6]) + " Count"
    return friendly(n)


def dax_table(t: str) -> str:
    return "'" + t.replace("'", "''") + "'"


def dax_col(t: str, col: str) -> str:
    return f"{dax_table(t)}[{col.replace(']', ']]')}]"


# --------------------------------------------------------------------------- #
# Column facts
# --------------------------------------------------------------------------- #
MONEY = re.compile(r"price|amount|amt|revenue|sales|cost|freight|profit|margin(?!_?pct)|tax|fee|charge|"
                   r"discount_?amt|balance|salary|pay|spend|budget|value", re.I)
PCT = re.compile(r"pct|percent|rate|ratio|share|discount$|^discount", re.I)
KEYISH = re.compile(r"(^|_|[a-z])(id|key|sk|code|no|num|nbr)$", re.I)
YEARISH = re.compile(r"(^|_)(year|yr|month|mon|day|week|qtr|quarter|fiscal_?year)$|^(year|month|day)", re.I)
DATEISH = re.compile(r"date|_dt$|_at$|time|timestamp|day$", re.I)
CATEGORY = [  # Power BI "Data category" (map, URL and barcode categories)
    (re.compile(r"^(ship_?|bill_?)?country(_?name)?$|countryregion|country_?code", re.I), "Country/Region"),
    (re.compile(r"^(ship_?|bill_?)?(state|province|region)(_?name)?$|stateprovince", re.I), "State or Province"),
    (re.compile(r"^(ship_?|bill_?)?city(_?name)?$", re.I), "City"),
    (re.compile(r"county", re.I), "County"),
    (re.compile(r"postal|zip|post_?code", re.I), "Postal Code"),
    (re.compile(r"^(ship_?|bill_?)?address(_?line)?\d*$", re.I), "Address"),
    (re.compile(r"continent", re.I), "Continent"),
    (re.compile(r"latitude|^lat$", re.I), "Latitude"),
    (re.compile(r"longitude|^lon$|^lng$", re.I), "Longitude"),
    (re.compile(r"image|photo|picture|logo|thumbnail", re.I), "Image URL"),
    (re.compile(r"url|website|homepage|link", re.I), "Web URL"),
    (re.compile(r"barcode|ean|upc", re.I), "Barcode"),
]


def pbi_type(sql_type: str | None, col: str = "") -> tuple[str, str | None]:
    """SQL type -> (Power BI data type, note). Money-like decimals become Fixed decimal number."""
    t = (sql_type or "").upper()
    if re.search(r"BOOL|^BIT", t):
        return "True/False", None
    if re.search(r"TIMESTAMP|DATETIME", t):
        return "Date/Time", None
    if re.search(r"^DATE", t):
        return "Date", None
    if re.search(r"^TIME", t):
        return "Time", None
    if re.search(r"INT|LONG|SHORT|BYTE", t):
        return "Whole number", None
    m = re.search(r"DEC(?:IMAL)?\s*\(\s*\d+\s*,\s*(\d+)\s*\)|NUMERIC\s*\(\s*\d+\s*,\s*(\d+)\s*\)", t)
    if m:
        scale = int(m.group(1) or m.group(2))
        if scale == 0:
            return "Whole number", None
        if scale <= 4:
            return "Fixed decimal number", None
        return "Decimal number", None
    if re.search(r"DEC|NUMERIC|MONEY", t):
        return "Fixed decimal number", None
    if re.search(r"DOUBLE|FLOAT|REAL", t):
        if MONEY.search(col):
            return "Fixed decimal number", "money stored as floating point: Fixed decimal number avoids rounding drift"
        return "Decimal number", None
    if re.search(r"BINARY|BLOB", t):
        return "Binary", "binary columns can't be used in visuals; don't load"
    if re.search(r"ARRAY|MAP|STRUCT|VARIANT", t):
        return "Text", "nested type: flatten it in a view or in Power Query before loading"
    if DATEISH.search(col) and not KEYISH.search(col):
        return "Date", "dates stored as text: change the type to Date in Power Query"
    return "Text", None


def _profile(c: dict) -> dict:
    """column_profile from a fresh build; older ontologies fall back to data properties."""
    prof = dict(c.get("column_profile") or {})
    if prof:
        return prof
    for pid, p in (c.get("data_properties") or {}).items():
        col = p.get("source_column", pid)
        prof[col] = {"sql_type": p.get("sql_type"), "nullable": not p.get("required"),
                     "primary_key": p.get("identifier") or None}
    for col in c.get("source_columns") or []:
        prof.setdefault(col, {})
    return prof


def _prop_for(c: dict, col: str) -> tuple[str | None, dict]:
    for pid, p in (c.get("data_properties") or {}).items():
        if str(p.get("source_column", pid)).lower() == col.lower():
            return pid, p
    return None, {}


def _class_by_table(onto: dict) -> dict:
    out = {}
    for cid, c in onto["classes"].items():
        for k in (cid, c.get("source_table"), c.get("source_fqn")):
            if k:
                out[str(k).lower()] = cid
    return out


def _split_ref(ref: str) -> tuple[str, str]:
    t, _, col = str(ref).rpartition(".")
    return t, col


def _fk_columns(onto: dict, cid: str) -> dict:
    """column (lower) -> (target class, target column) for joins that leave this class."""
    look, out = _class_by_table(onto), {}
    for p in onto.get("object_properties", {}).values():
        j = p.get("join") or {}
        if p.get("domain") != cid or not j.get("from") or j.get("via") or p.get("cardinality") == "one-to-many":
            continue
        for f, t in zip(j["from"], j["to"]):
            ft, fc = _split_ref(f)
            tt, tc = _split_ref(t)
            out[fc.lower()] = (look.get(tt.lower(), p.get("range")), tc)
    for sg in onto.get("suggested_relationships", []):  # proposed keys found in the data or query history
        if sg.get("domain") == cid and sg.get("status") != "rejected" and len(sg.get("from_cols") or []) == 1:
            out.setdefault(sg["from_cols"][0].lower(), (sg.get("range"), (sg.get("to_cols") or [""])[0]))
    c = onto["classes"][cid]
    for col, d in _profile(c).items():            # declared FKs to tables outside the selection
        if d.get("foreign_key") and col.lower() not in out:
            tt, tc = _split_ref(d["foreign_key"])
            out[col.lower()] = (look.get(tt.lower(), tt), tc)
    return out


def _pk(c: dict) -> list[str]:
    prof = _profile(c)
    cols = [col for col, d in prof.items() if d.get("primary_key")]
    if cols:
        return cols
    out = []
    for pid in c.get("identifier") or []:
        p = (c.get("data_properties") or {}).get(pid) or {}
        out.append(p.get("source_column", pid))
    return out


def _pct(x) -> float | None:
    return None if x is None else round(100.0 * x, 1)


def column_rows(onto: dict, cid: str) -> list[dict]:
    c = onto["classes"][cid]
    prof, fks, pk = _profile(c), _fk_columns(onto, cid), {k.lower() for k in _pk(c)}
    rows_total = c.get("instance_count")
    role = c.get("role", "entity")
    names_used = set()
    out = []
    for col, d in prof.items():
        pid, p = _prop_for(c, col)
        name = friendly(col)
        while name in names_used:
            name += " "
        names_used.add(name)
        dtype, type_note = pbi_type(d.get("sql_type") or p.get("sql_type"), col)
        is_pk, fk = col.lower() in pk, fks.get(col.lower())
        sem = p.get("semantic_role")
        notes = [type_note] if type_note else []
        non_null, distinct = d.get("non_null"), d.get("distinct")
        null_pct = None
        if rows_total and non_null is not None:
            null_pct = _pct(1 - non_null / rows_total) if rows_total else None
        numeric = dtype in ("Whole number", "Decimal number", "Fixed decimal number")
        flag = numeric and distinct is not None and distinct <= 2 and str(d.get("min")) in ("0", "0.0", "None") \
            and str(d.get("max")) in ("1", "1.0")
        if is_pk or fk:
            kind = "Key" if is_pk and not fk else ("Foreign key" if fk and not is_pk else "Key + foreign key")
        elif dtype in ("Date", "Date/Time"):
            kind = "Date"
        elif sem == "measure" and numeric and not flag:
            kind = "Measure source"
        else:
            kind = "Attribute"
        # Summarize by
        if kind == "Measure source":
            agg = (p.get("aggregation") or "SUM").upper()
            summarize = {"AVG": "Average", "COUNT_DISTINCT": "Count (Distinct)", "MIN": "Minimum",
                         "MAX": "Maximum", "COUNT": "Count"}.get(agg, "Sum")
            if PCT.search(col) or re.search(r"price|rate", col, re.I):
                summarize = "Average"
        else:
            summarize = "None" if numeric else ""
        # Hide from report view
        hide = bool(fk) or (is_pk and (role == "fact" or numeric)) or dtype == "Binary"
        if kind == "Measure source" and role == "fact":
            hide_reason = "use the explicit measures instead"
        else:
            hide_reason = None
        # format string
        fmt = None
        if dtype in ("Fixed decimal number", "Decimal number") and kind == "Measure source":
            fmt = "0.0%" if PCT.search(col) and d.get("max") is not None and _num(d.get("max"), 2) <= 1 else (
                "\\$#,0.00" if MONEY.search(col) else "#,0.00")
        elif dtype == "Whole number" and kind != "Key" and not fk and not YEARISH.search(col):
            fmt = "#,0"
        elif dtype == "Date":
            fmt = "yyyy-mm-dd"
        category = next((cat for rx, cat in CATEGORY if rx.search(col)), None) if dtype == "Text" or \
            re.search(r"lat|lon|lng", col, re.I) else None
        if category in ("Latitude", "Longitude") and not numeric:
            category = None
        # data quality
        if rows_total and non_null == 0:
            notes.append("always empty: don't load it")
        elif fk and null_pct:
            notes.append(f"{null_pct}% of rows have no {friendly(fk[0])}: they show as (Blank) in visuals; "
                         f"consider an 'Unknown' member")
        if rows_total and distinct == 1 and non_null:
            notes.append("only one value in the data: little analytical value")
        if flag:
            notes.append("0/1 flag: set Summarize by to None, or convert to True/False with a friendly label")
            summarize = "None"
        if dtype == "Text" and distinct and non_null and rows_total and rows_total >= 1000 \
                and distinct / non_null > 0.9 and not is_pk and not fk:
            notes.append("near-unique text: high cardinality makes the model bigger; load only if you need it")
        if dtype == "Date/Time" and role == "fact":
            notes.append("split into a Date column (for the Date table relationship) and a Time column "
                         "to cut cardinality")
        if is_pk and c.get("identifier_inferred"):
            notes.append("key inferred from the data, not declared: verify it is unique")
        if kind == "Measure source" and dtype == "Whole number" and distinct and rows_total and rows_total >= 100 \
                and distinct <= 12 and not MONEY.search(col) and not re.search(r"qty|quantity|units|count", col, re.I):
            notes.append(f"only {distinct} distinct values: probably a code or key, not an amount; "
                         f"set Summarize by to None")
            summarize, hide_reason = "None", None
        if hide_reason:
            notes.append(hide_reason)
        if YEARISH.search(col) and numeric:
            summarize = "None"
        syn = list(p.get("synonyms") or [])
        if p.get("label") and p["label"].lower() != name.lower():
            syn.insert(0, p["label"])
        out.append({
            "Column": name, "Source column": col, "SQL type": d.get("sql_type") or p.get("sql_type") or "",
            "Power BI type": dtype, "Kind": kind, "Summarize by": summarize, "Hide": hide,
            "Format": fmt or "", "Data category": category or "",
            "References": f"{table_name(onto, fk[0]) if fk[0] in onto['classes'] else fk[0]}[{friendly(fk[1])}]"
            if fk else "",
            "Nullable": d.get("nullable"), "Null %": null_pct, "Distinct": distinct,
            "Min": _short(d.get("min")), "Max": _short(d.get("max")),
            "Sample values": ", ".join(_short(v) for v in (p.get("samples") or p.get("allowed_values") or [])[:5]),
            "Description": d.get("comment") or p.get("description") or "",
            "Q&A synonyms": ", ".join(dict.fromkeys(s for s in syn if s)),
            "Notes": "; ".join(dict.fromkeys(notes)),
        })
    return out


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _short(v, n: int = 40) -> str:
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# Table card
# --------------------------------------------------------------------------- #
def grain(onto: dict, cid: str) -> str:
    c = onto["classes"][cid]
    pk, fks = _pk(c), _fk_columns(onto, cid)
    label = c.get("label", friendly(cid)).lower()
    if not pk:
        return (f"Grain not declared: no primary key. Check for duplicate rows before using {table_name(onto, cid)} "
                f"on the one side of a relationship.")
    if len(pk) == 1:
        return f"One row per {label} ({friendly(pk[0])})."
    parts = []
    for col in pk:
        tgt = fks.get(col.lower())
        parts.append((onto["classes"].get(tgt[0], {}).get("label") or friendly(tgt[0])).lower() if tgt else
                     friendly(col).lower())
    return f"One row per {' and '.join(dict.fromkeys(parts))} combination ({' + '.join(friendly(x) for x in pk)})."


def _date_columns(onto: dict, cid: str) -> list[str]:
    return [r["Source column"] for r in column_rows(onto, cid) if r["Power BI type"] in ("Date", "Date/Time")]


def _main_date(cols: list[str]) -> str | None:
    if not cols:
        return None
    for rx in (r"^order_?date$", r"order|invoice|transaction|sale|posting|booking|txn|created", r"date"):
        hit = next((c for c in cols if re.search(rx, c, re.I)), None)
        if hit:
            return hit
    return cols[0]


def storage_mode(onto: dict, cid: str) -> tuple[str, str]:
    c = onto["classes"][cid]
    rows = c.get("instance_count")
    size = (c.get("table_meta") or {}).get("size_bytes")
    if c.get("role") != "fact":
        return "Import", ("Dimension-like table: Import it. If a fact table is in DirectQuery, set this one to "
                          "Dual so slicers stay fast.")
    date = _main_date(_date_columns(onto, cid))
    if rows is None:
        return "Import", "Row count not profiled: Import is the default; profile the table to size it."
    if rows > 100_000_000 or (size or 0) > 5 * 1024 ** 3:
        return "DirectQuery or composite", (
            f"{rows:,} rows: keep the detail in DirectQuery on the SQL warehouse and add an Import aggregation "
            "table for the common visuals (rule of thumb above about 100M rows or 5 GB).")
    if rows > 5_000_000:
        return "Import + incremental refresh", (
            f"{rows:,} rows: Import with incremental refresh"
            + (f" partitioned on {friendly(date)} (RangeStart/RangeEnd filter in the Power Query script)." if date
               else "; it needs a date column to partition on."))
    return "Import", f"{rows:,} rows: small enough to Import and refresh in full."


def table_card(onto: dict, cid: str) -> dict:
    c = onto["classes"][cid]
    meta = c.get("table_meta") or {}
    cert = c.get("certification") or {}
    mode, why = storage_mode(onto, cid)
    rows = column_rows(onto, cid)
    notes = []
    pk = _pk(c)
    if len(pk) > 1 and any(r.get("range") == cid and (r.get("join") or {}).get("to") and
                           len(r["join"]["to"]) > 1 for r in onto.get("object_properties", {}).values()):
        notes.append("Other tables join on a multi-column key: Power BI relationships use one column, so add a "
                     "combined key column on both sides in Power Query.")
    if not pk and c.get("role") != "fact":
        notes.append("No key: it can't be the one side of a relationship until a unique column is chosen.")
    if c.get("identifier_inferred"):
        notes.append("Primary key inferred from the data, not declared in the source.")
    if c.get("source_kind") == "view" or "VIEW" in str(meta.get("table_type", "")).upper():
        notes.append("This is a view: Power BI runs its query at every refresh (or every visual in DirectQuery).")
    blank_cols = [r["Column"] for r in rows if "always empty" in r["Notes"]]
    if blank_cols:
        notes.append(f"Always-empty columns you can skip: {', '.join(blank_cols)}.")
    selfref = [p for p in onto.get("object_properties", {}).values()
               if p.get("domain") == cid and p.get("range") == cid and p.get("join")]
    if selfref:
        notes.append("Self-referencing hierarchy: Power BI can't relate a table to itself; flatten it with "
                     "PATH() and PATHITEM() calculated columns.")
    lin = onto.get("lineage", {}).get("tables", [])
    return {
        "class": cid, "table": table_name(onto, cid), "label": c.get("label", cid),
        "source": c.get("source_fqn") or c.get("source_table") or cid,
        "role": c.get("role", "entity"), "grain": grain(onto, cid), "description": c.get("description") or "",
        "rows": c.get("instance_count"), "columns": len(rows), "size_bytes": meta.get("size_bytes"),
        "owner": cert.get("owner") or meta.get("owner"), "created": meta.get("created"),
        "last_altered": meta.get("last_altered"), "format": meta.get("storage_format"),
        "table_type": meta.get("table_type") or ("VIEW" if c.get("source_kind") == "view" else None),
        "domains": c.get("domains") or [], "status": cert.get("status", "draft"),
        "certified_by": cert.get("by"), "storage_mode": mode, "storage_why": why,
        "main_date": _main_date(_date_columns(onto, cid)), "notes": notes,
        "upstream": sorted({e["source"] for e in lin if e.get("target") == cid}),
        "downstream": sorted({e["target"] for e in lin if e.get("source") == cid}),
    }


def human_size(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return "–"


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #
def relationships(onto: dict, include_dates: bool = True) -> list[dict]:
    look = _class_by_table(onto)
    rels, seen = [], set()
    for pid, p in onto.get("object_properties", {}).items():
        j = p.get("join") or {}
        card = p.get("cardinality")
        if not j.get("from") or card == "one-to-many":
            continue
        if card == "many-to-many":
            if pid in seen or p.get("inverse") in seen:
                continue
            seen.add(pid)
            rels.append({"From table": f"{j.get('via')} (bridge)", "From column": ", ".join(
                friendly(_split_ref(x)[1]) for x in j["from"] + j["to"]),
                "To table": f"{table_name(onto, p['domain'])} / {table_name(onto, p['range'])}", "To column": "",
                "Cardinality": "Many to one (*:1) ×2", "Cross filter": "Both on one side", "Active": True,
                "Property": pid, "Notes": f"Many-to-many through {j.get('via')}: load the bridge table, relate it "
                "*:1 to both tables and set the filter to Both on one of them (or use CROSSFILTER in measures)."})
            continue
        src, tgt = p.get("domain"), p.get("range")
        if src == tgt:
            continue                                          # self-reference: PATH(), see table notes
        fcols = [_split_ref(x)[1] for x in j["from"]]
        tcols = [_split_ref(x)[1] for x in j["to"]]
        tgt = look.get(_split_ref(j["to"][0])[0].lower(), tgt)
        rels.append({"From table": table_name(onto, src), "From column": " + ".join(friendly(x) for x in fcols),
                     "To table": table_name(onto, tgt), "To column": " + ".join(friendly(x) for x in tcols),
                     "Cardinality": "One to one (1:1)" if card == "one-to-one" else "Many to one (*:1)",
                     "Cross filter": "Single", "Active": True, "Property": pid,
                     "Notes": "multi-column join: build a combined key column on both sides" if len(fcols) > 1 else "",
                     "_src": src, "_tgt": tgt, "_fcol": fcols[0], "_tcol": tcols[0]})
    have = {(r.get("_src"), r.get("_fcol", "").lower()) for r in rels}
    for sg in onto.get("suggested_relationships", []):   # proposed, not yet accepted
        fc, tc = (sg.get("from_cols") or [""]), (sg.get("to_cols") or [""])
        if sg.get("status") == "rejected" or len(fc) != 1 or (sg.get("domain"), fc[0].lower()) in have \
                or sg.get("domain") not in onto["classes"] or sg.get("range") not in onto["classes"]:
            continue
        rels.append({"From table": table_name(onto, sg["domain"]), "From column": friendly(fc[0]),
                     "To table": table_name(onto, sg["range"]), "To column": friendly(tc[0]),
                     "Cardinality": "Many to one (*:1)", "Cross filter": "Single", "Active": True,
                     "Property": "(suggested)", "Notes": "suggested from the data, not declared: accept it in the "
                     "Network tab once confirmed", "_src": sg["domain"], "_tgt": sg["range"], "_fcol": fc[0],
                     "_tcol": tc[0]})
    # role-playing: several paths between the same two tables -> only one can be active
    by_pair = defaultdict(list)
    for r in rels:
        if r.get("_src"):
            by_pair[(r["_src"], r["_tgt"])].append(r)
    for (src, tgt), group in by_pair.items():
        if len(group) < 2:
            continue
        tk = {k.lower() for k in _pk(onto["classes"].get(tgt, {}))}
        group.sort(key=lambda r: (r["_fcol"].lower() not in tk and r["_fcol"].lower() != r["_tcol"].lower(),
                                  r["_fcol"]))
        for r in group[1:]:
            r["Active"] = False
            r["Notes"] = (f"role-playing {table_name(onto, tgt)}: inactive; use it in a measure with "
                          f"USERELATIONSHIP({dax_col(r['From table'], r['From column'])}, "
                          f"{dax_col(r['To table'], r['To column'])})")
    if include_dates:
        for cid, c in onto["classes"].items():
            if c.get("role") != "fact":
                continue
            cols = _date_columns(onto, cid)
            main = _main_date(cols)
            for col in cols:
                active = col == main
                rels.append({"From table": table_name(onto, cid), "From column": friendly(col),
                             "To table": "Date", "To column": "Date", "Cardinality": "Many to one (*:1)",
                             "Cross filter": "Single", "Active": active, "Property": "(date table)",
                             "Notes": "" if active else
                             f"role-playing date: inactive; CALCULATE([measure], USERELATIONSHIP("
                             f"{dax_col(table_name(onto, cid), friendly(col))}, 'Date'[Date]))"})
    return rels


def integrity_sql(onto: dict, op_id: str, dialect: str = "databricks") -> str | None:
    """Rows on the many side whose key has no match on the one side (they land in a (Blank) row)."""
    p = onto.get("object_properties", {}).get(op_id) or {}
    j = p.get("join") or {}
    if not j.get("from") or j.get("via") or p.get("cardinality") not in ("many-to-one", "one-to-one"):
        return None
    src, tgt = onto["classes"].get(p["domain"]) or {}, onto["classes"].get(p["range"]) or {}

    def tbl(c):
        q = "`" if dialect == "databricks" else '"'
        if c.get("source_fqn"):
            return ".".join(f"{q}{x}{q}" for x in c["source_fqn"].split("."))
        return f"{q}{c.get('source_table')}{q}"
    q = "`" if dialect == "databricks" else '"'
    fcols = [_split_ref(x)[1] for x in j["from"]]
    tcols = [_split_ref(x)[1] for x in j["to"]]
    on = " AND ".join(f"f.{q}{a}{q} = t.{q}{b}{q}" for a, b in zip(fcols, tcols))
    nn = " AND ".join(f"f.{q}{a}{q} IS NOT NULL" for a in fcols)
    return (f"SELECT /* ontology-studio */ COUNT(*) AS rows_total, "
            f"SUM(CASE WHEN {' OR '.join(f'f.{q}{a}{q} IS NULL' for a in fcols)} THEN 1 ELSE 0 END) AS rows_null_key, "
            f"SUM(CASE WHEN {nn} AND t.{q}{tcols[0]}{q} IS NULL THEN 1 ELSE 0 END) AS rows_orphan "
            f"FROM {tbl(src)} f LEFT JOIN {tbl(tgt)} t ON {on}")


# --------------------------------------------------------------------------- #
# SQL -> DAX
# --------------------------------------------------------------------------- #
class Untranslatable(Exception):
    pass


def _many_to_one_path(onto: dict, base: str) -> set[str]:
    """Classes reachable from base through *:1 joins: RELATED() works for these."""
    seen, frontier = {base}, [base]
    while frontier:
        nxt = []
        for p in onto.get("object_properties", {}).values():
            if p.get("domain") in frontier and p.get("cardinality") in ("many-to-one", "one-to-one") \
                    and (p.get("join") or {}).get("from") and p.get("range") not in seen:
                seen.add(p["range"])
                nxt.append(p["range"])
        frontier = nxt
    return seen


class _Dax:
    def __init__(self, onto: dict, base: str):
        self.onto, self.base = onto, base
        self.look = _class_by_table(onto)
        self.related = _many_to_one_path(onto, base)
        self.base_t = table_name(onto, base)

    def col(self, node: exp.Column, row: bool) -> str:
        qual = node.table
        cid = self.look.get(qual.lower()) if qual else self.base
        if qual and cid is None:
            raise Untranslatable(f"unknown table {qual}")
        ref = dax_col(table_name(self.onto, cid), friendly(node.name))
        if cid == self.base:
            if not row:
                raise Untranslatable("a bare column outside an aggregate")
            return ref
        if cid in self.related:
            if not row:
                raise Untranslatable("a related column outside an aggregate")
            return f"RELATED({ref})"
        raise Untranslatable(f"{cid} is not on the one side of {self.base}")

    def agg(self, fn: str, xfn: str, arg, row: bool) -> str:
        if row:
            raise Untranslatable("nested aggregate")
        if isinstance(arg, exp.Column) and self.look.get((arg.table or "").lower(), self.base) == self.base:
            return f"{fn}({self.col(arg, True)})"
        return f"{xfn}({dax_table(self.base_t)}, {self.tr(arg, True)})"

    def tr(self, n, row: bool = False) -> str:  # noqa: C901
        if isinstance(n, exp.Paren):
            return f"({self.tr(n.this, row)})"
        if isinstance(n, exp.Column):
            return self.col(n, row)
        if isinstance(n, exp.Literal):
            return n.this if not n.is_string else '"' + n.this.replace('"', '""') + '"'
        if isinstance(n, exp.Null):
            return "BLANK()"
        if isinstance(n, exp.Boolean):
            return "TRUE()" if n.this else "FALSE()"
        if isinstance(n, exp.Neg):
            return f"-{self.tr(n.this, row)}"
        if isinstance(n, exp.Cast):
            return self.tr(n.this, row)
        if isinstance(n, exp.Div):
            return f"DIVIDE({self.tr(n.this, row)}, {self.tr(n.expression, row)})"
        ops = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=",
               exp.LT: "<", exp.LTE: "<=", exp.And: "&&", exp.Or: "||", exp.DPipe: "&"}
        for k, o in ops.items():
            if type(n) is k:
                return f"{self.tr(n.this, row)} {o} {self.tr(n.expression, row)}"
        if isinstance(n, exp.Not):
            if isinstance(n.this, exp.Is):
                return f"NOT ISBLANK({self.tr(n.this.this, row)})"
            return f"NOT ({self.tr(n.this, row)})"
        if isinstance(n, exp.Is) and isinstance(n.expression, exp.Null):
            return f"ISBLANK({self.tr(n.this, row)})"
        if isinstance(n, exp.In) and n.expressions:
            return f"{self.tr(n.this, row)} IN {{{', '.join(self.tr(e, row) for e in n.expressions)}}}"
        if isinstance(n, exp.Between):
            x = self.tr(n.this, row)
            return f"({x} >= {self.tr(n.args['low'], row)} && {x} <= {self.tr(n.args['high'], row)})"
        if isinstance(n, exp.Case):
            parts = ["TRUE()"]
            for w in n.args.get("ifs") or []:
                parts += [self.tr(w.this, row), self.tr(w.args["true"], row)]
            if n.args.get("default") is not None:
                parts.append(self.tr(n.args["default"], row))
            return f"SWITCH({', '.join(parts)})"
        if isinstance(n, exp.If):
            f = n.args.get("false")
            return (f"IF({self.tr(n.this, row)}, {self.tr(n.args['true'], row)}"
                    + (f", {self.tr(f, row)}" if f is not None else "") + ")")
        if isinstance(n, exp.Coalesce):
            return f"COALESCE({', '.join(self.tr(e, row) for e in [n.this] + list(n.expressions))})"
        if isinstance(n, exp.Round):
            d = n.args.get("decimals")
            return f"ROUND({self.tr(n.this, row)}, {self.tr(d, row) if d is not None else 0})"
        if isinstance(n, exp.Abs):
            return f"ABS({self.tr(n.this, row)})"
        if isinstance(n, exp.Sum):
            return self.agg("SUM", "SUMX", n.this, row)
        if isinstance(n, exp.Avg):
            return self.agg("AVERAGE", "AVERAGEX", n.this, row)
        if isinstance(n, exp.Min):
            return self.agg("MIN", "MINX", n.this, row)
        if isinstance(n, exp.Max):
            return self.agg("MAX", "MAXX", n.this, row)
        if isinstance(n, exp.Count):
            if row:
                raise Untranslatable("nested aggregate")
            a = n.this
            if a is None or isinstance(a, exp.Star) or (isinstance(a, exp.Literal) and not a.is_string):
                return f"COUNTROWS({dax_table(self.base_t)})"
            if isinstance(a, exp.Distinct):
                e = a.expressions
                if len(e) == 1 and isinstance(e[0], exp.Column):
                    return f"DISTINCTCOUNTNOBLANK({self._dc(e[0])})"  # SQL ignores NULLs
                raise Untranslatable("COUNT(DISTINCT expression)")
            if isinstance(a, exp.Column) and self.look.get((a.table or "").lower(), self.base) == self.base:
                return f"COUNT({self.col(a, True)})"
            return f"COUNTX({dax_table(self.base_t)}, {self.tr(a, True)})"
        raise Untranslatable(f"{n.key.upper()} has no DAX translation here")

    def _dc(self, c: exp.Column) -> str:
        cid = self.look.get((c.table or "").lower(), self.base) if c.table else self.base
        if cid != self.base and cid not in self.related:
            raise Untranslatable(f"{cid} is not on the one side of {self.base}")
        return dax_col(table_name(self.onto, cid), friendly(c.name))


def sql_to_dax(onto: dict, base: str, sql: str, dialect: str = "databricks") -> tuple[str | None, str | None]:
    """(DAX, None) or (None, reason). Column names follow the friendly names used in the generated M."""
    if base not in onto.get("classes", {}):
        return None, f"base table {base} is not in the model"
    try:
        tree = sqlglot.parse_one(sql, read=dialect if dialect in ("databricks", "sqlite", "spark") else None)
    except Exception as e:  # noqa: BLE001
        return None, f"could not parse the SQL ({e})"
    try:
        return _Dax(onto, base).tr(tree), None
    except Untranslatable as e:
        return None, str(e)


def measure_format(name: str, sql: str, whole: bool = False) -> str:
    if re.search(r"COUNT(ROWS)?\s*\(", sql, re.I) or re.search(r"(^|_)(count|number|num)(_|$)", name, re.I):
        return "#,0"
    if re.search(r"(^|_)(pct|percent|ratio|share|rate)(_|$)", name, re.I):
        return "0.0%"
    if MONEY.search(f"{name} {sql}"):
        return "\\$#,0.00"
    return "#,0" if whole else "#,0.00"


def _metric_label(m: dict) -> str:
    """Heuristic metrics are named sum_unitsinstock; their synonym keeps the readable words."""
    n, syn = m["name"], m.get("synonyms") or []
    for pre in ("sum_", "avg_", "min_", "max_", "distinct_"):
        if n.lower().startswith(pre) and syn:
            return measure_name(pre + re.sub(r"\s+", "_", syn[0]))
    return measure_name(n)


def _whole_columns(onto: dict, base: str, sql: str) -> bool:
    try:
        cols = list(sqlglot.parse_one(sql).find_all(exp.Column))
    except Exception:  # noqa: BLE001
        return False
    if not cols or base not in onto.get("classes", {}) or re.search(r"[*/]|AVG", sql, re.I):
        return False
    types = {r["Source column"].lower(): r["Power BI type"] for r in column_rows(onto, base)}
    return all(types.get(c.name.lower()) == "Whole number" for c in cols)


def measures(onto: dict, dialect: str = "databricks", time_intelligence: bool = False) -> list[dict]:
    out, names = [], set()
    for m in onto.get("metrics", []):
        if m.get("status") == "deprecated":
            continue
        base = m.get("base_class")
        dax, why = sql_to_dax(onto, base, m.get("sql") or "", dialect)
        name = _metric_label(m)
        while name in names:
            name += " (2)"
        names.add(name)
        if dax and base in onto["classes"]:
            keys = {r["Source column"].lower() for r in column_rows(onto, base)
                    if r["Kind"] in ("Key", "Foreign key", "Key + foreign key") or "probably a code" in r["Notes"]}
            try:
                used = {c.name.lower() for c in sqlglot.parse_one(m.get("sql") or "1").find_all(exp.Column)} \
                    if re.search(r"SUM|AVG", m.get("sql") or "", re.I) else set()
            except Exception:  # noqa: BLE001
                used = set()
            if used and used <= keys:
                why = "adds up a key or code column: probably not a real measure; drop it from the ontology"
        tbl = table_name(onto, base) if base in onto.get("classes", {}) else str(base)
        doms = m.get("domains") or (onto["classes"].get(base, {}).get("domains") if base in onto["classes"] else [])
        out.append({"Measure": name, "Home table": tbl, "DAX": dax or "", "Format": measure_format(m["name"],
                    m.get("sql") or "", _whole_columns(onto, base, m.get("sql") or "")), "Display folder": (doms or [tbl])[0], "Description":
                    m.get("description") or "", "Status": m.get("status", "draft"), "SQL": m.get("sql") or "",
                    "Synonyms": ", ".join(m.get("synonyms") or []), "Translated": dax is not None,
                    "Notes": (why or "") if dax else f"translate by hand: {why}", "Metric": m["name"]})
    if time_intelligence:
        facts_with_dates = {table_name(onto, cid) for cid, c in onto["classes"].items()
                            if c.get("role") == "fact" and _date_columns(onto, cid)}
        extra = []
        for r in out:
            if not r["Translated"] or r["Home table"] not in facts_with_dates or r["Format"] == "0.0%" \
                    or re.match(r"(AVERAGE|MIN|MAX)", r["DAX"]):
                continue
            ref = f"[{r['Measure']}]"
            for suffix, dax, fmt in (
                    ("YTD", f"TOTALYTD({ref}, 'Date'[Date])", r["Format"]),
                    ("PY", f"CALCULATE({ref}, SAMEPERIODLASTYEAR('Date'[Date]))", r["Format"]),
                    ("YoY %", f"DIVIDE({ref} - [{r['Measure']} PY], [{r['Measure']} PY])", "0.0%")):
                extra.append(dict(r, Measure=f"{r['Measure']} {suffix}", DAX=dax, Format=fmt,
                                  **{"Display folder": f"{r['Display folder']}\\Time intelligence",
                                     "Description": f"{suffix} of {r['Measure']} (needs the Date table)",
                                     "Notes": "", "SQL": ""}))
        out += extra
    return out


def measures_dax_query(rows: list[dict]) -> str:
    """A DAX query view script: paste it into Power BI Desktop's DAX query view and use
    'Update model: Add new measure' on each DEFINE MEASURE line."""
    ok = [r for r in rows if r["DAX"]]
    lines = ["// Generated by Ontology Studio. Paste into Power BI Desktop > DAX query view,",
             "// then click 'Update model with changes' to add the measures.", "// Format strings and folders "
             "are listed in measures.csv.", "DEFINE"]
    for r in ok:
        if r.get("Description"):
            lines.append(f"    /// {r['Description']}")
        lines.append(f"    MEASURE {dax_table(r['Home table'])}[{r['Measure']}] = {r['DAX']}")
    first = ok[:6]
    lines.append("EVALUATE")
    lines.append("    ROW(" + ", ".join(f'"{r["Measure"]}", [{r["Measure"]}]' for r in first) + ")" if first
                 else "    ROW(\"none\", BLANK())")
    skipped = [r for r in rows if not r["DAX"]]
    if skipped:
        lines.append("")
        lines.append("// Not translated (write these by hand):")
        lines += [f"//   {r['Measure']}: {r['SQL']}  -- {r['Notes']}" for r in skipped]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Date table
# --------------------------------------------------------------------------- #
def date_range(onto: dict) -> tuple[int | None, int | None]:
    """Years covered by the fact tables' dates (birth and hire dates on dimensions don't count)."""
    years = []
    facts = [cid for cid, c in onto["classes"].items() if c.get("role") == "fact"] or list(onto["classes"])
    for cid in facts:
        c = onto["classes"][cid]
        prof = _profile(c)
        for col in _date_columns(onto, cid):
            for k in ("min", "max"):
                m = re.match(r"\s*(\d{4})-\d{2}", str((prof.get(col) or {}).get(k) or ""))
                if m:
                    years.append(int(m.group(1)))
    return (min(years), max(years)) if years else (None, None)


def date_table_dax(onto: dict) -> str:
    lo, hi = date_range(onto)
    base = (f"CALENDAR(DATE({lo}, 1, 1), DATE({hi}, 12, 31))" if lo else "CALENDARAUTO()")
    return f"""// Calculated table. Then: Table tools > Mark as date table (column Date),
// sort Month by Month Number, and relate each fact date column to 'Date'[Date].
Date =
ADDCOLUMNS(
    {base},
    "Year", YEAR([Date]),
    "Quarter", "Q" & QUARTER([Date]),
    "Year Quarter", YEAR([Date]) & " Q" & QUARTER([Date]),
    "Month Number", MONTH([Date]),
    "Month", FORMAT([Date], "MMM"),
    "Year Month", FORMAT([Date], "YYYY-MM"),
    "Week of Year", WEEKNUM([Date], 2),
    "Weekday Number", WEEKDAY([Date], 2),
    "Weekday", FORMAT([Date], "ddd"),
    "Is Weekend", WEEKDAY([Date], 2) > 5
)
"""


# --------------------------------------------------------------------------- #
# Power Query M
# --------------------------------------------------------------------------- #
_M_TYPE = {"Whole number": "Int64.Type", "Decimal number": "type number", "Fixed decimal number": "Currency.Type",
           "Date": "type date", "Date/Time": "type datetime", "Time": "type time", "True/False": "type logical",
           "Text": "type text"}


def _m_str(s: str) -> str:
    return '"' + str(s).replace('"', '""') + '"'


def power_query_m(onto: dict, cid: str, host: str = "<workspace-host>", http_path: str = "/sql/1.0/warehouses/<id>",
                  incremental: bool | None = None) -> str:
    c = onto["classes"][cid]
    card = table_card(onto, cid)
    rows = [r for r in column_rows(onto, cid) if "always empty" not in r["Notes"] and r["Power BI type"] != "Binary"]
    fqn = c.get("source_fqn")
    parts = fqn.split(".") if fqn and fqn.count(".") == 2 else ["<catalog>", "<schema>", c.get("source_table", cid)]
    kind = "View" if card["table_type"] and "VIEW" in str(card["table_type"]).upper() else "Table"
    keep = ", ".join(_m_str(r["Source column"]) for r in rows)
    ren = ", ".join(f"{{{_m_str(r['Source column'])}, {_m_str(r['Column'].strip())}}}" for r in rows
                    if r["Column"].strip() != r["Source column"])
    typ = ", ".join(f"{{{_m_str(r['Column'].strip())}, {_M_TYPE.get(r['Power BI type'], 'type text')}}}" for r in rows)
    if incremental is None:
        incremental = card["storage_mode"].startswith("Import + incremental")
    steps = [
        f"    Source = Databricks.Catalogs({_m_str(host)}, {_m_str(http_path)}, [Catalog = null, Database = null, "
        f"EnableAutomaticProxyDiscovery = null]),",
        f"    Catalog = Source{{[Name = {_m_str(parts[0])}, Kind = \"Database\"]}}[Data],",
        f"    Schema = Catalog{{[Name = {_m_str(parts[1])}, Kind = \"Schema\"]}}[Data],",
        f"    Data = Schema{{[Name = {_m_str(parts[2])}, Kind = \"{kind}\"]}}[Data],",
        f"    Kept = Table.SelectColumns(Data, {{{keep}}}),",
    ]
    last = "Kept"
    if ren:
        steps.append(f"    Renamed = Table.RenameColumns({last}, {{{ren}}}),")
        last = "Renamed"
    steps.append(f"    Typed = Table.TransformColumnTypes({last}, {{{typ}}})")
    last = "Typed"
    if incremental and card["main_date"]:            # filter after typing: text dates can't compare to DateTime
        d = next((r["Column"].strip() for r in rows if r["Source column"] == card["main_date"]), card["main_date"])
        dtype = next((r["Power BI type"] for r in rows if r["Source column"] == card["main_date"]), "Date")
        lo, hi = ("Date.From(RangeStart)", "Date.From(RangeEnd)") if dtype == "Date" else ("RangeStart", "RangeEnd")
        steps[-1] += ","
        steps.append("    // incremental refresh: create DateTime parameters RangeStart and RangeEnd first")
        steps.append(f"    Window = Table.SelectRows(Typed, each [{d}] >= {lo} and [{d}] < {hi})")
        last = "Window"
    head = (f"// {card['table']}: {card['grain']}\n// Source: {card['source']}  ·  storage: {card['storage_mode']}\n"
            f"// Query name in Power BI: {card['table']}\n")
    return head + "let\n" + "\n".join(steps) + f"\nin\n    {last}\n"


# --------------------------------------------------------------------------- #
# Search, dictionary export
# --------------------------------------------------------------------------- #
def search(onto: dict, text: str, limit: int = 60) -> list[dict]:
    q = (text or "").strip().lower()
    if not q:
        return []
    terms = q.split()
    hits = []

    def score(*fields):
        blob = " ".join(str(f or "") for f in fields).lower()
        if not all(t in blob for t in terms):
            return 0
        return 3 if any(q == str(f or "").lower() for f in fields) else (2 if q in blob else 1)

    for cid, c in onto["classes"].items():
        s = score(cid, c.get("label"), table_name(onto, cid), c.get("source_table"), c.get("description"))
        if s:
            hits.append({"Type": "Table", "Name": table_name(onto, cid), "Table": table_name(onto, cid),
                         "Detail": c.get("description") or c.get("source_fqn") or c.get("source_table"),
                         "_class": cid, "_s": s + 1})
        for r in column_rows(onto, cid):
            s = score(r["Column"], r["Source column"], r["Description"], r["Q&A synonyms"])
            if s:
                hits.append({"Type": "Column", "Name": r["Column"], "Table": table_name(onto, cid),
                             "Detail": f"{r['Power BI type']} · {r['Kind']}" + (f" · {r['Description']}"
                                                                               if r["Description"] else ""),
                             "_class": cid, "_s": s})
    for m in onto.get("metrics", []):
        if m.get("status") == "deprecated":
            continue
        s = score(m["name"], _metric_label(m), m.get("description"), " ".join(m.get("synonyms") or []))
        if s:
            hits.append({"Type": "Measure", "Name": _metric_label(m),
                         "Table": table_name(onto, m.get("base_class")) if m.get("base_class") in onto["classes"]
                         else m.get("base_class"), "Detail": m.get("description") or m.get("sql"),
                         "_class": m.get("base_class"), "_s": s})
    for p in onto.get("pages", []):
        s = score(p.get("title"), p.get("body"))
        if s:
            hits.append({"Type": "Page", "Name": p["title"], "Table": "", "Detail": (p.get("body") or "")[:120],
                         "_class": None, "_s": s})
    hits.sort(key=lambda h: (-h["_s"], h["Type"], h["Name"]))
    return hits[:limit]


def _md_table(rows: list[dict], cols: list[str]) -> str:
    def cell(v):
        if v is None or v == "":
            return ""
        if isinstance(v, bool):
            return "yes" if v else ""
        return str(v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(cell(r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(out)


DICT_COLS = ["Column", "Source column", "Power BI type", "Kind", "Summarize by", "Hide", "Format",
             "Data category", "References", "Null %", "Distinct", "Min", "Max", "Description", "Notes"]


def dictionary_markdown(onto: dict, classes: list[str] | None = None, dialect: str = "databricks") -> str:
    classes = classes or sorted(onto["classes"], key=lambda c: (onto["classes"][c].get("role") != "fact", c))
    name = (onto.get("ontology") or {}).get("name") or "Data model"
    lines = [f"# {name}: Power BI data dictionary", ""]
    lines += ["## Tables", "", _md_table([{**(tc := table_card(onto, c)), "rows": f"{tc['rows']:,}" if tc["rows"]
                                          is not None else ""} for c in classes],
                                         ["table", "role", "rows", "grain", "storage_mode", "status", "source"]), ""]
    for cid in classes:
        tc = table_card(onto, cid)
        lines += [f"## {tc['table']}", ""]
        if tc["description"]:
            lines += [tc["description"], ""]
        facts = [("Source", f"`{tc['source']}`"), ("Role", tc["role"]), ("Grain", tc["grain"]),
                 ("Rows", f"{tc['rows']:,}" if tc["rows"] is not None else None),
                 ("Size", human_size(tc["size_bytes"]) if tc["size_bytes"] else None),
                 ("Last changed", tc["last_altered"]), ("Owner", tc["owner"]),
                 ("Domains", ", ".join(tc["domains"]) or None), ("Certification", tc["status"]),
                 ("Storage mode", f"{tc['storage_mode']}: {tc['storage_why']}"),
                 ("Upstream", ", ".join(tc["upstream"]) or None), ("Downstream", ", ".join(tc["downstream"]) or None)]
        lines += [f"- **{k}:** {v}" for k, v in facts if v]
        lines += [f"- **Note:** {n}" for n in tc["notes"]]
        lines += ["", _md_table(column_rows(onto, cid), DICT_COLS), ""]
    rels = [r for r in relationships(onto) if r["From table"] in {table_name(onto, c) for c in classes}]
    lines += ["## Relationships", "", _md_table(rels, ["From table", "From column", "To table", "To column",
                                                       "Cardinality", "Cross filter", "Active", "Notes"]), ""]
    ms = [m for m in measures(onto, dialect) if m["Home table"] in {table_name(onto, c) for c in classes}]
    lines += ["## Measures", "", _md_table(ms, ["Measure", "Home table", "DAX", "Format", "Status", "Description",
                                                "Notes"]), ""]
    vm = [v for v in onto.get("value_meanings", []) if v.get("class") in classes and v.get("values")]
    if vm:
        lines += ["## Code values", ""]
        for v in vm:
            lines.append(f"- **{table_name(onto, v['class'])}[{friendly(_split_ref(v.get('column') or v.get('property', ''))[1])}]**: "
                         + ", ".join(f"`{k}` = {d}" for k, d in (v.get("values") or {}).items()))
        lines.append("")
    lines += ["## Date table", "", "```dax", date_table_dax(onto).strip(), "```", ""]
    return "\n".join(lines)


def _csv(rows: list[dict]) -> str:
    import csv
    buf = io.StringIO()
    if rows:
        cols = [k for k in rows[0] if not k.startswith("_")]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


def starter_kit(onto: dict, classes: list[str] | None = None, host: str = "<workspace-host>",
                http_path: str = "/sql/1.0/warehouses/<id>", dialect: str = "databricks") -> bytes:
    classes = classes or list(onto["classes"])
    names = {table_name(onto, c) for c in classes}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", README.format(n=len(classes)))
        z.writestr("data_dictionary.md", dictionary_markdown(onto, classes, dialect))
        z.writestr("tables.csv", _csv([{k: (", ".join(v) if isinstance(v, list) else v)
                                        for k, v in table_card(onto, c).items()} for c in classes]))
        z.writestr("columns.csv", _csv([dict({"Table": table_name(onto, c)}, **r) for c in classes
                                        for r in column_rows(onto, c)]))
        z.writestr("relationships.csv", _csv([r for r in relationships(onto) if r["From table"] in names]))
        ms = [m for m in measures(onto, dialect, time_intelligence=True) if m["Home table"] in names]
        z.writestr("measures.csv", _csv(ms))
        z.writestr("measures.dax", measures_dax_query(ms))
        z.writestr("date_table.dax", date_table_dax(onto))
        for c in classes:
            z.writestr(f"power_query/{table_name(onto, c)}.pq", power_query_m(onto, c, host, http_path))
        z.writestr("model.json", json.dumps({"tables": sorted(names)}, indent=2))
    return buf.getvalue()


README = """# Power BI starter kit ({n} tables)

1. **Power Query**: for each file in `power_query/`, Home > Get data > Blank query > Advanced editor, paste,
   and name the query as the file. Each one connects with the Azure Databricks / Databricks connector,
   keeps the useful columns, gives them business names and sets Power BI data types.
2. **Date table**: Modeling > New table, paste `date_table.dax`, then Mark as date table.
3. **Relationships**: create the ones in `relationships.csv` (Model view). Inactive ones are role-playing:
   use them in measures with USERELATIONSHIP.
4. **Measures**: open DAX query view, paste `measures.dax`, and click "Update model with changes".
   Format strings and display folders are in `measures.csv`.
5. **Column settings**: `columns.csv` lists, per column, Hide, Summarize by, Format, Data category and
   Q&A synonyms; `data_dictionary.md` is the human-readable version with grain and data-quality notes.
"""
