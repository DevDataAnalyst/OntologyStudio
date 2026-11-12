"""
Ontology Studio — Databricks App (Streamlit)

1. Connect   : pick up to 100 tables/views anywhere in Unity Catalog (or upload a SQLite / .sql file,
               or an Apache Ossie semantic model, which is validated against the official spec)
2. Analyze   : declared keys + discovered joins (naming, value overlap, query history), lineage,
               fact/dimension and measure/dimension classification, base metrics
3. Edit      : interactive D3.js network diagram — drag, connect, repoint, delete, accept/reject
               suggestions; inspector for classes, columns, metrics and joins
4. Export    : ontology YAML, Apache Ossie YAML and Databricks metric view YAML (download or deploy
               to Unity Catalog)
"""
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile

import pandas as pd
import streamlit as st
import yaml

from modules import discovery, governance as gv, governance_ui as gui, ontology_edit as ed, ossie, ossie_ui
from modules import semantics, store
from modules import metric_views as mv
from modules.extractors import from_sql_script, from_sqlite_file, is_sqlite_bytes
from modules.graph_component import handle_event, network
from modules.ontology_builder import (BuildOptions, attach_lineage, attach_suggestions,
                                      build_ontology, refresh_index, to_yaml, validate)

ON_DATABRICKS = bool(os.getenv("DATABRICKS_APP_NAME") or os.getenv("DATABRICKS_HOST"))
DEFAULT_CATALOG = os.getenv("TARGET_CATALOG", "")
DEFAULT_SCHEMA = os.getenv("TARGET_SCHEMA", "")
MAX_TABLES = 100
DIALECTS = ["auto", "sqlite", "mysql", "postgres", "tsql", "oracle", "snowflake",
            "databricks", "bigquery", "redshift", "duckdb", "teradata"]
ROLES = ["fact", "dimension", "entity", "bridge"]
PROP_ROLES = ["dimension", "measure", "time", "key"]
AGGS = ["SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT"]
CARDS = ["many-to-one", "one-to-one", "many-to-many"]

st.set_page_config(page_title="Ontology Studio", page_icon="🕸️", layout="wide")
ss = st.session_state
ss.setdefault("onto", None)
ss.setdefault("selection", {"kind": None})
ss.setdefault("last_nonce", None)
ss.setdefault("graph_msg", None)
ss.setdefault("graph_key", 0)
ss.setdefault("uc_cache", {})


def is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001
        return False


def commit(msg: str | None = None, rerun: bool = True):
    """Persist a mutation of ss.onto and redraw."""
    refresh_index(ss.onto)
    if msg:
        ss.graph_msg = msg
    if rerun:
        st.rerun()


def uc_runner():
    from modules.db_connection import make_runner
    return make_runner()


def cached(key, fn):
    if key not in ss.uc_cache:
        ss.uc_cache[key] = fn()
    return ss.uc_cache[key]


