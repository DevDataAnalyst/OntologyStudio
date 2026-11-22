"""Model: see and fix the structure.

  🕸️ Diagram   the editable D3 network with an inspector for classes, columns, metrics and joins
  📋 List      classes and relationships as tables, plus re-scan of the Unity Catalog tables
  ✏️ Form      the step-by-step form editor on the open model
  🧾 Source    (Advanced) the model as studio YAML, editable; accepts a pasted Ossie model too
"""
from __future__ import annotations


import pandas as pd
import streamlit as st
import yaml

from .. import governance as gv, governance_ui as gui, lifecycle_ui as lc, modeling as md, modeling_ui as mu
from .. import ontology_edit as ed, ossie_form, pipeline, semantics, theme, workbench_ui as wb
from ..graph_component import handle_event, network
from ..ontology_builder import refresh_index, to_yaml, validate
from .common import advanced, commit, ss, sub_view, uc_runner

ROLES = ["fact", "dimension", "entity", "bridge"]
PROP_ROLES = ["dimension", "measure", "time", "key"]
AGGS = ["SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT"]
CARDS = ["many-to-one", "one-to-one", "many-to-many"]
VIEWS = ["🕸️ Diagram", "📋 List", "✏️ Form"]
SOURCE = "🧾 Source"


def header(onto: dict) -> None:
    classes, ops = onto["classes"], onto["object_properties"]
    n_rel = sum(1 for p in ops.values() if p.get("join") and p["cardinality"] != "one-to-many")         - sum(1 for p in ops.values() if p["cardinality"] == "many-to-many") // 2
    pending = [s for s in onto.get("suggested_relationships", []) if s.get("status") == "proposed"]
    roles = pd.Series([c.get("role", "entity") for c in classes.values()]).value_counts().to_dict()
    m = st.columns(6)
    m[0].metric("Tables", len(classes))
    m[1].metric("Facts / Dims", f"{roles.get('fact', 0)} / {roles.get('dimension', 0)}")
    m[2].metric("Relationships", n_rel)
    m[3].metric("Suggested joins", len(pending))
    m[4].metric("Metrics", len(onto.get("metrics", [])))
    m[5].metric("Domains", len(onto.get("domains", [])))
    for w in onto.get("warnings", [])[:8]:
        st.warning(w)


def render(onto: dict) -> None:
    header(onto)
    view = sub_view("Model view", VIEWS + ([SOURCE] if advanced() else []), "md_view")
    if view == VIEWS[0]:
        _diagram(onto)
    elif view == VIEWS[1]:
        _list(onto)
    elif view == VIEWS[2]:
        st.caption("Changes made here replace the open model when you submit on the last step.")
        ossie_form.editor_tab(onto, pipeline.submit_form_model, allow_upload=False)
    else:
        _source(onto)


