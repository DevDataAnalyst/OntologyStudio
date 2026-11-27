"""
Normalized concepts → canonical Apache Ossie model (WS4), spec 0.3.0.dev0 (the version Ontology Studio's
validator targets).

Portable parts: datasets (UC source, primary key, fields with Databricks SQL), relationships (many → one,
field arrays), model-level metrics referencing dataset.field.

What Ossie can't say natively rides in `nonportable_extensions` with vendor_name SAP_BO:
  * per element: BO object id, folder, scope (universe / report:<id>), provenance of each property,
    the original BO expression, format, trace (bo → sir → ossie)
  * metrics: kind, components (metric-to-metric composition, since an Ossie metric can't reference another),
    the metric-view form of composed measures, windows (offset / cumulative / trailing), filter
  * model: hierarchies, prompts (parameters), predefined filters, report calculations that stay in BI,
    same-name variants, security inventory, findings summary

Only approved values (EXTRACTED / DERIVED / HUMAN_APPROVED / HUMAN_EDITED) become definitions. LLM proposals
go into the extension as `proposals`, never into description / synonyms.
"""
from __future__ import annotations

import yaml

from . import provenance as pv

VERSION = "0.3.0.dev0"
VENDOR = "SAP_BO"
MV_DIALECT = "DATABRICKS_METRIC_VIEW"


def _prov(rec: dict | None) -> dict:
    if not rec:
        return {}
    out = {"state": rec.get("state")}
    if rec.get("rule_id"):
        out["rule"] = rec["rule_id"]
    if rec.get("by") and rec["state"] in (pv.HUMAN_APPROVED, pv.HUMAN_EDITED, pv.HUMAN_REJECTED, pv.LLM_PROPOSED):
        out["by"] = rec["by"]
    return out


def _proposal(rec: dict | None):
    if rec and rec.get("state") == pv.LLM_PROPOSED:
        return {"value": rec.get("proposal"), "by": rec.get("by"), "confidence": rec.get("confidence"),
                "prompt_id": rec.get("prompt_id")}
    return None


def _ext(data: dict) -> list[dict]:
    return [{"vendor_name": VENDOR, "data": {k: v for k, v in data.items() if v not in (None, "", [], {})}}]


def _ai(desc_syn: dict | None) -> dict | None:
    syn = pv.approved_value(desc_syn) if desc_syn else None
    return {"synonyms": list(syn)[:10]} if syn else None


