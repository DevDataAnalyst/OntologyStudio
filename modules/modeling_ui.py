"""
Streamlit views for the modeling assistant (modules/modeling.py):
  rescan_box     🗂️ Tables tab: re-scan the model's Unity Catalog tables; add tables from the same schemas
  worklist_view  ✅ Review queue → 🧭 Modeling worklist: tables to describe (✨ draft with the LLM), default joins,
                 unlinked id columns, joins seen in queries, filters to save as rules
  search_box     🧠 Semantic layer tab: search the model and open a Term card
  changes_view   🕘 Versions tab: the change log saved with the model
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from . import answering as an
from . import modeling as md
from . import ontology_edit as ed

ss = st.session_state


def _user() -> str:
    from .governance_ui import current_user
    return current_user()


# --------------------------------------------------------------------------- #
# Re-scan
# --------------------------------------------------------------------------- #
def rescan_box(onto: dict, runner: dict, uc_runner, commit) -> None:
    fqns = md.table_fqns(onto)
    with st.expander("🔄 Re-scan the tables in Unity Catalog", expanded=bool(ss.get("rescan") or ss.get("rescan_new"))):
        if not (fqns and runner.get("kind") == "uc" and uc_runner is not None):
            st.caption("Needs a model whose classes point at Unity Catalog tables (catalog.schema.table), opened in "
                       "Databricks Apps.")
            return
        st.caption(f"Reads the model's {len(fqns)} tables again: new, dropped and retyped columns, row counts and "
                   "profiles. Descriptions, synonyms, roles, certification, joins and metrics are kept; new columns "
                   "go to the 🧭 Modeling worklist.")
        if st.button("🔄 Re-scan now", key="rescan_go"):
            from . import uc
            try:
                with st.spinner(f"Reading {len(fqns)} tables…"):
                    ds = uc.extract_objects(uc_runner(), fqns, profile=True)
                ss.rescan = {"ds": ds, "report": md.drift(onto, ds)}
            except Exception as e:  # noqa: BLE001
                st.error(str(e)[:400])
        r = ss.get("rescan")
        if r:
            rep = r["report"]
            st.caption(f"{rep['unchanged']} table(s) unchanged"
                       + (f" · not found or not readable: {', '.join(rep['missing'])}" if rep["missing"] else ""))
            if rep["changed"]:
                st.dataframe(pd.DataFrame([{
                    "class": cid, "new columns": ", ".join(ch["new"]), "dropped": ", ".join(ch["dropped"]),
                    "retyped": ", ".join(f"{x['column']} {x['was']} → {x['now']}" for x in ch["retyped"])}
                    for cid, ch in rep["changed"].items()]), hide_index=True, width="stretch")
            if st.button("Apply to the model", type="primary", key="rescan_apply"):
                ss.rescan_notes = md.apply_drift(onto, r["ds"], rep)
                ss.pop("rescan", None)
                new = sum(len(ch["new"]) for ch in rep["changed"].values())
                gone = sum(len(ch["dropped"]) for ch in rep["changed"].values())
                commit(f"Re-scanned {len(fqns)} tables: {new} new and {gone} dropped column(s)")
        for n in ss.get("rescan_notes") or []:
            st.warning(n)
        st.markdown("**Tables in the same schemas that the model does not have**")
        c1, c2 = st.columns([3, 1])
        pats = c1.text_input("Filter", key="rescan_pats", placeholder="SALES_*, HR.EMP*, !*_LOG",
                             label_visibility="collapsed",
                             help="Case-insensitive globs: * and ? wildcards, % works like *, schema.table when the "
                                  "pattern has a dot, ! excludes. Empty = all.")
        if c2.button("List tables", key="rescan_list", use_container_width=True):
            try:
                ss.rescan_new = md.new_tables(uc_runner(), onto, pats)
            except Exception as e:  # noqa: BLE001
                st.error(str(e)[:400])
        cand = ss.get("rescan_new")
        if cand is not None and not cand:
            st.caption("No other tables match.")
        elif cand:
            pick = st.multiselect("Add to the model", cand, key="rescan_pick", max_selections=50,
                                  format_func=lambda f: ".".join(f.split(".")[1:]))
            if st.button("➕ Add selected", disabled=not pick, key="rescan_add"):
                try:
                    with st.spinner(f"Reading {len(pick)} table(s) and looking for joins…"):
                        res = md.add_tables(uc_runner(), onto, pick)
                except Exception as e:  # noqa: BLE001
                    st.error(str(e)[:400])
                else:
                    ss.pop("rescan_new", None)
                    commit(f"Added {len(res['classes'])} table(s); {res['suggestions']} join suggestion(s) to review "
                           "in the Network tab")


# --------------------------------------------------------------------------- #
# Modeling worklist
# --------------------------------------------------------------------------- #
def worklist_view(onto: dict, runner: dict, uc_runner, on_databricks: bool, commit, traces: list[dict]) -> None:
    from . import workbench_ui as wb
    run, dialect = wb.make_run(runner, uc_runner), wb.dialect_of(runner)
    st.caption("What is still unmodeled, most urgent first. Nothing here is certified for you: drafts stay drafts "
               "until a person certifies them.")
    st.markdown("#### Tables to describe")
    rows = md.worklist(onto)
    if not rows:
        st.success("Every table says what one row is, and every column is described.")
    else:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        c1, c2, c3 = st.columns([2, 1.4, 1.4])
        cid = c1.selectbox("Table", [r["class"] for r in rows], key="wl_cls", label_visibility="collapsed")
        if c2.button("✨ Draft with the LLM", disabled=not on_databricks, use_container_width=True,
                     help="Fills empty descriptions (with the grain), column descriptions, synonyms and code meanings "
                          "for this table from its columns and samples. Nothing a person wrote is overwritten."):
            from .llm_enrich import enrich
            try:
                with st.spinner(f"Drafting {cid}…"):
                    enrich(onto, classes=[cid])
            except Exception as e:  # noqa: BLE001
                st.error(f"Draft failed: {str(e)[:300]}")
            else:
                onto["classes"][cid].pop("new_columns", None)
                commit(f"Drafted the descriptions of {cid} with the LLM")
        if c3.button("Mark new columns reviewed", disabled=not onto["classes"][cid].get("new_columns"),
                     use_container_width=True):
            onto["classes"][cid].pop("new_columns", None)
            commit(f"Reviewed the new columns of {cid}")

    st.markdown("#### Relationships")
    ops = onto.get("object_properties", {})
    for m in md.multi_role(onto):
        with st.container(border=True):
            st.markdown(f"**{m['domain']} → {m['range']}**: {len(m['relationships'])} joins · "
                        + (f"default **{m['default']}**" if m["default"] else "no default yet"))
            label = {pid: f"{pid}: " + ", ".join(ops[pid]["join"]["from"])
                     + (f" ({ops[pid]['role']})" if ops[pid].get("role") else "") for pid in m["relationships"]}
            rels = m["relationships"]
            pick = st.radio("Default join", rels, index=rels.index(m["default"]) if m["default"] in rels else 0,
                            format_func=label.get, key=f"dj_{m['domain']}_{m['range']}")
            if st.button("Set as default", key=f"djb_{m['domain']}_{m['range']}"):
                md.set_default(onto, pick)
                commit(f"{pick} is the default join between {m['domain']} and {m['range']}")
    un = md.unlinked_ids(onto)
    st.markdown(f"**Identifier columns without a relationship** ({len(un)})")
    if un:
        st.dataframe(pd.DataFrame([{"column": f"{u['class']}.{u['column']}", "could point at": ", ".join(u["targets"][:4])}
                                   for u in un]), hide_index=True, width="stretch", height=min(300, 38 + 35 * len(un)))
        c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
        key = c1.selectbox("Column", [f"{u['class']}.{u['column']}" for u in un], key="ul_col",
                           label_visibility="collapsed")
        u = next(x for x in un if f"{x['class']}.{x['column']}" == key)
        tgt = c2.selectbox("Points at", u["targets"] or list(onto["classes"]), key=f"ul_tgt_{key}",
                           label_visibility="collapsed")
        if c3.button("Check values", key="ul_chk", disabled=run is None or not ed.class_key(onto, tgt),
                     use_container_width=True):
            ratio = md.containment(run, onto, u["class"], u["column"], tgt, dialect)
            st.caption("Could not check." if ratio is None else
                       f"{ratio:.0%} of sampled {key} values exist in {tgt}'s key"
                       + (" — likely a real join." if ratio >= 0.9 else " — probably not this table."))
        if c4.button("Add", key="ul_add", use_container_width=True):
            pid = ed.add_relationship(onto, u["class"], tgt, [u["column"]], ed.class_key(onto, tgt) or None)
            commit(f"Added relationship {pid}: {key} → {tgt}")
    sqls = [e.get("sql") for e in onto.get("examples") or []] + \
        [t.get("sql") for t in traces if t.get("status") == "ok" and t.get("sql")]
    obs = md.observed_joins(onto, [s for s in sqls if s], dialect)
    st.markdown(f"**Joins in queries that the model does not have** ({len(obs)})")
    if not obs:
        st.caption("None so far. They come from Ask answers, 🧪 Check SQL runs and verified examples.")
    else:
        st.dataframe(pd.DataFrame([{"join": o["join"], "uses": o["uses"]} for o in obs]), hide_index=True,
                     width="stretch")
        c1, c2 = st.columns([3, 1])
        j = c1.selectbox("Join", [o["join"] for o in obs], key="oj_pick", label_visibility="collapsed")
        if c2.button("Add as relationship", key="oj_add", use_container_width=True):
            pid = md.add_observed(onto, next(o for o in obs if o["join"] == j))
            commit(f"Added relationship {pid} from a join people run ({j})")

    st.markdown("#### Filters to save as rules")
    vf = md.value_filters(onto)
    if not vf:
        st.caption("No short value lists left to turn into named filters.")
    else:
        st.dataframe(pd.DataFrame([{"name": f["name"], "filter": f["filter"], "meaning": f["description"]} for f in vf]),
                     hide_index=True, width="stretch", height=min(300, 38 + 35 * len(vf)))
        c1, c2 = st.columns([3, 1])
        name = c1.selectbox("Filter", [f["name"] for f in vf], key="vf_pick", label_visibility="collapsed")
        if c2.button("Save as rule", key="vf_save", use_container_width=True,
                     help="Runs the filter read-only on the data first; nothing is saved if the database rejects it."):
            f = next(x for x in vf if x["name"] == name)
            err = md.test_expression(run, onto, f["class"], f["filter"], "filter", dialect) if run else None
            if err:
                st.error(f"Not saved, the database rejected it: {err}")
            else:
                rule = md.save_rule(onto, f)
                commit(f"Saved rule {rule}: {f['filter']}")
    st.caption("Calculations people use (from query history, Ask 👍 and verified examples) arrive as learned "
               "snippets in 📚 Knowledge and wait in the ✅ Review queue to become metrics.")


# --------------------------------------------------------------------------- #
# Search and the Term card
# --------------------------------------------------------------------------- #
def search_box(onto: dict) -> None:
    q = st.text_input("🔎 Search the model", key="sem_q",
                      placeholder="A business word, metric, column, glossary term or stored value")
    if not q.strip():
        return
    hits = an.search(q, an.docs(onto), limit=12)
    values = an._values(q, onto, None, None, [0])
    if not hits and not values:
        st.caption("Nothing matches. Search is by words, not meaning: try another word or a synonym, and add the "
                   "words people use as synonyms so the next search finds them.")
        return
    rows = [{"kind": h["kind"], "name": h["name"], "class": h.get("class") or "", "score": h["score"]} for h in hits]
    rows += [{"kind": "stored value", "name": f"{v['column']} = {v['value']}", "class": v.get("class") or "",
              "score": None} for v in values]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    opts = [f"{h['kind']} · {h['name']}" for h in hits]
    if not opts:
        return
    pick = st.selectbox("Open", opts, key="sem_pick")
    h = hits[opts.index(pick)]
    with st.container(border=True):
        if h["kind"] == "metric":
            m = next(x for x in onto.get("metrics", []) if x["name"] == h["name"])
            st.markdown(f"**{m['name']}** ({m.get('status', 'draft')}) on {m.get('base_class')}")
            st.code(m.get("sql") or "", language="sql")
            if m.get("description"):
                st.caption(m["description"])
        elif h["kind"] == "rule":
            r = next(x for x in onto.get("rules", []) if x.get("name") == h["name"])
            st.markdown(f"**{r['name']}** ({r.get('status', 'draft')}) · applies to {', '.join(r.get('applies_to') or [])}")
            st.caption(r.get("description") or "")
            if r.get("sql_hint"):
                st.code(r["sql_hint"], language="sql")
        elif h["kind"] == "page":
            p = next(x for x in onto.get("pages", []) if x.get("title") == h["name"])
            st.markdown(f"**{p['title']}** ({p.get('status', 'draft')})")
            st.markdown((p.get("body") or "")[:1500])
        elif h["kind"] == "glossary":
            st.markdown(f"**{h['name']}** means {h.get('definition')}")
        if h.get("class") in onto.get("classes", {}):
            term_card(onto, h["class"])


def term_card(onto: dict, cid: str) -> None:
    """One class in full: definition, status, columns with samples, unmapped columns, joins, metrics, pages."""
    c, ops = onto["classes"][cid], onto.get("object_properties", {})
    cert = (c.get("certification") or {}).get("status", "draft")
    st.markdown(f"#### {cid}")
    st.caption(f"`{c.get('source_fqn') or c.get('source_table')}` · {c.get('instance_count', '?')} rows · {cert}"
               + (f" · domains: {', '.join(c['domains'])}" if c.get("domains") else ""))
    if c.get("description"):
        st.write(c["description"])
    if c.get("synonyms"):
        st.caption("Also called: " + ", ".join(c["synonyms"]))
    props = c.get("data_properties") or {}
    st.dataframe(pd.DataFrame([{
        "property": pid, "column": p.get("source_column") or pid, "type": p.get("sql_type") or p.get("range"),
        "role": p.get("semantic_role") or "", "description": p.get("description") or "",
        "values": ", ".join(map(str, (p.get("allowed_values") or p.get("samples") or [])[:6]))}
        for pid, p in props.items()]), hide_index=True, width="stretch")
    mapped = {str(p.get("source_column") or pid).lower() for pid, p in props.items()}
    unmapped = [x for x in c.get("source_columns") or [] if x.lower() not in mapped]
    if unmapped:
        st.caption("Columns without a property (join keys and others): " + ", ".join(unmapped))
    joins = [f"{pid}: {p['domain']} → {p['range']} on " + ", ".join(f"{a} = {b}" for a, b in zip(p["join"]["from"], p["join"]["to"]))
             + (f" · role {p['role']}" if p.get("role") else "") + (" · default" if p.get("default") else "")
             for pid, p in ops.items() if p.get("join") and p.get("cardinality") != "one-to-many"
             and cid in (p.get("domain"), p.get("range"))]
    if joins:
        st.caption("Joins: " + " | ".join(joins))
    metrics = [f"{m['name']} = {m.get('sql')} ({m.get('status', 'draft')})" for m in onto.get("metrics", [])
               if m.get("base_class") == cid and m.get("status") != "deprecated"]
    if metrics:
        st.caption("Metrics: " + " | ".join(metrics))
    pages = [p["title"] for p in onto.get("pages", []) if f"class:{cid}" in (p.get("links") or [])]
    if pages:
        st.caption("Pages: " + ", ".join(pages))
    if c.get("review_note"):
        st.caption(f"Reviewer note: {c['review_note']}")


# --------------------------------------------------------------------------- #
# Change log
# --------------------------------------------------------------------------- #
def changes_view(onto: dict) -> None:
    log = onto.get("changes") or []
    with st.expander(f"📝 Change log ({len(log)})"):
        if not log:
            st.caption("No changes recorded yet. Every edit in the studio is logged here with who made it and when, "
                       "and the log is saved with each version.")
        else:
            st.dataframe(pd.DataFrame(list(reversed(log))[:300]), hide_index=True, width="stretch")
