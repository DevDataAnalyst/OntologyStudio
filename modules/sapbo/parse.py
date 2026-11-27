"""
Bundle → Semantic IR (SIR), WS2 steps 1–3: parse Universe metadata, parse report SQL, identify physical tables
and columns. No interpretation happens here (that is normalize.py); every value is EXTRACTED as captured.

SIR layout (JSON-able dict, schema in docs/sapbo/sir.schema.json):

    sir_version, source_system, universe{...}
    physical:  tables[], joins[], contexts[]
    semantic:  objects[], prompts[], hierarchies[], security[]
    reports:   [{..., data_providers[{..., statements[]}], variables[], blocks[]}]
    mapping:   {BO table: {uc_table, uc_columns, uc_primary_key}}
    findings:  []
"""
from __future__ import annotations

from . import bo_sql
from . import provenance as pv
from . import webi_formula as wf

SIR_VERSION = "0.1"
LOGICAL = {"Numeric": "Decimal", "String": "String", "Date": "Date", "DateTime": "DateTime", "Boolean": "Boolean"}


def _finding(kind: str, severity: str, message: str, refs=None, klass: str = "Requires human validation",
             **extra) -> dict:
    return {"kind": kind, "severity": severity, "message": message, "subject_refs": list(refs or []),
            "class": klass, **extra}


