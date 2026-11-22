"""
Streamlit UI for the semantic-model lifecycle:

  versions_tab()  Publish → Save & versions: save the open model as a new version in ONTOLOGY_STORE
                  (the one save in the app), browse the history, compare two versions, open or restore one
  deploy_page()   Publish → Deploy: put an Ossie model into Databricks next to its tables, plus metric
                  views, tags, keys and a Genie space (the one place the app writes to Unity Catalog)
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Callable

import pandas as pd
import streamlit as st

from . import deploy as dp
from . import ossie, ossie_ui, store

ss = st.session_state


def current_user() -> str:
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username") or ""
    except Exception:  # noqa: BLE001
        return ""


def _deploy_log(catalog: str, schema: str, volume: str, model: str, token: str | None) -> list[dict]:
    key = ("dp_log", catalog, schema, volume, model, (ss.get("dp_result") or {}).get("version"))
    if ss.get("_dp_log_key") != key:
        try:
            ss._dp_log = dp.read_log(catalog, schema, volume, model, token) if catalog and schema else []
        except Exception:  # noqa: BLE001
            ss._dp_log = []
        ss._dp_log_key = key
    return ss._dp_log


def ossie_text(onto: dict) -> tuple[str, dict, object]:
    """The open studio model as Apache Ossie YAML (studio details kept as extensions), its doc and report."""
    import json
    fp = hashlib.sha1(json.dumps({k: v for k, v in onto.items() if k != "layout"}, sort_keys=True,
                                 default=str).encode("utf-8")).hexdigest()
    cache = ss.get("_lc_ossie")
    if not cache or cache[0] != fp:
        doc, _ = ossie.from_ontology(onto, include_studio=True, include_layout=False)
        text = ossie.dump_yaml(doc)
        cache = (fp, text, doc, ossie.validate_text(text, "model.ossie.yaml"))
        ss["_lc_ossie"] = cache
    return cache[1], cache[2], cache[3]


def default_model_name(onto: dict | None) -> str:
    ref = ss.get("model_ref") or {}
    if ref.get("model"):
        return ref["model"]
    if onto:
        return store.slug(onto.get("ontology", {}).get("name") or "model")
    return "model"


def forget_store_cache() -> None:
    """Drop the sidebar's cached model / version lists after a save."""
    cache = ss.get("uc_cache", {})
    for k in [k for k in cache if k in ("store_models", "versions") or str(k).startswith("hist:")]:
        cache.pop(k, None)
    ss.pop("vt_diff", None)


def _store_missing() -> None:
    st.info("Versioning is off. Set **ONTOLOGY_STORE** in `app.yaml` to a `/Volumes/<catalog>/<schema>/<volume>` "
            "path (the app needs READ VOLUME and WRITE VOLUME on it), then redeploy the app. Until then, download "
            "the model in **Publish → Exports** and import it again in **Connect**.")


# --------------------------------------------------------------------------- #
# Publish → Save & versions
# --------------------------------------------------------------------------- #
def save_box(onto: dict, token: str | None, key: str, compact: bool = False) -> None:
    """Save the open model to the store as a new Ossie version."""
    if not store.enabled():
        _store_missing()
        return
    c1, c2 = st.columns([1, 2]) if not compact else (st, st)
    model = c1.text_input("Model folder", default_model_name(onto), key=f"{key}_model",
                          help="Versions of the same model share a folder under ONTOLOGY_STORE.")
    note = c2.text_input("What changed (optional)", key=f"{key}_note",
                         placeholder="e.g. added customer churn metric")
    text, _, rep = ossie_text(onto)
    ossie_ui.headline(rep)
    if st.button("💾 Save as new version", type="primary", key=f"{key}_save", use_container_width=compact):
        try:
            v = store.save(text, model, token, kind=store.OSSIE, note=note, author=current_user(), report=rep)
            ss.model_ref = {"model": v.model, "version": v.version, "path": v.path, "text": text}
            forget_store_cache()
            st.success(f"Saved {v.path} (as {v.written_as}).")
        except Exception as e:  # noqa: BLE001
            st.error(str(e))