# =========================================================================== #
# Sidebar
# =========================================================================== #
with st.sidebar:
    st.title("🕸️ Ontology Studio")
    st.header("1 · Connect")
    sources = (["Unity Catalog"] if ON_DATABRICKS else []) + ["Upload file", "Ossie YAML", "Saved version"]
    source = st.radio("Source", sources, horizontal=True, label_visibility="collapsed")

    selected_fqns, uploaded, dialect, version_path = [], None, "auto", None
    ossie_rep, ossie_ready, ossie_model = None, False, 0
    if source == "Unity Catalog":
        from modules import uc
        run = uc_runner()
        try:
            catalogs = cached("catalogs", lambda: uc.list_catalogs(run))
        except Exception as e:  # noqa: BLE001
            st.error(f"Cannot list catalogs: {e}")
            catalogs = []
        cat = st.selectbox("Catalog", catalogs,
                           index=catalogs.index(DEFAULT_CATALOG) if DEFAULT_CATALOG in catalogs else 0)
        schemas = cached(f"schemas:{cat}", lambda: uc.list_schemas(run, cat)) if cat else []
        default_s = [DEFAULT_SCHEMA] if DEFAULT_SCHEMA in schemas else []
        chosen = st.multiselect("Schemas", schemas, default=default_s)
        options = []
        for s in chosen:
            df = cached(f"tables:{cat}.{s}", lambda s=s: uc.list_tables(run, cat, s))
            options += [f"{cat}.{s}.{r.table_name}" for r in df.itertuples(index=False)]
        ss.setdefault("uc_pick", [])
        ss.uc_pick = [x for x in ss.uc_pick if x in options] if options else ss.uc_pick
        c1, c2 = st.columns(2)
        if c1.button("Select all", disabled=not options, use_container_width=True):
            ss.uc_pick = options[:MAX_TABLES]
        if c2.button("Clear", disabled=not ss.uc_pick, use_container_width=True):
            ss.uc_pick = []
        selected_fqns = st.multiselect(
            f"Tables & views (max {MAX_TABLES})", options, key="uc_pick",
            format_func=lambda x: ".".join(x.split(".")[1:]), max_selections=MAX_TABLES)
        st.caption(f"{len(selected_fqns)} selected · runs with your Unity Catalog permissions")
    elif source == "Upload file":
        uploaded = st.file_uploader("SQLite database or .sql script",
                                    type=["db", "sqlite", "sqlite3", "sql", "ddl", "txt"])
        dialect = st.selectbox("SQL dialect (scripts)", DIALECTS)
    elif source == "Ossie YAML":
        ossie_file = st.file_uploader(
            "Apache Ossie semantic model (.yaml, .yml or .json)", type=["yaml", "yml", "json"],
            help="One semantic model per file (version, name, datasets …). It is checked against the official "
                 "Apache Ossie JSON Schema for the version it declares, then with the semantic checks of the "
                 "reference validator (names, references, keys, metric scoping, SQL, dialect drift).")
        strict = st.checkbox(f"Require the current spec version ({ossie.CURRENT_VERSION})", value=False,
                             help="Off: a 0.2.0.dev0 or 0.1.1 file is checked against its own version and gets "
                                  "a warning plus an upgrade. On: any other version is an error.")
        if ossie_file is not None:
            raw = ossie_file.getvalue()
            okey = (ossie_file.name, hashlib.sha1(raw).hexdigest(), strict)
            if ss.get("ossie_key") != okey:
                ss.ossie_key = okey
                ss.ossie_report = ossie.validate_text(raw.decode("utf-8-sig", errors="replace"),
                                                      ossie_file.name, strict=strict)
            ossie_rep = ss.ossie_report
            ossie_ui.sidebar_badge(ossie_rep)
            if len(ossie_rep.models) > 1:
                ossie_model = st.selectbox("Model to load", range(len(ossie_rep.models)),
                                           format_func=lambda i: ossie_rep.model_names()[i])
            load_anyway = False
            if ossie_rep.errors and ossie_rep.convertible:
                load_anyway = st.checkbox("Load it even though it has errors", value=False,
                                          help="Draws what can be read; fix the errors before exchanging it.")
            ossie_ready = ossie_rep.convertible and (not ossie_rep.errors or load_anyway)
            st.caption("The full report is in the main panel." if ss.onto is None
                       else "The full report is in the **Apache Ossie** tab.")
        else:
            ss.pop("ossie_key", None)
            ss.pop("ossie_report", None)
    else:
        if store.enabled():
            token = None
            if ON_DATABRICKS:
                from modules.db_connection import get_user_token
                token = get_user_token()
            versions = cached("versions", lambda: store.list_versions(token))
            version_path = st.selectbox("Version", versions,
                                        format_func=lambda p: "/".join(p.split("/")[-2:]))
            if st.button("Refresh list"):
                ss.uc_cache.pop("versions", None)
                st.rerun()
        else:
            st.caption("Set ONTOLOGY_STORE in app.yaml to a /Volumes/... path to keep versions. "
                       "You can also paste a YAML in the Ontology YAML tab.")

    st.header("2 · Analyze")
    with st.expander("Discovery & lineage", expanded=False):
        profile = st.checkbox("Profile data (row counts, samples, enumerations)", value=True)
        verify = st.checkbox("Verify joins by value overlap", value=True,
                             help="Runs a sampled DISTINCT/LEFT JOIN check per candidate join.")
        use_history = st.checkbox("Mine joins from query history", value=ON_DATABRICKS,
                                  disabled=not ON_DATABRICKS,
                                  help="Reads system.query.history (needs SELECT; text is redacted "
                                       "unless you are in databricks_pii_access).")
        history_days = st.slider("Query history window (days)", 7, 365, 90, disabled=not use_history)
        use_lineage = st.checkbox("Include lineage", value=ON_DATABRICKS, disabled=not ON_DATABRICKS,
                                  help="Reads system.access.table_lineage / column_lineage.")
        lineage_days = st.slider("Lineage window (days)", 7, 365, 180, disabled=not use_lineage)
        threshold = st.slider("Auto-accept joins with confidence ≥", 0.5, 0.99, 0.75, 0.01,
                              help="Lower-confidence joins become suggestions in the diagram.")
        enum_max = st.slider("Max distinct values for an enumeration", 2, 50, 12)
    with st.expander("Ontology naming", expanded=False):
        onto_name = st.text_input("Ontology name", value="MyOntology")
        base_iri = st.text_input("Base IRI", value="http://example.org/ontology/")
        prefix = st.text_input("Prefix", value="ex")
        naming = st.radio("Naming", ["semantic", "raw"], horizontal=True)
        strip = st.checkbox("Strip dim_/fact_ prefixes from class names", value=True)
        collapse = st.checkbox("Collapse bridge tables into many-to-many", value=True)
        include_views = st.checkbox("Include views", value=True)

    st.header("3 · Semantic layer (optional)")
    enrich = st.checkbox("LLM: descriptions, synonyms, code meanings, rules, metrics",
                         value=False, disabled=not ON_DATABRICKS)
    docs_files = st.file_uploader("Business docs to ground it", type=["md", "txt", "csv", "json", "yaml", "yml"],
                                  accept_multiple_files=True, disabled=not enrich)

    can_go = bool(selected_fqns or uploaded or version_path or ossie_ready)
    go = st.button({"Saved version": "Load version", "Ossie YAML": "Load Ossie model"}.get(source, "Analyze & build"),
                   type="primary", use_container_width=True, disabled=not can_go)


# =========================================================================== #
# Build pipeline
# =========================================================================== #
def sqlite_runner(path):
    def run_sql(q):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return pd.read_sql_query(q, conn)
        finally:
            conn.close()
    return run_sql