def build(c: dict, name: str | None = None) -> dict:
    u = c["universe"]
    datasets = []
    for d in c["datasets"].values():
        fields = []
        for f in d["fields"].values():
            fd = {"name": f["name"], "expression": {"sql": f["expression"]["value"]}}
            if f.get("label") and f.get("role") == "dimension":
                fd["label"] = f["label"]
            desc = pv.approved_value(f.get("description"))
            if desc:
                fd["description"] = desc
            if f.get("datatype"):
                fd["datatype"] = f["datatype"]
            if f.get("is_time"):
                fd["dimension"] = {"is_time": True}
            ai = _ai(f.get("synonyms"))
            if ai:
                fd["ai_context"] = ai
            props = {"expression": _prov(f["expression"]), "description": _prov(f.get("description"))}
            fd["nonportable_extensions"] = _ext({
                "role": f.get("role"), "bo_object": f.get("bo_object"), "folder": f.get("folder"),
                "scope": f.get("scope"), "hidden": f.get("hidden") or None, "format": f.get("format"),
                "original": f["expression"].get("original"), "provenance": props,
                "proposals": {k: p for k, p in (("description", _proposal(f.get("description"))),
                                                ("synonyms", _proposal(f.get("synonyms")))) if p},
                "trace": (f.get("trace") or {}) | {"ossie": f"{d['name']}.{f['name']}"} if f.get("trace") else None})
            fields.append(fd)
        ds = {"name": d["name"], "source": d["source"]["value"]}
        pk = [next((fn for fn, f in d["fields"].items() if f["expression"]["value"] == k), None)
              for k in d["primary_key"]]
        if pk and all(pk):
            ds["primary_key"] = pk
        desc = pv.approved_value(d.get("description"))
        if desc:
            ds["description"] = desc
        ds["fields"] = fields
        ds["nonportable_extensions"] = _ext({
            "role": d["role"]["value"] if d.get("role") else None, "bo_table": d["bo_table"], "kind": d["kind"],
            "alias_of": d.get("alias_of"),
            "provenance": {"source": _prov(d["source"]), "role": _prov(d.get("role")),
                           "description": _prov(d.get("description"))},
            "proposals": {"description": p} if (p := _proposal(d.get("description"))) else None,
            "trace": d["trace"] | {"ossie": d["name"]}})
        datasets.append(ds)

    rels = []
    for r in c["relationships"]:
        rels.append({"name": r["name"], "from": r["from"], "to": r["to"], "from_columns": r["from_columns"],
                     "to_columns": r["to_columns"],
                     "nonportable_extensions": _ext({"cardinality": r["cardinality"]["value"],
                                                     "contexts": r["contexts"],
                                                     "provenance": {"cardinality": _prov(r["cardinality"])},
                                                     "trace": r["trace"] | {"ossie": r["name"]}})})

    metrics = []
    for m in c["metrics"].values():
        expr = m["expression"]["value"]
        if m.get("over_window"):
            e = {"dialects": [{"dialect": MV_DIALECT, "expression": expr}]}
        else:
            e = {"sql": expr}
            if m.get("dialects"):
                e["dialects"] = [{"dialect": k, "expression": v} for k, v in m["dialects"].items()]
        md = {"name": m["name"], "expression": e}
        desc = pv.approved_value(m.get("description"))
        if desc:
            md["description"] = desc
        if m.get("datatype"):
            md["datatype"] = m["datatype"]
        ai = _ai(m.get("synonyms"))
        if ai:
            md["ai_context"] = ai
        md["nonportable_extensions"] = _ext({
            "label": m["label"], "scope": m["scope"], "kind": m["kind"], "components": m.get("components"),
            "mv_expression": m.get("mv_expression"), "window": m.get("window"), "filter": m.get("filter"),
            "datasets": m["datasets"], "format": m.get("format"), "projection": m.get("projection"),
            "reports": m.get("reports"), "bo_object": m.get("bo_object"), "folder": m.get("folder"),
            "original": m["expression"].get("original"),
            "provenance": {"expression": _prov(m["expression"]), "description": _prov(m.get("description"))},
            "proposals": {k: p for k, p in (("description", _proposal(m.get("description"))),
                                            ("synonyms", _proposal(m.get("synonyms")))) if p},
            "trace": (m.get("trace") or {}) | {"ossie": m["name"]}})
        metrics.append(md)

    sec = [f for f in c["findings"] if f["kind"] == "security"]
    doc = {
        "version": VERSION,
        "name": name or (u["name"] or "bo_model").lower().replace(" ", "_"),
        "description": (u.get("description") or "") + f" Generated from SAP BusinessObjects Universe "
                                                         f"'{u['name']}' by Ontology Studio.",
        "datasets": datasets, "relationships": rels, "metrics": metrics,
        "nonportable_extensions": _ext({
            "universe": {"id": u["id"], "cuid": u["cuid"], "name": u["name"], "type": u["type"],
                         "source_dialect": u["dialect"]},
            "hierarchies": c["hierarchies"], "parameters": c["parameters"],
            "filters": [{"name": f["name"], "label": f["label"], "expression": f["expression"]["value"],
                         "parameters": f["parameters"], "description": pv.approved_value(f["description"])}
                        for f in c["filters"].values()],
            "report_calculations": [x for x in c["report_calcs"] if not x.get("metric") and not x.get("field")],
            "variants": c["variants"],
            "security_inventory": [f["message"] for f in sec],
            "findings": {"total": len(c["findings"]),
                         "by_kind": {k: sum(1 for f in c["findings"] if f["kind"] == k)
                                     for k in dict.fromkeys(f["kind"] for f in c["findings"])}},
        }),
    }
    return doc


def to_yaml(doc: dict) -> str:
    head = ("# Apache Ossie model generated from an SAP BusinessObjects Universe by Ontology Studio.\n"
            "# BO-specific details (provenance, windows, composition, hierarchies, prompts) ride in SAP_BO "
            "nonportable_extensions.\n")
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


def validate(doc: dict):
    from modules.ossie import validate_text
    return validate_text(to_yaml(doc), "generated.ossie.yaml")
