"""Describe: what only people know about the data, in one place.

  📖 Dictionary  search every table, column, metric and page; one table in business terms
  🧠 Meanings    term search, code values, glossary, business rules, synonyms
  📐 Metrics     the model's metrics and its metric views (generated per fact + the guided builder)
  🏠 Domains     domains and subdomains, assigning tables, certification and owners
  📊 Governance clarity  coverage of descriptions, owners, domains and review status across the model
  📚 Knowledge   governed pages, learned snippets from query history, experts
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import governance_ui as gui, governance_visualization, metric_views as mv, mv_builder_ui as mvb
from .. import workbench_ui as wb
from ..dictionary_ui import dictionary_tab
from .common import ON_DATABRICKS, commit, runner, ss, sub_view, uc_runner

VIEWS = ["📖 Dictionary", "🧠 Meanings", "📐 Metrics", "🏠 Domains", "📊 Governance clarity", "📚 Knowledge"]


def render(onto: dict) -> None:
    view = sub_view("Describe view", VIEWS, "ds_view")
    if view == VIEWS[0]:
        dictionary_tab(onto, runner(), uc_runner, ON_DATABRICKS)
    elif view == VIEWS[1]:
        _meanings(onto)
    elif view == VIEWS[2]:
        _metrics(onto)
    elif view == VIEWS[3]:
        gui.home_tab(onto, runner(), uc_runner, ON_DATABRICKS)
    elif view == VIEWS[4]:
        governance_visualization.render(onto)
    else:
        gui.knowledge_tab(onto, runner(), uc_runner, ON_DATABRICKS)


def _meanings(onto: dict) -> None:
    from .. import modeling_ui as mu
    mu.search_box(onto)                       # term cards: classes, columns, metrics, rules, pages, values
    st.markdown("**Code values**: what coded values stand for")
    vms = onto.get("value_meanings", [])
    if vms:
        rows = [{"class": v["class"], "property": v["property"], "code": code, "meaning": meaning,
                 "provenance": v.get("provenance")} for v in vms for code, meaning in v["values"].items()]
        edited = st.data_editor(pd.DataFrame(rows), hide_index=True, width="stretch",
                                disabled=["class", "property", "code", "provenance"], key="vm_editor")
        if st.button("Apply code values"):
            for r in edited.itertuples(index=False):
                for v in vms:
                    if v["class"] == r[0] and v["property"] == r[1] and r.meaning \
                            and v["values"].get(r.code) != r.meaning:
                        v["values"][r.code] = r.meaning
                        v["provenance"] = "manual"
            commit("Code values updated")
    else:
        st.caption("No coded columns found (build with data profiling on to find them).")
    st.divider()
    wb.glossary_box(onto, commit)
    st.divider()
    st.markdown("**Business rules**")
    if onto.get("rules"):
        st.dataframe(pd.DataFrame(onto["rules"]), hide_index=True, width="stretch")
    else:
        st.caption("None yet. Turn on AI descriptions in Connect and give it documents that describe them, or "
                   "promote a learned filter in Review.")
    syn = onto.get("synonyms_index", {})
    with st.expander(f"Synonyms index ({len(syn)} terms)"):
        st.dataframe(pd.DataFrame([{"term": k, "maps to": v if isinstance(v, str) else ", ".join(v)}
                                   for k, v in syn.items()]), hide_index=True, width="stretch", height=260)


def _metrics(onto: dict) -> None:
    classes = onto["classes"]
    mode = sub_view("Metrics view", ["📊 Metrics and metric views", "➕ New metric view"], "mv_mode")
    if mode.startswith("➕"):
        mvb.builder(onto, commit)
        return
    if onto.get("metrics"):
        st.dataframe(pd.DataFrame([{"metric": m["name"], "sql": m.get("sql"), "table": m.get("base_class"),
                                    "status": m.get("status", "draft"), "description": m.get("description") or "",
                                    "provenance": m.get("provenance")} for m in onto["metrics"]]),
                     hide_index=True, width="stretch")
        st.caption("Add or change a metric in the Model step's inspector (select its table); certify it in Domains "
                   "or Review.")
    else:
        st.caption("No metrics yet. Select a fact table in the Model step to add one.")
    facts = mv.fact_classes(onto)
    custom = onto.get("metric_views") or {}
    if not facts and not custom:
        st.info("No fact tables, so no metric views. Mark a table as **fact** in the Model step, or build one in "
                "**➕ New metric view**.")
        return
    views = {f: mv.build_metric_view(onto, f) for f in facts}
    labels = {f: f"{f} ({classes[f].get('source_table')})" for f in facts}
    for n, c in custom.items():
        views[f"★ {n}"], labels[f"★ {n}"] = c["view"], f"★ {n} (made in New metric view)"
    pick = st.selectbox("Metric view", list(views), format_func=labels.get, key="mv_view_pick")
    st.code(mv.to_yaml(views[pick]), language="yaml")
    st.caption("Metric views are created in Unity Catalog by **Publish → Deploy** (tick *metric views*), and "
               "downloaded in **Publish → Exports**.")
    if pick.startswith("★") and st.button("🗑️ Remove this view from the model", key=f"mvrm_{pick}"):
        onto["metric_views"].pop(pick[2:], None)
        ss.pop("mv_view_pick", None)
        commit(f"Metric view {pick[2:]} removed")