def to_sir(b: dict) -> dict:
    u = b["universe"]
    dialect = (u.get("connection") or {}).get("dialect", "ansi")
    df, bl = u.get("dataFoundation", {}), u.get("businessLayer", {})
    findings: list[dict] = []

    mapping = {}
    for row in (b.get("mapping") or {}).get("tables", []):
        mapping[row["bo_table"].upper()] = {"uc_table": row.get("uc_table"), "uc_columns": row.get("uc_columns") or {},
                                            "uc_primary_key": row.get("uc_primary_key") or []}

    # ---------- physical
    tables = []
    for t in df.get("tables", []):
        name = t["name"].upper()
        rec = {"sir_id": f"tbl:{name}", "bo_id": t.get("id"), "name": name, "kind": t.get("type", "table"),
               "alias_of": (t.get("aliasOf") or "").upper() or None, "sql": t.get("sql"),
               "columns": [{"name": c["name"].upper(), "source_type": c.get("dataType")} for c in t.get("columns", [])],
               "uc": mapping.get(name, {}).get("uc_table")}
        if rec["kind"] == "derived" and rec["sql"]:
            tree, err = bo_sql.parse(rec["sql"], dialect)
            rec["sql_parsed"] = err is None
            if err:
                findings.append(_finding("unparsed_sql", "error", f"Derived table {name} SQL doesn't parse: {err}",
                                         [rec["sir_id"]]))
            else:
                rec["depends_on"] = [x.name.upper() for x in tree.find_all(bo_sql.exp.Table)]
        tables.append(rec)

    joins = []
    for j in df.get("joins", []):
        pairs, extra, err = bo_sql.join_pairs(j["expression"], dialect)
        joins.append({"sir_id": f"join:{j['id']}", "bo_id": j["id"], "expression": j["expression"],
                      "pairs": [[list(a), list(c)] for a, c in pairs], "extra_predicates": extra,
                      "parse_error": err, "cardinality": pv.record(j.get("cardinality"), pv.EXTRACTED, "extractor",
                                                                   evidence=[f"bo:join/{j['id']}"]),
                      "outer": j.get("outer", False), "shortcut": j.get("shortcut", False),
                      "contexts": [c["name"] for c in df.get("contexts", []) if j["id"] in c.get("joins", [])]})
    contexts = [{"sir_id": f"ctx:{c['name']}", "bo_id": c.get("id"), "name": c["name"],
                 "description": c.get("description", ""), "joins": [f"join:{x}" for x in c.get("joins", [])]}
                for c in df.get("contexts", [])]

    # ---------- semantic
    objects = []
    for it in bl.get("items", []):
        folder = it.get("path", "").split("\\", 1)[-1] if "\\" in it.get("path", "") else it.get("path", "")
        o = {"sir_id": f"obj:{it['id']}", "bo_id": it["id"], "kind": it["type"].lower(), "name": it["name"],
             "folder": folder, "select": it.get("select"), "where": it.get("where"),
             "projection": it.get("projection"), "source_datatype": it.get("dataType"),
             "datatype": LOGICAL.get(it.get("dataType")), "format": (it.get("format") or {}).get("numberFormat"),
             "parent": f"obj:{it['parent']}" if it.get("parent") else None, "hidden": it.get("hidden", False),
             "state": it.get("state", "Active"), "lov": (it.get("lov") or {}).get("name"),
             "description": pv.record(it.get("description") or None,
                                      pv.EXTRACTED if it.get("description") else pv.NEEDS_HUMAN, "extractor",
                                      evidence=[f"bo:obj/{it['id']}"])}
        resolved, notes = bo_sql.resolve(it.get("select") or "", prompt_mode="param")
        o["select_resolved"], o["bo_functions"] = (resolved or None), notes
        if it.get("where"):
            w, wnotes = bo_sql.resolve(it["where"], prompt_mode="param")
            o["where_resolved"], o["bo_functions"] = w, notes + wnotes
        for sql_key in ("select_resolved", "where_resolved"):
            if o.get(sql_key):
                tree, err = bo_sql.parse(o[sql_key].replace(":", "__p_"), dialect)
                if err:
                    o.setdefault("parse_errors", []).append(err)
                    findings.append(_finding("unparsed_sql", "error",
                                             f"Object '{it['name']}' {sql_key.split('_')[0]} doesn't parse: {err}",
                                             [o["sir_id"]]))
                else:
                    o.setdefault("tables", [])
                    o["tables"] = list(dict.fromkeys(o["tables"] + bo_sql.tables(tree)))
                    o.setdefault("columns", [])
                    o["columns"] = list(dict.fromkeys(o["columns"] + [f"{t}.{c}" for t, c in bo_sql.columns(tree)]))
                    if sql_key == "select_resolved":
                        o["aggregations"] = bo_sql.aggregations(tree)
        objects.append(o)

    prompts = [{"sir_id": f"prm:{p['id']}", "bo_id": p["id"], "name": p["name"], "datatype": p.get("dataType"),
                "multi": p.get("multi"), "optional": p.get("optional"), "default": p.get("default"),
                "bound_object": f"obj:{p['boundObject']}" if p.get("boundObject") else None}
               for p in bl.get("parameters", [])]
    hierarchies = [{"sir_id": f"hier:{h['id']}", "bo_id": h["id"], "name": h["name"],
                    "levels": [f"obj:{x}" for x in h.get("levels", [])]} for h in bl.get("hierarchies", [])]
    security = [{"sir_id": f"sec:{s['id']}", "bo_id": s["id"], "kind": s.get("type"), "name": s.get("name"),
                 "target": s.get("table"), "expression": s.get("where"), "groups": len(s.get("groups", []))}
                for s in bl.get("securityProfiles", [])]

    # ---------- reports
    by_id = {o["bo_id"]: o for o in objects}
    reports = []
    for r in b.get("reports", []):
        dps = []
        for dp in r.get("dataProviders", []):
            qs = dp.get("querySpecification") or {}
            conds = ((qs.get("filters") or {}).get("conditions")) or []
            stmts = []
            for i, sql in enumerate((dp.get("queryPlan") or {}).get("statements", [])):
                tree, err = bo_sql.parse(sql, dialect)
                st = {"sir_id": f"sql:{r['id']}/{dp['id']}/{i}", "sql": sql, "parse_error": err}
                if tree is not None:
                    st["tables"] = [t.name.upper() for t in tree.find_all(bo_sql.exp.Table)]
                    st["aliases"] = {(t.alias or t.name).upper(): t.name.upper()
                                     for t in tree.find_all(bo_sql.exp.Table)}
                    st["columns"] = [f"{t}.{c}" for t, c in bo_sql.columns(tree)]
                else:
                    findings.append(_finding("unparsed_sql", "error",
                                             f"{r['id']}/{dp['id']} statement {i + 1} doesn't parse: {err}",
                                             [st["sir_id"]]))
                stmts.append(st)
            if not stmts:
                findings.append(_finding("missing_sql", "error", f"{r['id']}/{dp['id']}: no generated SQL captured.",
                                         [f"rpt:{r['id']}"]))
            dps.append({"sir_id": f"dp:{r['id']}/{dp['id']}", "id": dp["id"], "name": dp.get("name"),
                        "universe": dp.get("dataSourceId"), "objects": [f"obj:{x}" for x in qs.get("resultObjects", [])],
                        "conditions": [c for c in conds if "object" in c],
                        "predefined_filters": [f"obj:{c['predefinedFilter']}" for c in conds if "predefinedFilter" in c],
                        "prompts": [f"prm:{c['prompt']}" for c in conds if c.get("prompt")],
                        "prompt_values": dp.get("promptValues") or {}, "custom_sql": dp.get("customSql", False),
                        "statements": stmts})
            for oid in qs.get("resultObjects", []):
                if oid not in by_id:
                    findings.append(_finding("unknown_object", "error",
                                             f"{r['id']}/{dp['id']} uses object {oid}, which isn't in the Universe.",
                                             [f"dp:{r['id']}/{dp['id']}"]))
        variables = []
        for v in r.get("variables", []):
            variables.append({"sir_id": f"var:{r['id']}/{v['id']}", "bo_id": v["id"], "name": v["name"],
                              "qualification": v.get("qualification", "Measure"), "formula": v.get("formula", ""),
                              "classification": wf.classify(v.get("formula", ""), v.get("qualification", "Measure"))})
        blocks = [blk | {"report_tab": rep.get("name")} for rep in r.get("reports", []) for blk in rep.get("blocks", [])]
        reports.append({"sir_id": f"rpt:{r['id']}", "bo_id": r["id"], "cuid": r.get("cuid"), "name": r["name"],
                        "folder": r.get("folder"), "usage": r.get("usage", {}),
                        "constructs_intended": r.get("constructs_intended", []),
                        "data_providers": dps, "variables": variables, "blocks": blocks})

    return {
        "sir_version": SIR_VERSION,
        "source_system": (b.get("manifest") or {}).get("source_system", {"type": "sap_bo"}),
        "extraction_methods": (b.get("manifest") or {}).get("extraction_methods", {}),
        "universe": {"id": u.get("id"), "cuid": u.get("cuid"), "name": u.get("name"), "type": u.get("type"),
                     "description": u.get("description", ""), "dialect": dialect,
                     "qualifier": (u.get("connection") or {}).get("qualifier")},
        "physical": {"tables": tables, "joins": joins, "contexts": contexts},
        "semantic": {"objects": objects, "prompts": prompts, "hierarchies": hierarchies, "security": security},
        "reports": reports, "mapping": mapping, "findings": findings,
    }
