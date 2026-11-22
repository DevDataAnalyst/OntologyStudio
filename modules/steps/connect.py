"""Connect: the one place a model comes into the studio.

  🏛️ Unity Catalog tables  build a model from tables and views (default on Databricks)
  📂 Saved model           open a version from the Volume store (ONTOLOGY_STORE)
  📥 Import a file         Apache Ossie YAML / JSON, a studio model, a SQLite database or a .sql script,
                           or pasted YAML; Ossie files are validated here, and nowhere else
  ✏️ Start from scratch    a blank model in the step-by-step form editor
"""
from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from .. import lifecycle_ui as lc, ossie, ossie_form, ossie_ui, pipeline, store
from .common import (DEFAULT_CATALOG, DEFAULT_SCHEMA, MAX_TABLES, ON_DATABRICKS, advanced, cached, goto, ss,
                     uc_runner, user_token)

DIALECTS = ["auto", "sqlite", "mysql", "postgres", "tsql", "oracle", "snowflake",
            "databricks", "bigquery", "redshift", "duckdb", "teradata"]
UC, SAVED, IMPORT, BLANK = "🏛️ Unity Catalog tables", "📂 Saved model", "📥 Import a file", "✏️ Start from scratch"
DB_FILES = ("db", "sqlite", "sqlite3", "sql", "ddl", "txt")
MODEL_FILES = ("yaml", "yml", "json")


def render() -> None:
    st.title("Connect")
    if ss.get("onto") is not None:
        st.info(f"**{(ss.onto.get('ontology') or {}).get('name', 'A model')}** is open. Connecting something new "
                "replaces it in the studio (saved versions are kept). Continue with **Model**.")
    starts = ([UC] if ON_DATABRICKS else []) + ([SAVED] if store.enabled() else []) + [IMPORT, BLANK]
    start = st.radio("Start from", starts, horizontal=True, key="cn_start", label_visibility="collapsed")
    if start == UC:
        _unity_catalog()
    elif start == SAVED:
        _saved()
    elif start == IMPORT:
        _import()
    else:
        st.caption("Describe a model step by step in plain forms: datasets, fields, relationships and metrics. "
                   "Submit on the last step to open it in the studio.")
        ossie_form.editor_tab(None, _submitted, allow_upload=False)
    if not ON_DATABRICKS:
        st.caption("Unity Catalog tables are available when the app runs in Databricks Apps.")


def _submitted(doc: dict, name: str) -> None:
    pipeline.submit_form_model(doc, name)
    goto("model")


# --------------------------------------------------------------------------- #
# Options shared by the build paths
# --------------------------------------------------------------------------- #
def _options(req: pipeline.BuildRequest, uc: bool) -> pipeline.BuildRequest:
    with st.expander("Options: profiling, discovery, AI descriptions", expanded=False):
        c1, c2 = st.columns(2)
        req.profile = c1.checkbox("Profile data (row counts, samples, code values)", value=True)
        req.verify = c1.checkbox("Check joins on the data (value overlap)", value=True,
                                 help="Runs a sampled DISTINCT/LEFT JOIN check per candidate join.")
        req.use_history = c2.checkbox("Learn from query history (joins and common calculations)", value=uc,
                                      disabled=not uc, help="One read of system.query.history (needs SELECT; text "
                                                            "is redacted unless you are in databricks_pii_access).")
        req.use_lineage = c2.checkbox("Include lineage", value=uc, disabled=not uc,
                                      help="Reads system.access.table_lineage / column_lineage.")
        req.enrich = st.checkbox("Draft descriptions, synonyms, code meanings, rules and metrics with AI",
                                 value=False, disabled=not ON_DATABRICKS,
                                 help="Foundation Model endpoint. Drafts never overwrite what a person wrote.")
        req.docs = st.file_uploader("Business documents to ground the AI (optional)",
                                    type=["md", "txt", "csv", "json", "yaml", "yml"], accept_multiple_files=True,
                                    disabled=not req.enrich) or []
        if advanced():
            st.markdown("**Advanced**")
            a1, a2, a3 = st.columns(3)
            req.history_days = a1.slider("Query history window (days)", 7, 365, 90, disabled=not req.use_history)
            req.lineage_days = a2.slider("Lineage window (days)", 7, 365, 180, disabled=not req.use_lineage)
            req.threshold = a3.slider("Auto-accept joins with confidence ≥", 0.5, 0.99, 0.75, 0.01)
            req.enum_max = a1.slider("Max distinct values for a code list", 2, 50, 12)
            req.base_iri = a2.text_input("Base IRI", ss.get("opt_base_iri", req.base_iri))
            req.prefix = a3.text_input("Prefix", ss.get("opt_prefix", req.prefix))
            ss.opt_base_iri, ss.opt_prefix = req.base_iri, req.prefix
            req.naming = a1.radio("Naming", ["semantic", "raw"], horizontal=True)
            req.strip = a2.checkbox("Strip dim_/fact_ prefixes", value=True)
            req.collapse = a3.checkbox("Collapse bridge tables into many-to-many", value=True)
            req.include_views = a1.checkbox("Include views", value=True)
    return req