status_area = st.empty()     # fixed slot so the page structure is identical on every run


def build():
    box = status_area.container()
    status = box.status("Building ontology…", expanded=True)
    bar = box.progress(0.0)
    lineage, joins, run_sql, table_ref, quote = None, None, None, None, None
    if source == "Unity Catalog":
        from modules import uc
        run = uc_runner()
        status.write(f"Reading {len(selected_fqns)} objects from Unity Catalog…")
        ds = uc.extract_objects(run, selected_fqns, profile=profile, enum_max=enum_max,
                                progress=lambda f, m: bar.progress(f * 0.6, m))
        if use_lineage:
            status.write("Reading lineage…")
            lineage = uc.read_lineage(run, selected_fqns, lineage_days)
            ds.warnings += lineage.pop("warnings", [])
        if use_history:
            status.write("Mining joins from query history…")
            qh = uc.read_query_joins(run, selected_fqns, history_days)
            joins = qh["joins"]
            ds.warnings += qh["warnings"]
            if qh["queries"]:
                status.write(f"Parsed {qh['queries']} queries, {len(joins)} distinct join pairs.")
        run_sql = run
        table_ref = lambda t: uc.bq(*t.full_name.split("."))  # noqa: E731
        quote = lambda c: f"`{c}`"  # noqa: E731
        ss.runner = {"kind": "uc", "dialect": "databricks"}
    else:
        data = uploaded.getvalue()
        name = re.sub(r"\.[^.]+$", "", uploaded.name)
        path = os.path.join(tempfile.gettempdir(), f"onto_{abs(hash(data))}.db")
        if is_sqlite_bytes(data):
            open(path, "wb").write(data)
            ds = from_sqlite_file(data, name, profile=profile, enum_max=enum_max)
            sqlite_ok = True
        else:
            text = data.decode("utf-8-sig", errors="replace")
            ds = from_sql_script(text, name, dialect=dialect, profile=profile, enum_max=enum_max)
            sqlite_ok = ds.dialect == "sqlite"
            if sqlite_ok:
                if os.path.exists(path):
                    os.remove(path)
                conn = sqlite3.connect(path)
                conn.executescript(text)
                conn.close()
        if sqlite_ok:
            run_sql = sqlite_runner(path)
            table_ref = lambda t: '"' + t.name.replace('"', '""') + '"'  # noqa: E731
            quote = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
            ss.runner = {"kind": "sqlite", "path": path, "dialect": "sqlite"}
        else:
            ss.runner = {"kind": None, "dialect": ds.dialect}
    bar.progress(0.65, "Discovering relationships…")
    status.write("Discovering relationships…")
    suggestions, notes = discovery.discover(ds, run_sql=run_sql, table_ref=table_ref, quote=quote,
                                            joins=joins, verify=verify and run_sql is not None,
                                            threshold=threshold)
    ds.warnings += [n for n in notes if "failed" in n]
    opts = BuildOptions(ontology_name=onto_name, base_iri=base_iri, prefix=prefix, naming=naming,
                        strip_prefixes=strip, collapse_junction_tables=collapse,
                        include_views=include_views, enum_detection=profile, enum_max=enum_max)
    onto = build_ontology(ds, opts)
    attach_suggestions(onto, suggestions)
    if lineage:
        attach_lineage(onto, lineage)
    semantics.classify(onto)
    onto.setdefault("ontology", {})["discovery_notes"] = notes or None
    gv.ensure(onto)
    if source == "Unity Catalog":               # domains / certification already tagged in UC
        status.write("Reading ontology tags…")
        n_tagged = gv.apply_uc_tags(onto, gv.read_uc_tags(run, selected_fqns))
        if n_tagged:
            status.write(f"{n_tagged} table(s) carry domain / certification tags.")
    if enrich:
        bar.progress(0.8, "LLM enrichment…")
        docs = {f.name: f.getvalue().decode("utf-8-sig", errors="replace") for f in docs_files or []}
        try:
            from modules.llm_enrich import enrich as llm_enrich
            onto = llm_enrich(onto, docs, progress=lambda f, m: bar.progress(0.8 + 0.2 * f, m))
        except Exception as e:  # noqa: BLE001
            onto.setdefault("warnings", []).append(f"LLM enrichment skipped: {e}")
    refresh_index(onto)
    status_area.empty()
    return onto


def load_ossie(rep: ossie.Report, model_index: int) -> dict:
    """Validated Ossie model -> ontology (0.1.1 files are unwrapped first)."""
    doc, notes = rep.document, []
    if rep.profile is not None and rep.profile.wrapped:
        docs, notes = ossie.upgrade(doc)
        doc = docs[min(model_index, len(docs) - 1)]
    new = ossie.to_ontology(doc, rep.file_name, base_iri=base_iri, prefix=prefix)
    if enrich:
        box = status_area.container()
        bar = box.progress(0.0, "LLM enrichment…")
        docs_text = {f.name: f.getvalue().decode("utf-8-sig", errors="replace") for f in docs_files or []}
        try:
            from modules.llm_enrich import enrich as llm_enrich
            new = llm_enrich(new, docs_text, progress=lambda f, m: bar.progress(f, m))
        except Exception as e:  # noqa: BLE001
            new.setdefault("warnings", []).append(f"LLM enrichment skipped: {e}")
        status_area.empty()
    three_part = all(str(c.get("source_fqn") or "").count(".") == 2 for c in new["classes"].values())
    ss.runner = {"kind": "uc" if ON_DATABRICKS and three_part else None, "dialect": "databricks"}
    ss.ossie_loaded = {"key": ss.get("ossie_key"), "file": rep.file_name, "notes": notes}
    return refresh_index(new)