def versions_tab(onto: dict, token: str | None, on_load: Callable[[str, str], None]) -> None:
    st.markdown("Every save is a new file; nothing is overwritten. The store keeps who saved each version, "
                "when, and why, so you can compare two versions or bring an old one back.")
    if not store.enabled():
        _store_missing()
        return
    st.caption(f"Store: `{store.root()}`")
    st.subheader("💾 Save the open model")
    save_box(onto, token, key="vt")

    ref = ss.get("model_ref") or {}
    if ref.get("text"):
        now, _, _ = ossie_text(onto)
        if now != ref["text"]:
            with st.expander(f"Unsaved changes since version {store.pretty_stamp(ref.get('version', ''))}"):
                st.code(store.diff(ref["text"], now, "saved", "open model") or "(no textual change)",
                        language="diff")
        else:
            st.caption(f"No changes since version {store.pretty_stamp(ref.get('version', ''))}.")

    st.divider()
    st.subheader("🗂️ History")
    models = store.list_models(token)
    if not models:
        st.caption("No versions saved yet.")
        return
    cur = default_model_name(onto)
    model = st.selectbox("Model", models, index=models.index(cur) if cur in models else 0, key="vt_hist_model")
    hist = store.history(model, token)
    if not hist:
        st.caption("This folder has no versions.")
        return
    st.dataframe(pd.DataFrame([v.as_row() for v in hist]).drop(columns=["path"]), hide_index=True,
                 use_container_width=True)
    labels = {v.path: v.label() for v in hist}
    paths = [v.path for v in hist]
    c1, c2 = st.columns(2)
    k = f"{model}_{hist[0].version}_{len(hist)}"          # fresh pickers whenever a version is added
    a = c1.selectbox("Version", paths, format_func=labels.get, key=f"vt_a_{k}")
    b = c2.selectbox("Compare with", paths, index=min(1, len(paths) - 1), format_func=labels.get, key=f"vt_b_{k}")
    va = next(v for v in hist if v.path == a)
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("Compare", use_container_width=True, disabled=a == b):
        try:
            ss.vt_diff = store.diff(store.load(b, token), store.load(a, token),
                                    labels[b].split(" · ")[0], labels[a].split(" · ")[0]) or "(identical)"
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    if b2.button("Open in the studio", use_container_width=True):
        try:
            on_load(store.load(a, token), a)
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    if b3.button("Restore as newest", use_container_width=True,
                 help="Copies this version forward as a new version; the history is kept."):
        try:
            v = store.restore(va, token, author=current_user())
            forget_store_cache()
            st.success(f"Restored as {v.path}.")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    try:
        b4.download_button("⬇️ Download", store.load(a, token), file_name=a.rsplit("/", 1)[-1],
                           mime="application/x-yaml", use_container_width=True, key=f"vt_dl_{a}")
    except Exception as e:  # noqa: BLE001
        b4.caption(f"Cannot read: {str(e)[:80]}")
    if ss.get("vt_diff"):
        st.code(ss.vt_diff, language="diff")


