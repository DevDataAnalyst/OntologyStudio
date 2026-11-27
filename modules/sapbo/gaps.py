"""
Coverage and gap reporting (WS1 T1.11–T1.13, WS2 T2.13).

* required_artifacts(sir)  – per report, the Universe and report artifacts needed to reproduce it (the
  denominator of the ≥95% extraction target, not total metadata volume)
* extraction_coverage(...) – what share of those was captured with the properties it needs, misses categorized
* provenance_summary(c)    – how every exported property was obtained (the five client classes)
* gap_report_md(...)       – the semantic gap report (markdown), nothing silently inferred
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import provenance as pv

NEEDS = {  # artifact type -> properties it must have to count as captured
    "object": ("select", "datatype"),
    "measure": ("select", "projection"),
    "filter": ("where",),
    "table": ("columns",),
    "join": ("expression", "cardinality"),
    "prompt": ("datatype",),
    "sql": ("sql",),
    "variable": ("formula",),
}


def required_artifacts(sir: dict) -> list[dict]:
    objects = {o["sir_id"]: o for o in sir["semantic"]["objects"]}
    tables = {t["name"]: t for t in sir["physical"]["tables"]}
    joins = sir["physical"]["joins"]
    prompts = {p["sir_id"]: p for p in sir["semantic"]["prompts"]}
    rows = []
    for r in sir["reports"]:
        need: dict[str, tuple[str, dict]] = {}
        for dp in r["data_providers"]:
            for oid in dp["objects"] + dp["predefined_filters"] + [f"obj:{c['object']}" for c in dp["conditions"]]:
                o = objects.get(oid)
                kind = "measure" if o and o["kind"] == "measure" else "filter" if o and o["kind"] == "filter" \
                    else "object"
                need[oid] = (kind, o or {})
                for t in (o or {}).get("tables", []):
                    need[f"tbl:{t}"] = ("table", tables.get(t, {}))
            for pid in dp["prompts"]:
                need[pid] = ("prompt", prompts.get(pid, {}))
            for st in dp["statements"]:
                need[st["sir_id"]] = ("sql", st)
                for t in st.get("tables", []):
                    need[f"tbl:{t}"] = ("table", tables.get(t, {}))
            if not dp["statements"]:
                need[f"sql:{r['bo_id']}/{dp['id']}"] = ("sql", {})
        used_tables = {k[4:] for k, (kind, _) in need.items() if kind == "table"}
        used_tables |= {a for k, (kind, st) in need.items() if kind == "sql" for a in (st.get("aliases") or {})}
        for j in joins:
            ts = {p[0][0] for p in j["pairs"]} | {p[1][0] for p in j["pairs"]}
            if ts and ts <= used_tables:
                need[j["sir_id"]] = ("join", j)
        for v in r["variables"]:
            need[v["sir_id"]] = ("variable", v)
        for sid, (kind, art) in need.items():
            missing = []
            for prop in NEEDS[kind]:
                val = art.get(prop)
                if isinstance(val, dict) and "state" in val:
                    val = val.get("value")
                if val in (None, "", []):
                    missing.append(prop)
            if not art:
                cat = "not in extract"
            elif not missing:
                cat = ""
            elif kind == "join" and missing == ["cardinality"]:
                cat = "missing in BO (not set in the Universe)"
            else:
                cat = "not exposed by extraction method"
            rows.append({"report": r["bo_id"], "artifact": sid, "type": kind,
                         "name": art.get("name") or art.get("expression") or sid,
                         "captured": not missing and bool(art), "missing": ", ".join(missing), "category": cat})
    return rows


def extraction_coverage(rows: list[dict]) -> dict:
    total = len(rows)
    captured = sum(r["captured"] for r in rows)
    by_type = defaultdict(lambda: [0, 0])
    by_report = defaultdict(lambda: [0, 0])
    for r in rows:
        by_type[r["type"]][1] += 1
        by_type[r["type"]][0] += r["captured"]
        by_report[r["report"]][1] += 1
        by_report[r["report"]][0] += r["captured"]
    misses = Counter(r["category"] for r in rows if not r["captured"])
    return {"required": total, "captured": captured, "pct": round(100 * captured / total, 1) if total else 0.0,
            "by_type": {k: {"captured": a, "required": b} for k, (a, b) in sorted(by_type.items())},
            "by_report": {k: {"captured": a, "required": b} for k, (a, b) in sorted(by_report.items())},
            "misses": dict(misses), "target_met": total > 0 and captured / total >= 0.95,
            "uncategorized_misses": sum(1 for r in rows if not r["captured"] and not r["category"])}


def provenance_rows(c: dict) -> list[dict]:
    """One row per exported property, with its provenance state and client class."""
    rows = []

    def add(element, name, prop, rec):
        rows.append({"element": element, "name": name, "property": prop, "state": (rec or {}).get("state"),
                     "class": pv.klass(rec), "rule": (rec or {}).get("rule_id") or ""})
    for d in c["datasets"].values():
        add("dataset", d["name"], "source", d["source"])
        add("dataset", d["name"], "role", d["role"])
        add("dataset", d["name"], "description", d["description"])
        for f in d["fields"].values():
            if f.get("role") in ("key", "measure_input"):
                continue
            add("field", f"{d['name']}.{f['name']}", "expression", f["expression"])
            add("field", f"{d['name']}.{f['name']}", "description", f["description"])
    for r in c["relationships"]:
        add("relationship", r["name"], "cardinality", r["cardinality"])
    for m in c["metrics"].values():
        add("metric", m["name"], "expression", m["expression"])
        add("metric", m["name"], "description", m["description"])
    for f in c["findings"]:
        if f["kind"] in ("theta_join", "security", "unmapped_object", "unmapped_table"):
            rows.append({"element": f["kind"], "name": ",".join(f["subject_refs"]), "property": "definition",
                         "state": pv.UNSUPPORTED if f["class"].startswith("Unsupported") else pv.NEEDS_HUMAN,
                         "class": f["class"], "rule": ""})
    for calc in c["report_calcs"]:
        if calc["status"] in ("Manual", "Consumer specific"):
            rows.append({"element": "report_calc", "name": f"{calc['report']}.{calc['label']}",
                         "property": "definition",
                         "state": pv.NEEDS_HUMAN if calc["status"] == "Manual" else pv.UNSUPPORTED,
                         "class": "Requires human validation" if calc["status"] == "Manual"
                         else "Unsupported/unresolved", "rule": calc["kind"]})
    return rows


def provenance_summary(c: dict) -> dict:
    rows = provenance_rows(c)
    counts = Counter(r["class"] for r in rows)
    return {k: counts.get(k, 0) for k in pv.CLASSES} | {"total": len(rows)}


def gap_report_md(sir: dict, c: dict, cov: dict) -> str:
    u = sir["universe"]
    s = provenance_summary(c)
    lines = [f"# Semantic gap report: {u['name']}", "",
             f"Source: SAP BusinessObjects {sir['source_system'].get('version', '')} · Universe `{u['name']}` "
             f"({u['type']}, {u['dialect']}) · {len(sir['reports'])} reports", "",
             "## Extraction coverage", "",
             f"**{cov['captured']} of {cov['required']} required artifacts captured ({cov['pct']}%)** · target ≥95% "
             f"{'met' if cov['target_met'] else 'NOT met'} · uncategorized misses: {cov['uncategorized_misses']}", "",
             "| Artifact type | Captured | Required |", "|---|---|---|"]
    lines += [f"| {k} | {v['captured']} | {v['required']} |" for k, v in cov["by_type"].items()]
    if cov["misses"]:
        lines += ["", "Misses by category: " + ", ".join(f"{k}: {v}" for k, v in cov["misses"].items())]
    lines += ["", "## How each property was obtained", "", "| Class | Properties |", "|---|---|"]
    lines += [f"| {k} | {s[k]} |" for k in pv.CLASSES] + [f"| **Total** | **{s['total']}** |"]
    by_kind = defaultdict(list)
    for f in c["findings"]:
        by_kind[f["kind"]].append(f)
    lines += ["", "## Findings", ""]
    titles = {"unmapped_table": "Tables with no Unity Catalog counterpart", "unmapped_object": "Objects left out",
              "theta_join": "Non-equi joins", "derived_cardinality": "Cardinality derived from Unity Catalog keys",
              "unknown_cardinality": "Unknown cardinality", "aggregate_awareness": "Aggregate awareness",
              "projection_conflict": "Projection vs SQL aggregation conflicts",
              "ambiguous_name": "Ambiguous names in reports",
              "same_name_different_calc": "Same name, different calculation",
              "same_calc_different_name": "Different names, same calculation",
              "sql_conflict": "Report SQL vs Universe conflicts", "security": "Security (inventory only)",
              "unparsed_sql": "SQL that doesn't parse", "unresolved_reference": "Unresolved report references",
              "unresolved_column": "Unresolved columns", "missing_sql": "Missing report SQL",
              "cross_table_dimension": "Dimensions over several tables"}
    for kind, fs in by_kind.items():
        lines += [f"### {titles.get(kind, kind)} ({len(fs)})", ""]
        lines += [f"- {f['message']}  _(→ {f['class']})_" for f in fs] + [""]
    lines += ["## Report calculations", "", "| Report | Variable | Kind | Status | Becomes |", "|---|---|---|---|---|"]
    for calc in c["report_calcs"]:
        lines.append(f"| {calc['report']} | {calc['label']} | {calc['kind']} | {calc['status']} | "
                     f"{calc.get('metric') or calc.get('field') or '—'} |")
    missing_desc = [f"{d['name']}.{f['name']}" for d in c["datasets"].values() for f in d["fields"].values()
                    if f.get("role") == "dimension" and not pv.approved_value(f["description"])]
    missing_desc += [m["name"] for m in c["metrics"].values() if not pv.approved_value(m["description"])]
    lines += ["", f"## Missing business descriptions ({len(missing_desc)})", "",
              ", ".join(f"`{x}`" for x in missing_desc) or "None.", "",
              "These are candidates for LLM-proposed descriptions (WS3); proposals need a human approval before "
              "they become definitions."]
    return "\n".join(lines) + "\n"