if go:
    if source == "Saved version":
        token = None
        if ON_DATABRICKS:
            from modules.db_connection import get_user_token
            token = get_user_token()
        ss.onto = yaml.safe_load(store.load(version_path, token))
        ss.pop("ossie_loaded", None)
    elif source == "Ossie YAML":
        ss.onto = load_ossie(ossie_rep, ossie_model)
    else:
        ss.onto = build()
        ss.pop("ossie_loaded", None)
    ss.selection, ss.last_nonce, ss.graph_msg = {"kind": None}, None, None
    ss.graph_key += 1           # fresh diagram for a fresh ontology

onto = ss.onto
if onto is not None:
    gv.ensure(onto)
if onto is None:
    if source == "Ossie YAML" and ss.get("ossie_report") is not None:
        st.title("Apache Ossie validation")
        nxt = ("**Load Ossie model** in the sidebar to edit it as a network." if ss.ossie_report.ok
               else "fix what the report flags, then **Load Ossie model** to edit it as a network.")
        st.caption(f"File: **{ss.ossie_report.file_name}** · {nxt}")
        ossie_ui.report_view(ss.ossie_report, key="landing")
        st.stop()
    st.title("Ontology Studio")
    st.markdown(
        "Connect to Unity Catalog tables (or upload a SQLite / `.sql` file), then **Analyze & build**. "
        "You get a network of classes and relationships you can edit, plus exports as ontology "
        "YAML, Apache Ossie YAML and Databricks metric views.\n\n"
        "Have an **Apache Ossie** semantic model already? Choose **Ossie YAML** in the sidebar: the file is "
        f"checked against the official specification (current version `{ossie.CURRENT_VERSION}`; older "
        "`0.2.0.dev0` and `0.1.1` files are checked against their own version and can be upgraded) and can "
        "then be loaded into the same editor.")
    st.stop()

# =========================================================================== #
# Header metrics
# =========================================================================== #
classes, ops = onto["classes"], onto["object_properties"]
n_rel = sum(1 for p in ops.values() if p.get("join") and p["cardinality"] != "one-to-many") \
    - sum(1 for p in ops.values() if p["cardinality"] == "many-to-many") // 2
pending = [s for s in onto.get("suggested_relationships", []) if s.get("status") == "proposed"]
roles = pd.Series([c.get("role", "entity") for c in classes.values()]).value_counts().to_dict()
m = st.columns(7)
m[0].metric("Classes", len(classes))
m[1].metric("Facts / Dims", f"{roles.get('fact', 0)} / {roles.get('dimension', 0)}")
m[2].metric("Relationships", n_rel)
m[3].metric("Suggestions", len(pending))
m[4].metric("Lineage edges", len(onto.get("lineage", {}).get("tables", [])))
m[5].metric("Metrics", len(onto.get("metrics", [])))
m[6].metric("Domains", len(onto.get("domains", [])))
for w in onto.get("warnings", [])[:8]:
    st.warning(w)

(tab_net, tab_home, tab_yaml, tab_ossie, tab_mv, tab_sem, tab_know, tab_review, tab_tables, tab_ask,
 tab_mon) = st.tabs(
    ["🕸️ Network", "🏠 Domains", "Ontology YAML", "Apache Ossie", "Metric views", "Semantic layer",
     "📚 Knowledge", "✅ Review queue", "Tables", "Ask (Text-to-SQL)", "📈 Monitor"])

