"""
SAP BusinessObjects workspace (Streamlit): the POC flow from an extract bundle to metric views, Power BI
helpers and reconciliation. Rendered by app.py when the source is "SAP BusinessObjects".

Tabs follow the roadmap's workstreams: Extract · Normalize · Enrich · Ossie · Metric views · Power BI · Reconcile.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from modules.sapbo import bundle as bnd
from modules.sapbo import consume, enrich, pipeline, recon, to_mv
from modules.sapbo import provenance as pv

KEY = "sapbo"


def sidebar(on_databricks: bool) -> dict:
    """Sidebar inputs for the SAP BO source. Returns the selection; runs the pipeline on click."""
    ss = st.session_state
    mode = st.radio("Extract bundle", ["Synthetic CNR Finance sample", "Upload a bundle (.zip)", "BO server (REST)"],
                    key="sapbo_mode", help="A bundle is what the extractor captured from BusinessObjects: Universe, "
                                           "10 reports with their SQL, and the BO → Unity Catalog table mapping.")
    up = None
    if mode == "Upload a bundle (.zip)":
        up = st.file_uploader("Extract bundle", type=["zip"], key="sapbo_zip")
    elif mode == "BO server (REST)":
        st.info("Live extraction needs network access from the app to the BO server and a BO service account. "
                "Run an extractor where BO is reachable, package the bundle as described in "
                "docs/sapbo/extract-bundle.md and upload it here.")
    c1, c2 = st.columns(2)
    catalog = c1.text_input("Target catalog", value="cnfinance", key="sapbo_catalog")
    schema = c2.text_input("Target schema", value="semantic", key="sapbo_schema")
    report_metrics = st.checkbox("Include report-level calculations in the metric views", value=True,
                                 key="sapbo_rm", help="Off: only Universe measures become governed metrics; "
                                                      "report variables stay in the gap report.")
    can = mode == "Synthetic CNR Finance sample" or up is not None
    if st.button("Run SAP BO pipeline", type="primary", use_container_width=True, disabled=not can):
        try:
            b = bnd.sample() if mode.startswith("Synthetic") else bnd.load(up.getvalue())
            ss[KEY] = pipeline.run(b, catalog, schema, report_metrics)
            ss["sapbo_view"] = True
            ss.pop("sapbo_recon", None)
        except Exception as e:  # noqa: BLE001
            st.error(f"Pipeline failed: {e}")
    if ss.get(KEY) is not None:
        if ss.pop("sapbo_hide", False):
            ss["sapbo_view"] = False
        ss.setdefault("sapbo_view", True)
        st.toggle("Show the SAP BO workspace", key="sapbo_view")
    return {"mode": mode}


def _df(rows, cols=None):
    df = pd.DataFrame(rows)
    if cols and not df.empty:
        df = df[[c for c in cols if c in df.columns]]
    return df


def _state(rec):
    return (rec or {}).get("state", "")


def workspace(open_in_studio, run_sql=None, on_databricks: bool = False) -> None:
    ss = st.session_state
    res = ss.get(KEY)
    st.title("SAP BusinessObjects → Ossie → Metric views")
    if res is None:
        st.markdown(
            "Pick an **extract bundle** in the sidebar and **Run SAP BO pipeline**. The synthetic *CNR Finance* "
            "Universe (10 Web Intelligence reports over the `cnfinance` catalog) is built in, so every step works "
            "without a BusinessObjects server.\n\n"
            "The pipeline parses the Universe and the report SQL, normalizes them into datasets, relationships, "
            "fields and metrics, reports every gap instead of guessing, lets an LLM *propose* missing descriptions "
            "for human review, generates a canonical **Apache Ossie** model and converts it into **Databricks "
            "metric views**, with Power BI helpers and reconciliation queries.")
        st.stop()
    sir, c, cov, mvr = res["sir"], res["concepts"], res["coverage"], res["mv"]
    m = st.columns(6)
    m[0].metric("Extraction coverage", f"{cov['pct']}%", help="Required artifacts captured (target ≥95%)")
    m[1].metric("Datasets", len(c["datasets"]))
    m[2].metric("Metrics", len(c["metrics"]))
    m[3].metric("Findings", len(c["findings"]))
    m[4].metric("Ossie", "valid" if res["ossie_validation"]["ok"] else "errors")
    m[5].metric("Metric views", len(mvr["views"]))
    tabs = st.tabs(["1 · Extract", "2 · Normalize", "3 · Enrich", "4 · Ossie", "5 · Metric views", "6 · Power BI",
                    "7 · Reconcile"])
    with tabs[0]:
        _extract_tab(res)
    with tabs[1]:
        _normalize_tab(res)
    with tabs[2]:
        _enrich_tab(res, on_databricks)
    with tabs[3]:
        _ossie_tab(res, open_in_studio)
    with tabs[4]:
        _mv_tab(res)
    with tabs[5]:
        _pbi_tab(res)
    with tabs[6]:
        _recon_tab(res, run_sql)


def _extract_tab(res):
    sir, cov = res["sir"], res["coverage"]
    u = sir["universe"]
    st.subheader(f"Universe {u['name']} ({u['type']}, source dialect {u['dialect']})")
    st.caption(" · ".join(f"{k}: {v}" for k, v in (sir.get("extraction_methods") or {}).items()))
    st.markdown(f"**{cov['captured']} of {cov['required']}** artifacts the selected reports need were captured "
                f"(**{cov['pct']}%**, target ≥95% {'met ✅' if cov['target_met'] else 'not met ⚠️'}). "
                "Coverage counts only what the reports need, not total metadata volume.")
    c1, c2 = st.columns(2)
    c1.dataframe(_df([{"type": k, **v} for k, v in cov["by_type"].items()]), hide_index=True, use_container_width=True)
    c2.dataframe(_df([{"report": k, **v} for k, v in cov["by_report"].items()]), hide_index=True,
                 use_container_width=True)
    st.markdown("**Reports**")
    st.dataframe(_df([{"report": r["bo_id"], "name": r["name"], "refreshes (90d)": r["usage"].get("refreshes_90d"),
                       "data providers": len(r["data_providers"]),
                       "SQL statements": sum(len(d["statements"]) for d in r["data_providers"]),
                       "variables": len(r["variables"]), "constructs": ", ".join(r["constructs_intended"])}
                      for r in sir["reports"]]), hide_index=True, use_container_width=True)
    st.markdown("**Manual comparison checklist** · give it to a BO developer to tick against IDT / Webi")
    rows = res["required"]
    df = _df(rows).assign(matches_in_bo="")
    st.dataframe(df, hide_index=True, use_container_width=True, height=260)
    st.download_button("Download checklist (CSV)", df.to_csv(index=False), "extraction_validation.csv", "text/csv")
    st.markdown("**BO table → Unity Catalog mapping**")
    st.dataframe(_df([{"bo_table": k, "uc_table": v.get("uc_table") or "— not mapped —",
                       "primary key": ", ".join(v.get("uc_primary_key") or [])} for k, v in sir["mapping"].items()]),
                 hide_index=True, use_container_width=True)
    with st.expander("Report SQL as captured"):
        rid = st.selectbox("Report", [r["bo_id"] for r in sir["reports"]], key="sapbo_sql_r")
        r = next(x for x in sir["reports"] if x["bo_id"] == rid)
        for dp in r["data_providers"]:
            for st_ in dp["statements"]:
                st.caption(f"{dp['id']} · {dp['name']}" + (" · custom SQL" if dp["custom_sql"] else ""))
                st.code(st_["sql"], language="sql")


def _normalize_tab(res):
    c = res["concepts"]
    st.markdown("Every property is tagged with how it was obtained: **Extracted directly**, **Derived "
                "deterministically** (a named rule), **LLM-enriched** (proposal + human decision), **Requires human "
                "validation** or **Unsupported/unresolved**.")
    st.dataframe(_df([{"class": k, "properties": v} for k, v in res["provenance"].items() if k != "total"]),
                 hide_index=True)
    sub = st.radio("Show", ["Findings", "Metrics", "Datasets & fields", "Relationships", "Report calculations",
                            "Variants"], horizontal=True, key="sapbo_norm_view", label_visibility="collapsed")
    if sub == "Findings":
        st.dataframe(_df(c["findings"], ["kind", "severity", "class", "message"]), hide_index=True,
                     use_container_width=True, height=420)
    elif sub == "Metrics":
        st.dataframe(_df([{"metric": m["name"], "label": m["label"], "scope": m["scope"], "kind": m["kind"],
                           "expression": m["expression"]["value"], "provenance": _state(m["expression"]),
                           "rule": m["expression"].get("rule_id") or "", "BO": m["expression"].get("original"),
                           "description": pv.approved_value(m["description"]) or ""}
                          for m in c["metrics"].values()]), hide_index=True, use_container_width=True, height=420)
    elif sub == "Datasets & fields":
        st.dataframe(_df([{"dataset": d["name"], "role": d["role"]["value"], "kind": d["kind"],
                           "source": d["source"]["value"][:90], "fields": len(d["fields"])}
                          for d in c["datasets"].values()]), hide_index=True, use_container_width=True)
        ds = st.selectbox("Dataset", list(c["datasets"]), key="sapbo_ds")
        st.dataframe(_df([{"field": f["name"], "label": f["label"], "role": f["role"], "time": f["is_time"],
                           "expression": f["expression"]["value"], "provenance": _state(f["expression"]),
                           "rule": f["expression"].get("rule_id") or "", "BO": f["expression"].get("original"),
                           "scope": f.get("scope")} for f in c["datasets"][ds]["fields"].values()]),
                     hide_index=True, use_container_width=True)
    elif sub == "Relationships":
        st.dataframe(_df([{"name": r["name"], "from": r["from"], "to": r["to"],
                           "columns": ", ".join(f"{a}={b}" for a, b in zip(r["from_columns"], r["to_columns"])),
                           "cardinality": r["cardinality"]["value"], "provenance": _state(r["cardinality"]),
                           "contexts": ", ".join(r["contexts"])} for r in c["relationships"]]),
                     hide_index=True, use_container_width=True)
    elif sub == "Report calculations":
        st.dataframe(_df(c["report_calcs"], ["report", "label", "formula", "kind", "status", "reason", "metric",
                                             "field"]), hide_index=True, use_container_width=True)
    else:
        for v in c["variants"]:
            st.markdown(f"**{v['label']}** · {v['reason']}")
            st.dataframe(_df([{"metric": k, "expression": e} for k, e in v["expressions"].items()]), hide_index=True,
                         use_container_width=True)
            if v.get("llm_explanation"):
                st.info(f"LLM (proposal, not approved): {v['llm_explanation']['text']}")
    st.download_button("Download gap report (Markdown)", res["gap_report"], "gap_report.md", "text/markdown")


def _enrich_tab(res, on_databricks):
    ss = st.session_state
    c = res["concepts"]
    tg = enrich.targets(c)
    st.markdown(f"**{len(tg)}** elements have no approved business description. The LLM *proposes* descriptions "
                "and synonyms; nothing is exported as a definition until a reviewer approves or edits it. "
                "Expressions, filters and joins are never touched.")
    c1, c2 = st.columns(2)
    if c1.button("Propose descriptions with the LLM", disabled=not on_databricks or not tg, use_container_width=True,
                 help=None if on_databricks else "Needs the app's Foundation Model endpoint (Databricks Apps)."):
        bar = st.progress(0.0)
        try:
            stats = enrich.propose(c, progress=lambda f, msg: bar.progress(min(f, 1.0), msg))
            pipeline.rebuild(res)
            st.success(f"{stats['proposed']} proposals ({stats['skipped']} skipped).")
        except Exception as e:  # noqa: BLE001
            st.error(f"LLM call failed: {e}")
    if c2.button("Explain same-name variants", disabled=not on_databricks or not c["variants"],
                 use_container_width=True):
        try:
            enrich.explain_variants(c)
        except Exception as e:  # noqa: BLE001
            st.error(f"LLM call failed: {e}")
    acc = enrich.acceptance(c)
    st.caption(f"Proposals: {acc['pending']} pending · {acc['approved']} approved · {acc['edited']} edited · "
               f"{acc['rejected']} rejected")
    props = enrich.proposals(c)
    if not props:
        st.info("No pending proposals.")
        return
    reviewer = st.text_input("Reviewer", value=ss.get("sapbo_reviewer", ""), key="sapbo_reviewer")
    for i, p in enumerate(props[:40]):
        with st.container(border=True):
            st.markdown(f"**{p['name']}** · `{p['id']}` · {p['property']} · confidence {p['confidence']}")
            val = p["proposal"] if isinstance(p["proposal"], str) else ", ".join(p["proposal"] or [])
            edited = st.text_area("Proposal", value=val, key=f"sapbo_prop_{i}_{p['id']}_{p['property']}",
                                  label_visibility="collapsed", height=68)
            b1, b2, b3 = st.columns(3)
            dec = None
            if b1.button("Approve", key=f"sapbo_ok_{i}", disabled=not reviewer):
                dec = "edit" if edited != val else "approve"
            if b2.button("Reject", key=f"sapbo_no_{i}", disabled=not reviewer):
                dec = "reject"
            st.caption(f"evidence: {p['evidence']}")
            if dec:
                new = edited if p["property"] == "description" else [s.strip() for s in edited.split(",") if s.strip()]
                enrich.review(c, p["id"], p["property"], dec, reviewer, new)
                pipeline.rebuild(res)
                st.rerun()


def _ossie_tab(res, open_in_studio):
    v = res["ossie_validation"]
    (st.success if v["ok"] else st.error)(v["headline"])
    for f in v["findings"][:20]:
        st.caption(f"{f['severity']} · {f['check']} · {f['message']}")
    c1, c2 = st.columns(2)
    c1.download_button("Download Ossie YAML", res["ossie_yaml"], "cnr_finance.ossie.yaml", "text/yaml",
                       use_container_width=True)
    if c2.button("Open in Ontology Studio (network, editors, Ask)", use_container_width=True, type="primary"):
        err = open_in_studio(res["ossie_yaml"], "cnr_finance.ossie.yaml")
        if err:
            st.error(err)
        else:
            st.session_state["sapbo_hide"] = True     # applied before the toggle is drawn
            st.rerun()
    st.code(res["ossie_yaml"][:60000], language="yaml")


def _mv_tab(res):
    mvr = res["mv"]
    counts = pd.DataFrame(to_mv.report_counts(mvr["report"])).T
    st.markdown("Conversion status per element (**Direct · Transformed · Partial · Unsupported · Manual**). "
                "Deployment stays manual in the POC: copy the script into a SQL editor.")
    st.dataframe(counts.fillna(0).astype(int), use_container_width=True)
    st.dataframe(_df(mvr["report"], ["element", "name", "scope", "kind", "view", "status", "reason"]),
                 hide_index=True, use_container_width=True, height=300)
    bad = {n: x for n, x in mvr["checks"].items() if x}
    if bad:
        st.warning("Checks: " + "; ".join(f"{n}: {', '.join(x)}" for n, x in bad.items()))
    c1, c2 = st.columns(2)
    c1.download_button("Download deploy.sql (schema + metric views + Pattern 2 views)", pipeline.deploy_sql(res),
                       "deploy.sql", "text/plain", use_container_width=True)
    c2.download_button("Download conversion report", res["conversion_report"], "conversion_report.md",
                       "text/markdown", use_container_width=True)
    view = st.selectbox("Metric view", list(mvr["views"]), key="sapbo_mv_pick")
    st.code(mvr["ddl"][view], language="sql")


def _pbi_tab(res):
    sub = st.radio("Pattern", ["1 · Replicated semantic model (DAX)", "2 · SQL views over metric views",
                               "3 · Extra DAX: extension or override?", "4 · Publish to Power BI (preview)"],
                   key="sapbo_pbi", horizontal=True, label_visibility="collapsed")
    if sub.startswith("1"):
        rows = res["dax"]
        manual = sum(1 for r in rows if not r.get("dax"))
        st.markdown(f"DAX for **{len(rows)}** metric view measures over a star model of the same tables "
                    f"({manual} need hand-written DAX). Every line here is modelling that Power BI duplicates; "
                    "compare with Pattern 4, where Databricks generates it.")
        st.dataframe(_df(rows, ["view", "measure", "label", "status", "dax", "format"]), hide_index=True,
                     use_container_width=True, height=360)
        st.download_button("Download measures (TMDL)", consume.tmdl_measures(rows), "measures.tmdl", "text/plain")
    elif sub.startswith("2"):
        st.markdown("One plain SQL view per report block: Power BI reads pre-aggregated rows (no MEASURE() needed). "
                    "Set non-additive columns to **Don't summarize**: summing a ratio across rows is wrong.")
        flags = consume.summarize_flags(res["concepts"])
        st.dataframe(_df([{"view": v["name"], "report": v["report"], "block": v["block"],
                           "not converted": ", ".join(v["not_converted"])} for v in res["p2_views"]]),
                     hide_index=True, use_container_width=True)
        pick = st.selectbox("View", [v["name"] for v in res["p2_views"]], key="sapbo_p2")
        st.code(next(v["ddl"] for v in res["p2_views"] if v["name"] == pick), language="sql")
        st.dataframe(_df([{"measure": k, "summarize by": v} for k, v in flags.items()]), hide_index=True)
    elif sub.startswith("3"):
        st.markdown("A **presentation extension** only combines governed measures (YoY %, rolling averages, "
                    "formatting). A **semantic override** re-aggregates raw columns, removes filters or reuses a "
                    "governed name with its own logic. **Review** = a new business definition that should probably "
                    "be governed.")
        st.dataframe(_df([{"measure": r["name"], "expected": r["expected"], "classified": r["classification"],
                           "why": "; ".join(r["reasons"]), "dax": r["dax"]} for r in res["pattern3"]]),
                     hide_index=True, use_container_width=True)
        name = st.text_input("Measure name", key="sapbo_dax_name", value="My KPI")
        dax = st.text_area("DAX", key="sapbo_dax_text", value="DIVIDE([freight_revenue], [carloads])")
        governed = {m["name"]: m["label"] for m in res["concepts"]["metrics"].values() if m["scope"] == "universe"}
        r = consume.classify_dax(name, dax, governed)
        st.info(f"**{r['classification']}**: " + "; ".join(r["reasons"]))
    else:
        st.markdown(
            "**Publish to Power BI** from Catalog Explorer is a Databricks **Private Preview** (Nov 2026): each "
            "metric view becomes one table in a Power BI semantic model, dimensions become columns, measures become "
            "DAX, descriptions and formats carry over, **no relationships** are created, DirectQuery is the "
            "default (Import is possible), and sync is one-way (republish or a Power BI task in a Lakeflow Job). "
            "It can't be driven from this app: it needs the preview enabled in the workspace and Power BI "
            "sign-in.\n\nBefore publishing, name every field and add measure inputs as fields. The test plan is in "
            "the roadmap §10.4.")
        st.dataframe(_df([{"metric view": t, "measures": len(res["mv"]["views"][n]["measures"]),
                           "fields": len(res["mv"]["views"][n]["fields"]),
                           "windows (check after publish)": sum(1 for x in res["mv"]["views"][n]["measures"]
                                                               if x.get("window"))}
                          for n, t in res["mv"]["targets"].items()]), hide_index=True, use_container_width=True)


def _recon_tab(res, run_sql):
    ss = st.session_state
    items = res["recon_plan"]
    st.markdown("For every report statement: **L1** = the BO SQL as captured, transpiled and re-pointed at Unity "
                "Catalog; **L3** = the same question asked of the metric view with MEASURE(). The comparison counts "
                "rows and cells that differ. In the synthetic track BO's own SQL is the baseline.")
    st.dataframe(_df(items, ["report", "data_provider", "measures", "view", "status", "reason"]), hide_index=True,
                 use_container_width=True)
    c1, c2 = st.columns(2)
    c2.download_button("Download recon.sql", "\n\n".join((i["compare_sql"] or f"-- {i['reason']}") + ";"
                                                        for i in items), "recon.sql", "text/plain",
                       use_container_width=True)
    if c1.button("Run reconciliation on the warehouse", disabled=run_sql is None, use_container_width=True,
                 help=None if run_sql else "Needs the app in Databricks Apps, with the metric views deployed."):
        with st.spinner("Running comparison queries…"):
            ss["sapbo_recon"] = recon.run(items, run_sql)
    rows = ss.get("sapbo_recon")
    if rows:
        st.json(recon.summary(rows))
        st.dataframe(_df(rows), hide_index=True, use_container_width=True)
    pick = st.selectbox("Comparison SQL", [f"{i['report']}/{i['data_provider']}" for i in items], key="sapbo_rc")
    it = next(i for i in items if f"{i['report']}/{i['data_provider']}" == pick)
    st.code(it["compare_sql"] or it["reason"], language="sql")