def _diagram(onto: dict) -> None:
    classes, ops = onto["classes"], onto["object_properties"]
    pending = [s for s in onto.get("suggested_relationships", []) if s.get("status") == "proposed"]
    left, right = st.columns([2.6, 1.2], gap="medium")
    with left:
        evt = network(onto, height=660, dark=theme.is_dark(), message=ss.graph_msg, selected=ss.selection,
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
            md.log_change(onto, msg, lc.current_user())
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
                cnote = st.text_input("Reviewer note", c.get("review_note") or "",
                                      placeholder="What a reviewer should know or check")
                if st.form_submit_button("Apply", type="primary"):
                    redo = cert.get("status") == "certified" and cstatus == "certified" and (
                        new_name != cid or desc.strip() != (c.get("description") or "").strip())
                    if redo:                           # a certified definition that changed needs a new review
                        cstatus = "draft"
                    ed.update_class(onto, cid, role=role, description=desc,
                                    synonyms=syn.split(",") if syn else [])
                    if cnote.strip():
                        c["review_note"] = cnote.strip()
                    else:
                        c.pop("review_note", None)
                    gv.certify(onto, "class", cid, cstatus, owner=cowner or None, by=gui.current_user(),
                               domains=[d for d in doms.split(",") if d.strip()])
                    if new_name != cid:
                        try:
                            new = ed.rename_class(onto, cid, new_name)
                            ss.selection = {"kind": "node", "id": new}
                        except ValueError as e:
                            st.error(str(e))
                            st.stop()
                    commit(f"Updated {new_name}" + (": its certified definition changed, so it is back to draft"
                                                    if redo else ""))
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
                st.caption("Add / replace a metric (SQL over this class's columns). With live data it is run "
                           "read-only first, and nothing is saved if the database rejects it.")
                mn = st.text_input("Name", placeholder="gross_margin")
                msql = st.text_input("SQL", placeholder="SUM(net_amount) - SUM(cost)")
                mdesc = st.text_input("Description")
                if st.form_submit_button("Save metric") and mn and msql:
                    run_ = wb.make_run(ss.get("runner", {}), uc_runner)
                    err = md.test_expression(run_, onto, cid, msql, "measure",
                                             wb.dialect_of(ss.get("runner", {}))) if run_ else None
                    if err:
                        st.error(f"Not saved, the database rejected it: {err}")
                    else:
                        ed.upsert_metric(onto, mn, cid, msql, mdesc)
                        commit(f"Saved metric {mn}" + ("" if run_ else " (not tested: no live data)"))

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
            siblings = [q for q, x in ops.items() if x.get("join") and x.get("domain") == p["domain"]
                        and x.get("range") == p["range"] and x.get("cardinality") in ("many-to-one", "one-to-one")]
            with st.form(f"rel_{pid}"):
                name = st.text_input("Name", pid)
                card = st.selectbox("Cardinality", CARDS, index=CARDS.index(p["cardinality"])
                                    if p["cardinality"] in CARDS else 0)
                fcols = st.multiselect(f"{p['domain']} columns (key side)", dcols,
                                       default=[x for x in cur_from if x in dcols])
                tcols = st.multiselect(f"{p['range']} columns (referenced)", rcols,
                                       default=[x for x in cur_to if x in rcols])
                rrole = st.text_input("Role", p.get("role") or "", placeholder="e.g. billing, delivery",
                                      help="The business role when several columns join the same two classes.")
                rdesc = st.text_input("Description", p.get("description") or "")
                rdef = st.checkbox("Default join between these two classes", value=bool(p.get("default")),
                                   disabled=len(siblings) < 2,
                                   help="Used by Ask, join paths and the SQL checker when a question does not say "
                                        "which role it means.")
                c1, c2 = st.columns(2)
                apply_ = c1.form_submit_button("Apply", type="primary")
                delete_ = c2.form_submit_button("Delete")
            if apply_:
                if len(fcols) != len(tcols) or not fcols:
                    st.error("Pick the same number of columns on both sides.")
                else:
                    new = ed.update_relationship(onto, pid, fcols, tcols, card, new_name=name)
                    for k, v in (("role", rrole.strip()), ("description", rdesc.strip())):
                        if v:
                            ops[new][k] = v
                        else:
                            ops[new].pop(k, None)
                    if len(siblings) > 1 and rdef:
                        md.set_default(onto, new)
                    elif not rdef:
                        ops[new].pop("default", None)
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
                st.caption(f"**{len(pending)} suggested joins** are dashed orange in the diagram. Click one to "
                           "accept or reject it, or settle them all in **Review**.")
            if st.button("Re-classify facts / dimensions / measures"):
                semantics.classify(onto, overwrite=True)
                commit("Re-classified")



def _list(onto: dict) -> None:
    classes, ops = onto["classes"], onto["object_properties"]
    mu.rescan_box(onto, ss.get("runner", {}), uc_runner, commit)
    st.markdown("**Classes**")
    st.dataframe(pd.DataFrame([{"class": cid, "role": c.get("role"), "table": c.get("source_fqn")
                                or c.get("source_table"), "rows": c.get("instance_count"),
                                "identifier": ", ".join(c.get("identifier", [])),
                                "pk inferred": c.get("identifier_inferred", False),
                                "measures": sum(p.get("semantic_role") == "measure"
                                                for p in c["data_properties"].values()),
                                "unmapped columns": len({x.lower() for x in c.get("source_columns") or []}
                                                        - {str(p.get("source_column") or k).lower()
                                                           for k, p in c["data_properties"].items()}),
                                "new columns": ", ".join(c.get("new_columns") or [])}
                               for cid, c in classes.items()]), hide_index=True, width="stretch")
    st.markdown("**Relationships**")
    st.dataframe(pd.DataFrame([{"property": pid, "domain": p["domain"], "range": p["range"],
                                "cardinality": p["cardinality"], "provenance": p.get("provenance"),
                                "confidence": p.get("confidence"),
                                "join": ", ".join(p["join"]["from"]) + " → " + ", ".join(p["join"]["to"])}
                               for pid, p in ops.items() if p.get("join")]), hide_index=True, width="stretch")



def _source(onto: dict) -> None:
    """The open model as studio YAML (Advanced): edit and apply, or paste an Apache Ossie model to open it."""
    yaml_text = to_yaml(onto)
    issues = validate(onto)
    if issues:
        with st.expander(f"⚠️ {len(issues)} validation note(s)"):
            for i in issues:
                st.write("•", i)
    edited = st.text_area("Studio model YAML (edit and apply; a pasted Apache Ossie model is validated and opened)",
                          yaml_text, height=480, key=f"yaml_{hash(yaml_text)}")
    if st.button("Apply YAML edits", disabled=edited == yaml_text):
        try:
            new = yaml.safe_load(edited)
            if isinstance(new, dict) and "datasets" in new and "classes" not in new:
                err = pipeline.open_text(edited, "pasted.ossie.yaml")
                if err:
                    raise ValueError(err)
                st.rerun()
            assert isinstance(new, dict) and "classes" in new and "object_properties" in new
            new.setdefault("layout", onto.get("layout", {}))
            ss.onto = refresh_index(new)
            ss.graph_key += 1
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Not a valid model: {e}")
    st.caption("Downloads (studio YAML, Apache Ossie, metric views, Power BI) are in **Publish → Exports**.")