# =========================================================================== #
# Network + inspector
# =========================================================================== #
with tab_net:
    left, right = st.columns([2.6, 1.2], gap="medium")
    with left:
        evt = network(onto, height=660, dark=is_dark(), message=ss.graph_msg, selected=ss.selection,
                      key=f"graph_{ss.graph_key}")
        st.caption("Drag to arrange · scroll to zoom · double-click a class to focus · **＋ Connect**: drag from "
                   "the class holding the key to the class it points to · select a relationship to **Repoint** "
                   "or **Delete** · crow's feet mark the many side · dashed orange = suggestion (Accept/Reject) "
                   "· dotted green = lineage · hover for details.")
    if evt and evt.get("nonce") != ss.last_nonce:
        ss.last_nonce = evt["nonce"]
        try:
            msg, sel = handle_event(onto, evt)
        except Exception as e:  # noqa: BLE001
            msg, sel = f"Edit failed: {e}", None
        if sel is not None:
            ss.selection = sel
        if evt["type"] not in ("positions",):
            ss.graph_msg = msg
            st.rerun()

    with right:
        sel = ss.selection or {"kind": None}
        # ---------------- class inspector ----------------
        if sel.get("kind") == "node" and sel.get("id") in classes:
            cid = sel["id"]
            c = classes[cid]
            st.subheader(cid)
            st.caption(f'{c.get("source_fqn") or c.get("source_table", "")} · '
                       f'{c.get("instance_count", "?")} rows')
            with st.form(f"cls_{cid}"):
                new_name = st.text_input("Class name", cid)
                role = st.selectbox("Role", ROLES, index=ROLES.index(c.get("role", "entity"))
                                    if c.get("role", "entity") in ROLES else 2)
                desc = st.text_area("Description", c.get("description", "") or "", height=70)
                syn = st.text_input("Synonyms (comma separated)", ", ".join(c.get("synonyms", [])))
                doms = st.text_input("Domains (comma separated)", ", ".join(c.get("domains") or []))
                cert = c.get("certification") or {}
                g1, g2 = st.columns(2)
                cstatus = g1.selectbox("Status", gv.STATUSES, index=gv.STATUSES.index(cert.get("status", "draft")))
                cowner = g2.text_input("Owner", cert.get("owner") or "")
                if st.form_submit_button("Apply", type="primary"):
                    ed.update_class(onto, cid, role=role, description=desc,
                                    synonyms=syn.split(",") if syn else [])
                    gv.certify(onto, "class", cid, cstatus, owner=cowner or None, by=gui.current_user(),
                               domains=[d for d in doms.split(",") if d.strip()])
                    if new_name != cid:
                        try:
                            new = ed.rename_class(onto, cid, new_name)
                            ss.selection = {"kind": "node", "id": new}
                        except ValueError as e:
                            st.error(str(e))
                            st.stop()
                    commit(f"Updated {new_name}")
            st.markdown("**Columns** — role & aggregation")
            rows = [{"property": pid, "column": p.get("source_column", pid), "type": p.get("range"),
                     "role": p.get("semantic_role", "dimension"), "aggregation": p.get("aggregation"),
                     "description": p.get("description") or ""}
                    for pid, p in c["data_properties"].items()]
            edited = st.data_editor(
                pd.DataFrame(rows), hide_index=True, width="stretch", key=f"props_{cid}",
                disabled=["property", "column", "type"],
                column_config={"role": st.column_config.SelectboxColumn(options=PROP_ROLES),
                               "aggregation": st.column_config.SelectboxColumn(options=AGGS)})
            if st.button("Apply column changes", key=f"apply_props_{cid}"):
                for r in edited.itertuples(index=False):
                    ed.update_property(onto, cid, r.property, semantic_role=r.role,
                                       aggregation=r.aggregation if isinstance(r.aggregation, str) else None,
                                       description=r.description)
                commit(f"Updated columns of {cid}")
            st.markdown("**Metrics**")
            mine = [x for x in onto.get("metrics", []) if x.get("base_class") == cid]
            for x in mine:
                a, b = st.columns([5, 1])
                a.markdown(f"`{x['name']}` = `{x['sql']}`")
                if b.button("✕", key=f"delm_{cid}_{x['name']}", help="Delete metric"):
                    ed.delete_metric(onto, x["name"])
                    commit(f"Deleted metric {x['name']}")
            with st.form(f"addm_{cid}", clear_on_submit=True):
                st.caption("Add / replace a metric (SQL over this class's columns)")
                mn = st.text_input("Name", placeholder="gross_margin")
                msql = st.text_input("SQL", placeholder="SUM(net_amount) - SUM(cost)")
                mdesc = st.text_input("Description")
                if st.form_submit_button("Save metric") and mn and msql:
                    ed.upsert_metric(onto, mn, cid, msql, mdesc)
                    commit(f"Saved metric {mn}")

        # ---------------- relationship inspector ----------------
        elif sel.get("kind") == "edge" and sel.get("edge_kind") == "rel" and sel.get("pid") in ops:
            pid = ed.forward_pid(onto, sel["pid"])
            p = ops[pid]
            st.subheader(pid)
            st.caption(f'{p["domain"]} → {p["range"]} · provenance: {p.get("provenance", "?")}'
                       + (f' · confidence {p["confidence"]:.0%}' if p.get("confidence") else ""))
            dcols, rcols = ed.class_columns(onto, p["domain"]), ed.class_columns(onto, p["range"])
            cur_from = [x.rpartition(".")[2] for x in p["join"]["from"]]
            cur_to = [x.rpartition(".")[2] for x in p["join"]["to"]]
            with st.form(f"rel_{pid}"):
                name = st.text_input("Name", pid)
                card = st.selectbox("Cardinality", CARDS, index=CARDS.index(p["cardinality"])
                                    if p["cardinality"] in CARDS else 0)
                fcols = st.multiselect(f"{p['domain']} columns (key side)", dcols,
                                       default=[x for x in cur_from if x in dcols])
                tcols = st.multiselect(f"{p['range']} columns (referenced)", rcols,
                                       default=[x for x in cur_to if x in rcols])
                c1, c2 = st.columns(2)
                apply_ = c1.form_submit_button("Apply", type="primary")
                delete_ = c2.form_submit_button("Delete")
            if apply_:
                if len(fcols) != len(tcols) or not fcols:
                    st.error("Pick the same number of columns on both sides.")
                else:
                    new = ed.update_relationship(onto, pid, fcols, tcols, card, new_name=name)
                    ss.selection = {"kind": "edge", "edge_kind": "rel", "pid": new, "id": f"rel:{new}"}
                    commit(f"Updated {new}")
            if delete_:
                ed.delete_relationship(onto, pid)
                ss.selection = {"kind": None}
                commit(f"Deleted {pid}")
            st.caption("Repoint either end from the diagram toolbar.")

        # ---------------- suggestion inspector ----------------
        elif sel.get("kind") == "edge" and sel.get("edge_kind") == "sug":
            s = next((x for x in onto.get("suggested_relationships", []) if x["id"] == sel.get("sid")), None)
            if s:
                st.subheader("Suggested relationship")
                st.markdown(f"**{s['domain']}**.`{s['from_cols'][0]}` → **{s['range']}**.`{s['to_cols'][0]}`")
                st.progress(float(s["confidence"]), f"confidence {s['confidence']:.0%}")
                for e in s.get("evidence", []):
                    st.write("•", e)
                c1, c2 = st.columns(2)
                if c1.button("✓ Accept", type="primary", use_container_width=True):
                    pid = ed.accept_suggestion(onto, s["id"])
                    ss.selection = {"kind": "edge", "edge_kind": "rel", "pid": pid, "id": f"rel:{pid}"}
                    commit(f"Accepted → {pid}")
                if c2.button("✕ Reject", use_container_width=True):
                    ed.reject_suggestion(onto, s["id"])
                    ss.selection = {"kind": None}
                    commit("Suggestion rejected")

        # ---------------- lineage inspector ----------------
        elif sel.get("kind") == "edge" and sel.get("edge_kind") == "lin":
            idx = int(str(sel.get("id", "lin:0")).split(":")[1])
            lin = onto.get("lineage", {}).get("tables", [])
            if idx < len(lin):
                e = lin[idx]
                st.subheader("Lineage")
                st.markdown(f"**{e['source']}** feeds **{e['target']}**")
                st.caption(f"{e.get('events', 0)} events · last seen {e.get('last_seen', '?')}")
                colmap = [c for c in onto["lineage"].get("columns", [])
                          if c["source"].rsplit(".", 1)[0] == e["source"]
                          and c["target"].rsplit(".", 1)[0] == e["target"]]
                if colmap:
                    st.dataframe(pd.DataFrame([{"from": c["source"].rsplit(".", 1)[1],
                                                "to": c["target"].rsplit(".", 1)[1],
                                                "events": c["events"]} for c in colmap]),
                                 hide_index=True, width="stretch")

        # ---------------- nothing selected ----------------
        else:
            st.subheader("Inspector")
            st.caption("Click a class or a relationship in the diagram to edit it.")
            with st.form("add_rel"):
                st.markdown("**Add a relationship**")
                names = list(classes)
                d = st.selectbox("From (holds the key)", names)
                r = st.selectbox("To (referenced)", names, index=min(1, len(names) - 1))
                card = st.selectbox("Cardinality", CARDS)
                if st.form_submit_button("Add", type="primary"):
                    pid = ed.add_relationship(onto, d, r, cardinality=card)
                    ss.selection = {"kind": "edge", "edge_kind": "rel", "pid": pid, "id": f"rel:{pid}"}
                    commit(f"Added {pid}")
            if pending:
                st.markdown(f"**{len(pending)} suggestions** waiting")
                c1, c2 = st.columns(2)
                if c1.button("Accept all ≥ 60%", use_container_width=True):
                    for s in list(pending):
                        if s["confidence"] >= 0.6:
                            ed.accept_suggestion(onto, s["id"])
                    commit("Accepted suggestions ≥ 60%")
                if c2.button("Reject all", use_container_width=True):
                    for s in pending:
                        ed.reject_suggestion(onto, s["id"])
                    commit("Rejected all suggestions")
            if st.button("Re-classify facts / dimensions / measures"):
                semantics.classify(onto, overwrite=True)
                commit("Re-classified")