# --------------------------------------------------------------------------- #
# Publish → Deploy
# --------------------------------------------------------------------------- #
def deploy_page(onto: dict | None, token: str | None, runner: Callable | None, on_databricks: bool) -> None:
    st.markdown("Puts the Apache Ossie YAML in a Unity Catalog **volume inside the same schema as its tables**, "
                "so the model travels with the data and inherits its permissions, and, if you tick them, writes "
                "table and column comments, tags, metric views, informational keys and a **Genie space**. You see "
                "everything before it runs, and it runs with your permissions.")

    choices = []
    if onto is not None:
        choices.append("Model open in the studio")
    if store.enabled():
        choices.append("A saved version")
    if not choices:
        st.info("Open a model in **Connect** first (or set ONTOLOGY_STORE to deploy saved versions).")
        return
    pick = st.radio("What to deploy", choices, horizontal=True, key="dp_pick")

    text, label = None, ""
    if pick == "Model open in the studio":
        text, _, _ = ossie_text(onto)
        label = default_model_name(onto)
    else:
        versions = [v for v in store.all_versions(token) if v.kind == store.OSSIE]
        if not versions:
            st.info("No versions in the store yet. Save one in **Save & versions**.")
            return
        sel = st.selectbox("Version", versions, format_func=lambda v: f"{v.model} · {v.label()}", key="dp_ver")
        try:
            text = store.load(sel.path, token)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            return
        label = sel.model
    if not text:
        return

    rkey = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if ss.get("dp_rep_key") != rkey:
        ss.dp_rep_key, ss.dp_rep = rkey, ossie.validate_text(text, "deploy.ossie.yaml")
    rep = ss.dp_rep
    ossie_ui.headline(rep)
    if not rep.convertible:
        ossie_ui.report_view(rep, key="deploy")
        return
    doc = rep.document
    if rep.profile is not None and rep.profile.wrapped:
        doc = ossie.upgrade(doc)[0][0]
        text = ossie.dump_yaml(doc)
    if rep.errors:
        with st.expander("Validation details"):
            ossie_ui.report_view(rep, key="deploy", show_stats=False, upgrade=False)
        st.error("Fix the errors before deploying: they are listed in **Review → Blockers**.")
        return

    model_onto = onto if pick == "Model open in the studio" else None
    if model_onto is None:
        try:
            model_onto = ossie.to_ontology(doc, "deploy.ossie.yaml")
        except Exception:  # noqa: BLE001
            model_onto = None

    st.subheader("Target")
    dcat, dsch = dp.default_target(doc)
    n_uc = len(dp.uc_datasets(doc))
    n_all = len(doc.get("datasets") or [])
    st.caption(f"{n_uc} of {n_all} dataset(s) are Unity Catalog tables"
               + (f"; most live in `{dcat}.{dsch}`." if dcat else "."))
    t1, t2, t3, t4 = st.columns(4)
    model = t1.text_input("Model name", store.slug(label or doc.get("name") or "model"), key=f"dp_model_{rkey}")
    catalog = t2.text_input("Catalog", dcat, key=f"dp_cat_{rkey}")
    schema = t3.text_input("Schema", dsch, key=f"dp_sch_{rkey}")
    volume = t4.text_input("Volume", "semantic_models", key="dp_vol")

    st.subheader("What to deploy")
    o1, o2, o3 = st.columns(3)
    create_volume = o1.checkbox("Create the volume if missing", value=True, key="dp_o_vol")
    comments = o1.checkbox("Descriptions → table & column comments", value=True, key="dp_o_com", disabled=not n_uc)
    tags = o2.checkbox("Tag tables: model, version, domain, certification, owner", value=True, key="dp_o_tag",
                       disabled=not n_uc)
    mviews = o2.checkbox("Create metric views (one per fact, plus the ones you built)", value=True, key="dp_o_mv",
                         disabled=not n_uc)
    app_sp = os.getenv("DATABRICKS_CLIENT_ID", "")
    grant = o3.checkbox("Let the app write the files", value=bool(app_sp), key="dp_o_grant", disabled=not app_sp,
                        help="Grants the app's service principal USE CATALOG, USE SCHEMA and READ/WRITE VOLUME on "
                             "this volume (as you), so the YAML can still be written when your sign-in has no file "
                             "access (OAuth 'files' scope).")
    cons = o3.checkbox("Informational PK / FK constraints", value=False, key="dp_o_con", disabled=not n_uc,
                       help="NOT ENFORCED keys. Primary keys need NOT NULL columns; existing constraints fail.")
    wid = os.getenv("SQL_WAREHOUSE_ID", "")
    wid = "" if wid.startswith("REPLACE") else wid
    genie = o1.checkbox("Create or update a Genie space", value=False, key="dp_o_genie",
                        disabled=not (n_uc and wid),
                        help="Tables and metric views, instructions from rules, glossary, code values and certified "
                             "pages, joins, and the test questions as sample questions and benchmarks. Needs the "
                             "app's `genie` user authorization scope." + ("" if wid else " Needs SQL_WAREHOUSE_ID."))

    if not (catalog and schema and volume):
        st.info("Enter the target catalog, schema and volume.")
        return
    log = _deploy_log(catalog.strip(), schema.strip(), volume.strip(), model, token) if on_databricks else []
    p = dp.plan(doc, model_onto, catalog.strip(), schema.strip(), volume.strip(), create_volume=create_volume,
                comments=comments and n_uc > 0, tags=tags and n_uc > 0, metric_views=mviews and n_uc > 0,
                constraints=cons and n_uc > 0, model_name=model, yaml_text=text,
                grant_app=app_sp if grant and app_sp else None, genie=genie and n_uc > 0,
                genie_space_id=dp.last_genie_space(log))
    if model_onto is not None:
        from . import testset as ts
        t = ts.status(model_onto)
        if t["total"] and (t["stale"] or t["passed"] < t["total"]):
            st.warning(f"Test set: {t['verdict']}. Run it in **Test** before you publish.")
        elif t["total"]:
            st.caption(f"✅ Test set: {t['verdict']}.")

    st.subheader("Preview")
    counts = p.counts()
    m = st.columns(6)
    m[0].metric("Files", len(p.files) + 1)
    m[1].metric("Comments", counts.get("comment", 0))
    m[2].metric("Tags", counts.get("tag", 0))
    m[3].metric("Metric views", counts.get("metric_view", 0))
    m[4].metric("Constraints", counts.get("constraint", 0))
    m[5].metric("Genie space", ("update" if p.genie["space_id"] else "new") if p.genie else "–")
    if counts.get("grant"):
        st.caption(f"• The app's service principal (`{app_sp}`) gets READ / WRITE VOLUME on "
                   f"`{p.catalog}.{p.schema}.{p.volume}` (and USE on its catalog and schema).")
    st.markdown("**Files**\n" + "\n".join(f"- `{f}`" for f in [*p.files, f"{p.folder}/deployments.json"]))
    for n in p.notes:
        st.caption("• " + n)
    with st.expander(f"SQL ({len(p.steps)} statement(s))"):
        st.code(p.script() or "-- no SQL, files only", language="sql")
    with st.expander("YAML to deploy"):
        st.code(text, language="yaml")
    if p.genie:
        from . import genie as gn
        sm = gn.summary(p.genie["definition"])
        with st.expander(f"Genie space “{p.genie['title']}”: {sm['tables']} tables, {sm['metric_views']} metric "
                         f"views, {sm['joins']} joins, {sm['examples']} example questions, {sm['benchmarks']} "
                         "benchmarks"):
            st.json(p.genie["definition"], expanded=False)
            st.download_button("⬇️ Genie space definition (.json)", json.dumps(p.genie["definition"], indent=2),
                               file_name=f"{p.model}_genie_space.json", mime="application/json")
            st.caption("Without the app's `genie` scope, create the space yourself with this definition:")
            st.code(gn.curl_command(p.genie["definition"], wid, p.genie["title"], p.genie["description"]),
                    language="bash")

    if not on_databricks:
        st.warning("Deploying needs the app to run in Databricks Apps. Download the SQL and YAML instead:")
    c1, c2, c3 = st.columns([1.2, 1, 1])
    go = c1.button("🚀 Deploy", type="primary", use_container_width=True, disabled=not on_databricks or runner is None)
    c2.download_button("⬇️ SQL script", p.script(), file_name=f"deploy_{p.model}.sql", use_container_width=True)
    c3.download_button("⬇️ YAML", text, file_name=f"{p.model}.ossie.yaml", mime="application/x-yaml",
                       use_container_width=True)
    if go:
        bar = st.progress(0.0, "Deploying…")
        from . import genie as gn
        result = dp.execute(p, runner(), token, author=current_user(), progress=lambda f, msg: bar.progress(f, msg),
                            genie_fn=lambda d, t, ds, sid: gn.publish(d, wid, t, ds, token, sid))
        bar.empty()
        ss.dp_result = result
    res = ss.get("dp_result")
    if res and res.get("target") == f"{catalog.strip()}.{schema.strip()}" and res.get("model") == p.model:
        (st.success if res["ok"] else st.warning)(
            f"Deployed {res['model']} version {store.pretty_stamp(res['version'])} to `{res['target']}`"
            + ("" if res["ok"] else " with some failures (see below)") + ".")
        g = res.get("genie")
        if g and g.get("status") == "ok":
            st.success(f"Genie space {g.get('action', 'published')}" + (f": [open it]({g['url']})" if g.get("url")
                                                                         else f" (id {g.get('space_id')})"))
        elif g:
            st.error(f"Genie space failed: {g.get('message')}. Download its definition above and create it "
                     "with the API, or ask an admin to add the `genie` scope to the app.")
        if res["file_errors"]:
            st.error(f"{len(res['file_errors'])} file(s) could not be written. Details:")
            st.code("\n\n".join(res["file_errors"]), language="text", wrap_lines=True)
        if res["steps"]:
            st.dataframe(pd.DataFrame(res["steps"]), hide_index=True, use_container_width=True)

    with st.expander("Previous deployments of this model here"):
        if log:
            st.dataframe(pd.DataFrame([{"version": store.pretty_stamp(e.get("version", "")),
                                        "deployed at": e.get("deployed_at"), "by": e.get("deployed_by"),
                                        "failed steps": sum(1 for s in e.get("steps", []) if s["status"] != "ok"),
                                        "files": len(e.get("files", [])),
                                        "genie": (e.get("genie") or {}).get("status", "")} for e in log]),
                         hide_index=True, use_container_width=True)
        else:
            st.caption("None yet.")
