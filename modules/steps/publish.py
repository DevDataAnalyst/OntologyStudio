"""Publish: keep a version, put the model where people use it, or take it elsewhere.

  💾 Save & versions  the one save (Apache Ossie YAML in ONTOLOGY_STORE), unsaved changes, history,
                      compare, open, restore
  🚀 Deploy           one plan for everything the app writes to Databricks: the YAML next to the tables,
                      comments, tags, metric views, keys and a Genie space
  ⬇️ Exports          Apache Ossie YAML / JSON, metric views, the Power BI starter kit, the data
                      dictionary, and (Advanced) the studio model YAML
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile

import pandas as pd
import streamlit as st

from .. import lifecycle_ui as lc, metric_views as mv, ossie, ossie_ui, pipeline, powerbi as pb
from ..ontology_builder import to_yaml
from .common import ON_DATABRICKS, advanced, goto, runner, sub_view, uc_runner, user_token

VIEWS = ["💾 Save & versions", "🚀 Deploy", "⬇️ Exports"]


def render(onto: dict | None) -> None:
    view = sub_view("Publish view", VIEWS if onto is not None else VIEWS[1:2], "pb_view")
    if view == VIEWS[1] or onto is None:
        lc.deploy_page(onto, user_token(), uc_runner if ON_DATABRICKS else None, ON_DATABRICKS)
    elif view == VIEWS[0]:
        lc.versions_tab(onto, user_token(), _open_version)
    else:
        exports(onto)


def _open_version(text: str, path: str) -> None:
    err = pipeline.open_text(text, path.rsplit("/", 1)[-1], path, force=True)
    if err:
        st.error(err)
    else:
        goto("model")


def _slug(onto: dict) -> str:
    return re.sub(r"\W+", "_", (onto.get("ontology") or {}).get("name") or "model").strip("_").lower() or "model"


def exports(onto: dict) -> None:
    name = _slug(onto)
    st.markdown("#### Apache Ossie")
    st.caption(f"The vendor-neutral exchange format for semantic models (spec {ossie.CURRENT_VERSION}).")
    o1, o2 = st.columns(2)
    keep_studio = o1.checkbox("Keep studio details as extensions", value=True,
                              help="Roles, aggregations, code values, rules, governance and provenance travel as "
                                   "ONTOLOGY_STUDIO extensions, so a round trip loses nothing. Other tools ignore "
                                   "them.")
    keep_layout = o2.checkbox("Include the diagram layout", value=False)
    if keep_studio and not keep_layout:
        text, doc, rep = lc.ossie_text(onto)                    # the cached copy every step shares
        notes = []
    else:
        doc, notes = ossie.from_ontology(onto, include_studio=keep_studio, include_layout=keep_layout)
        text = ossie.dump_yaml(doc)
        rep = ossie.validate_text(text, f"{name}.ossie.yaml")
    ossie_ui.headline(rep)
    for n in notes:
        st.caption("• " + n)
    d1, d2, _ = st.columns([1.2, 1.2, 2])
    d1.download_button("⬇️ Ossie YAML", text, file_name=f"{name}.ossie.yaml", mime="application/x-yaml",
                       type="primary", use_container_width=True)
    d2.download_button("⬇️ Ossie JSON", json.dumps(doc, indent=2, default=str), file_name=f"{name}.ossie.json",
                       mime="application/json", use_container_width=True)
    with st.expander("Preview"):
        st.code(text, language="yaml")
    if rep.findings:
        with st.expander(f"Export check details ({len(rep.findings)} finding(s))"):
            ossie_ui.report_view(rep, key="export", show_stats=False, upgrade=False)

    st.markdown("#### Databricks metric views")
    facts, custom = mv.fact_classes(onto), onto.get("metric_views") or {}
    if facts or custom:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in facts:
                c = onto["classes"][f]
                target = (".".join(c["source_fqn"].split(".")[:2]) + "." if c.get("source_fqn") else "") \
                    + mv.view_name(onto, f)
                y = mv.to_yaml(mv.build_metric_view(onto, f))
                z.writestr(f"{mv.view_name(onto, f)}.yaml", y)
                z.writestr(f"{mv.view_name(onto, f)}.sql", mv.ddl(y, target))
            for n, c in custom.items():
                y = mv.to_yaml(c["view"])
                z.writestr(f"{n}.yaml", y)
                z.writestr(f"{n}.sql", mv.ddl(y, c.get("target") or n))
        st.download_button(f"⬇️ {len(facts) + len(custom)} metric view(s): YAML + SQL (.zip)", buf.getvalue(),
                           file_name=f"{name}_metric_views.zip", mime="application/zip")
    else:
        st.caption("No fact tables or custom metric views yet.")

    st.markdown("#### Power BI and documentation")
    host = os.getenv("DATABRICKS_HOST", "").replace("https://", "").rstrip("/") or "<workspace-host>"
    wid = os.getenv("SQL_WAREHOUSE_ID", "")
    path = f"/sql/1.0/warehouses/{wid}" if wid and not wid.startswith("REPLACE") else "/sql/1.0/warehouses/<id>"
    classes = list(onto["classes"])
    d = runner().get("dialect") or "databricks"
    dialect = "databricks" if d == "auto" else d
    p1, p2, p3 = st.columns(3)
    p1.download_button("⬇️ Power BI starter kit (.zip)", pb.starter_kit(onto, classes, host, path, dialect),
                       file_name=f"{name}_powerbi_kit.zip", mime="application/zip", use_container_width=True,
                       help="Data dictionary, column / relationship / measure CSVs, measures.dax for DAX query view, "
                            "a Date table, one Power Query (M) script per table and storage-mode advice.")
    p2.download_button("⬇️ Data dictionary (.md)", pb.dictionary_markdown(onto, classes, dialect),
                       file_name=f"{name}_data_dictionary.md", mime="text/markdown", use_container_width=True)
    p3.download_button("⬇️ All columns (.csv)",
                       pd.DataFrame([dict({"Table": pb.table_name(onto, c)}, **r) for c in classes
                                     for r in pb.column_rows(onto, c)]).to_csv(index=False),
                       file_name=f"{name}_columns.csv", mime="text/csv", use_container_width=True)

    if advanced():
        st.markdown("#### Studio model (Advanced)")
        include_layout = st.checkbox("Include diagram layout", value=True, key="ex_layout")
        y = to_yaml(onto if include_layout else {k: v for k, v in onto.items() if k != "layout"})
        st.download_button("⬇️ Studio model YAML", y, file_name=f"{name}.ontology.yaml", mime="application/x-yaml",
                           help="The studio's own format (RDF/OWL flavoured). Saved versions use Apache Ossie.")