# =========================================================================== #
# Ontology YAML
# =========================================================================== #
yaml_text = to_yaml(onto)
with tab_yaml:
    issues = validate(onto)
    if issues:
        with st.expander(f"⚠️ {len(issues)} validation note(s)"):
            for i in issues:
                st.write("•", i)
    include_layout = st.checkbox("Include diagram layout in the export", value=True)
    export_text = yaml_text if include_layout else to_yaml({k: v for k, v in onto.items() if k != "layout"})
    edited_yaml = st.text_area("Ontology (edit and apply to update the diagram)", export_text, height=480,
                               key=f"yaml_{hash(export_text)}")
    c1, c2, c3 = st.columns(3)
    fname = re.sub(r"\W+", "_", onto["ontology"]["name"]).strip("_").lower() + ".ontology.yaml"
    c1.download_button("⬇️ Download ontology YAML", edited_yaml, file_name=fname,
                       mime="application/x-yaml", type="primary", use_container_width=True)
    if c2.button("Apply YAML edits", use_container_width=True, disabled=edited_yaml == export_text):
        try:
            new = yaml.safe_load(edited_yaml)
            if isinstance(new, dict) and "datasets" in new and "classes" not in new:
                raise ValueError("this is an Apache Ossie semantic model, not a studio ontology. Load it with "
                                 "Source → Ossie YAML in the sidebar so it is validated first.")
            assert isinstance(new, dict) and "classes" in new and "object_properties" in new
            new.setdefault("layout", onto.get("layout", {}))
            ss.onto = refresh_index(new)
            ss.graph_key += 1
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Not a valid ontology: {e}")
    if store.enabled():
        if c3.button("💾 Save version to Volume", use_container_width=True):
            from modules.db_connection import get_user_token
            try:
                st.success(store.save(yaml_text, onto["ontology"]["name"], get_user_token()))
                ss.uc_cache.pop("versions", None)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    else:
        c3.caption("Set ONTOLOGY_STORE to save versions to a Volume.")