def _go_build(req: pipeline.BuildRequest) -> None:
    try:
        pipeline.build(req)
    except Exception as e:  # noqa: BLE001
        st.error(f"Build failed: {e}")
        return
    goto("model")
    st.rerun()


# --------------------------------------------------------------------------- #
# Unity Catalog
# --------------------------------------------------------------------------- #
def _unity_catalog() -> None:
    from .. import uc
    run = uc_runner()
    try:
        catalogs = cached("catalogs", lambda: uc.list_catalogs(run))
    except Exception as e:  # noqa: BLE001
        st.error(f"Cannot list catalogs: {e}")
        return
    c1, c2 = st.columns([1, 2])
    cat = c1.selectbox("Catalog", catalogs,
                       index=catalogs.index(DEFAULT_CATALOG) if DEFAULT_CATALOG in catalogs else 0)
    schemas = cached(f"schemas:{cat}", lambda: uc.list_schemas(run, cat)) if cat else []
    chosen = c2.multiselect("Schemas", schemas, default=[DEFAULT_SCHEMA] if DEFAULT_SCHEMA in schemas else [])
    options = []
    for s in chosen:
        df = cached(f"tables:{cat}.{s}", lambda s=s: uc.list_tables(run, cat, s))
        options += [f"{cat}.{s}.{r.table_name}" for r in df.itertuples(index=False)]
    ss.setdefault("uc_pick", [])
    ss.uc_pick = [x for x in ss.uc_pick if x in options] if options else ss.uc_pick
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Select all", disabled=not options, use_container_width=True):
        ss.uc_pick = options[:MAX_TABLES]
    if b2.button("Clear", disabled=not ss.uc_pick, use_container_width=True):
        ss.uc_pick = []
    picked = st.multiselect(f"Tables & views (max {MAX_TABLES})", options, key="uc_pick",
                            format_func=lambda x: ".".join(x.split(".")[1:]), max_selections=MAX_TABLES)
    st.caption(f"{len(picked)} selected · runs with your Unity Catalog permissions")
    req = _options(pipeline.BuildRequest(source="uc", fqns=picked, name=cat or "MyOntology"), uc=True)
    if st.button("Build the model", type="primary", disabled=not picked):
        _go_build(req)