# =========================================================================== #
# Apache Ossie: validation report, export, validate any document
# =========================================================================== #
with tab_ossie:
    st.markdown(f"[Apache Ossie](https://github.com/apache/ossie) is the vendor-neutral exchange format for "
                f"semantic models. Files are checked against the official JSON Schema of the version they "
                f"declare (current: `{ossie.CURRENT_VERSION}`), then with the reference validator's semantic "
                "checks and a few modelling lints.")
    pending_rep = ss.get("ossie_report")
    loaded = ss.get("ossie_loaded")
    if pending_rep is not None:
        is_loaded = bool(loaded and loaded.get("key") == ss.get("ossie_key"))
        st.subheader(f"📄 {pending_rep.file_name}" + ("" if is_loaded else " (uploaded, not loaded)"))
        for n in (loaded or {}).get("notes", []) if is_loaded else []:
            st.caption(f"Loaded after upgrade: {n}")
        ossie_ui.report_view(pending_rep, key="tab")
        st.divider()

    st.subheader(f"⬇️ Export this ontology as Apache Ossie {ossie.CURRENT_VERSION}")
    oc1, oc2 = st.columns(2)
    keep_studio = oc1.checkbox("Keep studio details as ONTOLOGY_STUDIO extensions", value=True,
                               help="Roles, aggregations, value meanings, rules and provenance travel in "
                                    "nonportable_extensions, so a round trip through Ossie loses nothing. "
                                    "Other tools ignore them.")
    keep_layout = oc2.checkbox("Include the diagram layout", value=False)
    fingerprint = json.dumps({k: v for k, v in onto.items() if keep_layout or k != "layout"},
                             sort_keys=True, default=str)
    xkey = (hashlib.sha1(fingerprint.encode("utf-8")).hexdigest(), keep_studio, keep_layout)
    if ss.get("ossie_export_key") != xkey:
        x_doc, x_notes = ossie.from_ontology(onto, include_studio=keep_studio, include_layout=keep_layout)
        x_text = ossie.dump_yaml(x_doc)
        x_name = ossie_ui.stem(str(x_doc.get("name") or "model")) + ".ossie.yaml"
        ss.ossie_export = {"text": x_text, "notes": x_notes, "name": x_name,
                           "report": ossie.validate_text(x_text, x_name), "doc": x_doc}
        ss.ossie_export_key = xkey
    ex = ss.ossie_export
    ossie_ui.headline(ex["report"])
    for n in ex["notes"]:
        st.caption("• " + n)
    d1, d2, _ = st.columns([1.3, 1.3, 2])
    d1.download_button("⬇️ Ossie YAML", ex["text"], file_name=ex["name"], mime="application/x-yaml",
                       type="primary", use_container_width=True)
    d2.download_button("⬇️ Ossie JSON", json.dumps(ex["doc"], indent=2, default=str),
                       file_name=ex["name"].replace(".yaml", ".json"), mime="application/json",
                       use_container_width=True)
    with st.expander("Preview the exported document"):
        st.code(ex["text"], language="yaml")
    if ex["report"].findings:
        with st.expander(f"Export check details ({len(ex['report'].findings)} finding(s))"):
            ossie_ui.report_view(ex["report"], key="export", show_stats=False, upgrade=False)

    st.divider()
    st.subheader("🔎 Validate another document")
    v1, v2 = st.columns([2, 1])
    pasted = v1.text_area("Paste Ossie YAML or JSON", height=180, key="ossie_paste",
                          placeholder="version: 0.3.0.dev0\nname: sales\ndatasets:\n  - name: orders\n    "
                                      "source: main.sales.orders")
    v_strict = v2.checkbox("Require the current version", value=False, key="ossie_paste_strict")
    v2.caption("Nothing is loaded or changed; this only checks the text.")
    if v2.button("Validate", type="primary", disabled=not pasted.strip(), use_container_width=True):
        ss.ossie_paste_report = ossie.validate_text(pasted, "pasted.yaml", strict=v_strict)
    if ss.get("ossie_paste_report") is not None and pasted.strip():
        ossie_ui.report_view(ss.ossie_paste_report, key="paste")

# =========================================================================== #
# Metric views
# =========================================================================== #
with tab_mv:
    facts = mv.fact_classes(onto)
    if not facts:
        st.info("No fact classes found. Mark a class as **fact** in the inspector (or give it measure "
                "columns) to generate a metric view.")
    else:
        views = {f: mv.to_yaml(mv.build_metric_view(onto, f)) for f in facts}
        fsel = st.selectbox("Fact", facts, format_func=lambda f: f"{f} ({classes[f].get('source_table')})")
        st.code(views[fsel], language="yaml")
        fq = classes[fsel].get("source_fqn")
        default_target = (".".join(fq.split(".")[:2]) + "." if fq else "") + mv.view_name(onto, fsel)
        target = st.text_input("Metric view name (catalog.schema.view)", default_target, key=f"mvt_{fsel}")
        ddl = mv.ddl(views[fsel], target)
        with st.expander("Deploy SQL"):
            st.code(ddl, language="sql")
        c1, c2, c3 = st.columns(3)
        c1.download_button("⬇️ This metric view (.yaml)", views[fsel], file_name=f"{mv.view_name(onto, fsel)}.yaml",
                           mime="application/x-yaml", use_container_width=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f, y in views.items():
                z.writestr(f"{mv.view_name(onto, f)}.yaml", y)
                tgt = (".".join(classes[f]["source_fqn"].split(".")[:2]) + "." if classes[f].get("source_fqn")
                       else "") + mv.view_name(onto, f)
                z.writestr(f"{mv.view_name(onto, f)}.sql", mv.ddl(y, tgt))
            z.writestr("ontology.yaml", yaml_text)
        c2.download_button("⬇️ All metric views + ontology (.zip)", buf.getvalue(),
                           file_name="metric_views.zip", mime="application/zip", use_container_width=True)
        can_deploy = ss.get("runner", {}).get("kind") == "uc" and target.count(".") == 2
        if c3.button("🚀 Deploy to Unity Catalog", disabled=not can_deploy, use_container_width=True,
                     help="Runs CREATE OR REPLACE VIEW … WITH METRICS with your permissions "
                          "(needs CREATE TABLE on the schema)."):
            try:
                uc_runner()(ddl)
                st.success(f"Created metric view {target}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Deploy failed: {e}")

# =========================================================================== #
# Semantic layer
# =========================================================================== #
with tab_sem:
    st.markdown("**Value meanings** — what coded values stand for")
    vms = onto.get("value_meanings", [])
    if vms:
        vm_rows = [{"class": v["class"], "property": v["property"], "code": code, "meaning": meaning,
                    "provenance": v.get("provenance")} for v in vms for code, meaning in v["values"].items()]
        edited_vm = st.data_editor(pd.DataFrame(vm_rows), hide_index=True, width="stretch",
                                   disabled=["class", "property", "code", "provenance"], key="vm_editor")
        if st.button("Apply value meanings"):
            for r in edited_vm.itertuples(index=False):
                for v in vms:
                    if v["class"] == r[0] and v["property"] == r[1] and r.meaning \
                            and v["values"].get(r.code) != r.meaning:
                        v["values"][r.code] = r.meaning
                        v["provenance"] = "manual"
            commit("Value meanings updated")
    else:
        st.caption("No coded columns detected (turn on data profiling to find them).")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Metrics**")
        if onto.get("metrics"):
            st.dataframe(pd.DataFrame(onto["metrics"]), hide_index=True, width="stretch")
    with c2:
        st.markdown("**Business rules**")
        if onto.get("rules"):
            st.dataframe(pd.DataFrame(onto["rules"]), hide_index=True, width="stretch")
        else:
            st.caption("None yet — enable LLM enrichment and upload docs that describe them.")
    st.markdown("**Synonyms index**")
    st.dataframe(pd.DataFrame([{"term": k, "maps to": v if isinstance(v, str) else ", ".join(v)}
                               for k, v in onto.get("synonyms_index", {}).items()]),
                 hide_index=True, width="stretch", height=260)

# =========================================================================== #
# Tables
# =========================================================================== #
with tab_tables:
    st.markdown("**Classes**")
    st.dataframe(pd.DataFrame([{"class": cid, "role": c.get("role"), "table": c.get("source_fqn")
                                or c.get("source_table"), "rows": c.get("instance_count"),
                                "identifier": ", ".join(c.get("identifier", [])),
                                "pk inferred": c.get("identifier_inferred", False),
                                "measures": sum(p.get("semantic_role") == "measure"
                                                for p in c["data_properties"].values())}
                               for cid, c in classes.items()]), hide_index=True, width="stretch")
    st.markdown("**Relationships**")
    st.dataframe(pd.DataFrame([{"property": pid, "domain": p["domain"], "range": p["range"],
                                "cardinality": p["cardinality"], "provenance": p.get("provenance"),
                                "confidence": p.get("confidence"),
                                "join": ", ".join(p["join"]["from"]) + " → " + ", ".join(p["join"]["to"])}
                               for pid, p in ops.items() if p.get("join")]), hide_index=True, width="stretch")
    if onto.get("suggested_relationships"):
        st.markdown("**Suggestions**")
        st.dataframe(pd.DataFrame([{k: (", ".join(v) if isinstance(v, list) else v) for k, v in s.items()}
                                   for s in onto["suggested_relationships"]]), hide_index=True, width="stretch")

# =========================================================================== #
# Governed + learned knowledge, Ask, Monitor
# =========================================================================== #
with tab_home:
    gui.home_tab(onto, ss.get("runner", {}), uc_runner, ON_DATABRICKS)
with tab_know:
    gui.knowledge_tab(onto, ss.get("runner", {}), uc_runner, ON_DATABRICKS)
with tab_review:
    gui.review_tab(onto, ON_DATABRICKS)
with tab_ask:
    gui.ask_tab(onto, ss.get("runner", {}), uc_runner, ON_DATABRICKS)
with tab_mon:
    gui.monitor_tab(uc_runner, ON_DATABRICKS)