# --------------------------------------------------------------------------- #
# Saved model (Volume store)
# --------------------------------------------------------------------------- #
def _saved() -> None:
    if not store.enabled():
        lc._store_missing()
        return
    token = user_token()
    models = cached("store_models", lambda: store.list_models(token))
    if not models:
        st.info(f"No models in `{store.root()}` yet. Build or import one, then save it in **Publish**.")
        return
    c1, c2 = st.columns(2)
    smodel = c1.selectbox("Model", models, key="sv_model")
    hist = cached(f"hist:{smodel}", lambda: store.history(smodel, token)) if smodel else []
    labels = {v.path: v.label() for v in hist}
    path = c2.selectbox("Version", list(labels), format_func=labels.get, key=f"sv_ver_{smodel}_{len(hist)}")
    force = st.checkbox("Open even if it has validation errors", value=False, key="sv_force")
    b1, b2 = st.columns([1, 4])
    if b2.button("Refresh list"):
        lc.forget_store_cache()
        st.rerun()
    if b1.button("Open", type="primary", disabled=not path, use_container_width=True):
        try:
            err = pipeline.open_text(store.load(path, token), path.rsplit("/", 1)[-1], path, force=force)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if err:
            st.error(err)
            if ss.get("ossie_report") is not None:
                ossie_ui.report_view(ss.ossie_report, key="cn_saved")
        else:
            goto("model")
            st.rerun()
    with st.expander("All saved versions"):
        rows = [v.as_row() | {"model": v.model} for v in store.all_versions(token)[:200]]
        if rows:
            st.dataframe(pd.DataFrame(rows)[["model", "version", "kind", "saved by", "note", "errors"]],
                         hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
# Import a file or pasted YAML
# --------------------------------------------------------------------------- #
def _import() -> None:
    st.caption("An **Apache Ossie** model (YAML or JSON, checked against the official specification of the "
               "version it declares), a studio model (`.ontology.yaml`), a **SQLite** database or a **.sql** "
               "script. Or paste YAML below.")
    f = st.file_uploader("File", type=list(MODEL_FILES + DB_FILES), key="cn_file", label_visibility="collapsed")
    if f is None:
        pasted = st.text_area("…or paste an Ossie model or studio model YAML", height=200, key="cn_paste",
                              placeholder="version: 0.3.0.dev0\nname: sales\ndatasets:\n  - name: orders\n"
                                          "    source: main.sales.orders")
        if pasted.strip():
            _model_text(pasted, "pasted.yaml")
        return
    ext = f.name.rsplit(".", 1)[-1].lower()
    raw = f.getvalue()
    if ext in MODEL_FILES:
        _model_text(raw.decode("utf-8-sig", errors="replace"), f.name)
        return
    dialect = st.selectbox("SQL dialect of the script", DIALECTS) if ext in ("sql", "ddl", "txt") else "auto"
    name = f.name.rsplit(".", 1)[0]
    req = _options(pipeline.BuildRequest(source="file", upload=f, dialect=dialect, name=name), uc=False)
    if st.button("Build the model", type="primary"):
        _go_build(req)


def _model_text(text: str, file_name: str) -> None:
    if store.kind_of_text(text) == store.ONTOLOGY:
        st.caption("A studio model file.")
        if st.button("Open", type="primary", key="cn_open_studio"):
            err = pipeline.open_text(text, file_name)
            if err:
                st.error(err)
            else:
                goto("model")
                st.rerun()
        return
    strict = st.checkbox(f"Require the current spec version ({ossie.CURRENT_VERSION})", value=False,
                         help="Off: a 0.2.0.dev0 or 0.1.1 file is checked against its own version and can be "
                              "upgraded. On: any other version is an error.")
    key = (file_name, hashlib.sha1(text.encode("utf-8")).hexdigest(), strict)
    if ss.get("cn_rep_key") != key:
        ss.cn_rep_key, ss.ossie_report = key, ossie.validate_text(text, file_name, strict=strict)
    rep = ss.ossie_report
    model_index = 0
    if len(rep.models) > 1:
        model_index = st.selectbox("Model to open", range(len(rep.models)), format_func=lambda i: rep.model_names()[i])
    anyway = False
    if rep.errors and rep.convertible:
        anyway = st.checkbox("Open it even though it has errors", value=False,
                             help="Draws what can be read; fix the errors before you publish it.")
    ready = rep.convertible and (not rep.errors or anyway)
    if st.button("Open", type="primary", disabled=not ready, key="cn_open_ossie"):
        pipeline.open_report(rep, model_index)
        goto("model")
        st.rerun()
    ossie_ui.report_view(rep, key="cn_import")
