"""
Streamlit views for the governed / learned side of the ontology:
  home_tab       Ontology home: domains & subdomains, the assets in each, certification, UC tags
  knowledge_tab  Pages (governed wiki), learned snippets with authority scores, experts
  review_tab     Review queue: promote, certify, settle conflicts, fix feedback hotspots
  monitor_tab    Request traces, feedback, token cost per user / domain, budgets, warehouse spend
  ask_tab        Ask, scoped by domain, traced, with feedback that feeds learned knowledge
"""
from __future__ import annotations

import io
import time
import zipfile

import pandas as pd
import streamlit as st

from . import governance as gv
from . import review as rv
from . import snippets as sn
from . import telemetry as tm

ss = st.session_state
STATUS_ICON = {"certified": "✅", "draft": "📝", "deprecated": "⛔"}
KIND_ICON = {"join": "🔗", "filter": "🔎", "expression": "🧮"}


def current_user() -> str:
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-Preferred-Username") or "local-user"
    except Exception:  # noqa: BLE001
        return "local-user"


def _commit(onto, msg):
    from .ontology_builder import refresh_index
    refresh_index(onto)
    ss.gov_msg = msg
    st.rerun()


def _flash():
    msg = ss.pop("gov_msg", None)
    if msg:
        st.toast(msg)


def _icon(status: str | None) -> str:
    return STATUS_ICON.get(status or "draft", "📝")


def _csv(v) -> str:
    return ", ".join(v or [])


def _split(text) -> list[str]:
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def traces() -> list[dict]:
    return tm.merge(ss.setdefault("traces", []), ss.get("stored_traces", []))


# =========================================================================== #
# Ontology home
# =========================================================================== #
def home_tab(onto: dict, runner: dict, uc_runner, on_databricks: bool):
    gv.ensure(onto)
    _flash()
    stats = gv.governance_stats(onto)
    m = st.columns(5)
    m[0].metric("Domains", stats["domains"])
    m[1].metric("Certified tables", stats["certified_classes"])
    m[2].metric("Certified metrics", f'{stats["certified_metrics"]} / {len(onto.get("metrics", []))}')
    m[3].metric("Pages", f'{stats["pages"]} ({stats["certified_pages"]} ✅)')
    m[4].metric("Learned snippets", stats["snippets"])
    st.caption("A **domain** is a tag for a logical group of assets (Finance, Sales, a region …). An asset can live "
               "in several domains. Scoping Ask to a domain sends only that domain's context to the model, which "
               "helps accuracy, latency and token cost.")

    if not onto["domains"]:
        st.info("No domains yet. Suggest them from table names and joins, or add your own below.")
    c1, c2 = st.columns([1, 3])
    if c1.button("✨ Suggest domains", help="Keyword match on class/table names, then neighbours for the rest. "
                                             "Only fills classes that have no domain."):
        gv.suggest_domains(onto)
        _commit(onto, "Domains suggested")
    if onto["domains"] and c2.button("Re-suggest for every class (overwrite)"):
        gv.suggest_domains(onto, overwrite=True)
        gv.inherit_metric_domains(onto, overwrite=True)
        _commit(onto, "Domains re-suggested")

    # ---- domain map -------------------------------------------------------------
    tops = [d for d in onto["domains"] if not d.get("parent")]
    if tops:
        st.markdown("#### Domain map")
        cols = st.columns(3)
        for i, d in enumerate(tops):
            a = gv.assets_in(onto, d["name"])
            subs = [x["name"] for x in onto["domains"] if x.get("parent") == d["name"]]
            cert_m = sum(1 for x in a["metrics"] if x.get("status") == "certified")
            with cols[i % 3].container(border=True):
                st.markdown(f"**{d['name']}**" + (f" · owner {d['owner']}" if d.get("owner") else ""))
                if subs:
                    st.caption("Subdomains: " + ", ".join(subs))
                st.caption(f"{len(a['classes'])} tables · {len(a['metrics'])} metrics ({cert_m} ✅) · "
                           f"{len(a['pages'])} pages · {len(a['snippets'])} snippets")
                if d.get("description"):
                    st.caption(d["description"])

    # ---- browse one domain ----------------------------------------------------------
    names = gv.domain_names(onto)
    if names:
        st.markdown("#### Browse a domain")
        dsel = st.selectbox("Domain", names, format_func=lambda n: gv.domain_path(onto, n), key="home_dom")
        a = gv.assets_in(onto, dsel)
        t1, t2, t3, t4 = st.tabs([f"Tables ({len(a['classes'])})", f"Metrics ({len(a['metrics'])})",
                                  f"Pages ({len(a['pages'])})", f"Snippets ({len(a['snippets'])})"])
        with t1:
            rows = [{"class": cid, "role": onto["classes"][cid].get("role"),
                     "table": onto["classes"][cid].get("source_fqn") or onto["classes"][cid].get("source_table"),
                     "status": (onto["classes"][cid].get("certification") or {}).get("status", "draft"),
                     "owner": (onto["classes"][cid].get("certification") or {}).get("owner"),
                     "domains": _csv(onto["classes"][cid].get("domains"))} for cid in a["classes"]]
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        with t2:
            if a["metrics"]:
                df = pd.DataFrame([{"metric": x["name"], "sql": x.get("sql"), "base": x.get("base_class"),
                                    "status": x.get("status", "draft"), "owner": x.get("owner") or "",
                                    "domains": _csv(x.get("domains")), "provenance": x.get("provenance")}
                                   for x in a["metrics"]])
                ed_df = st.data_editor(
                    df, hide_index=True, width="stretch", key=f"home_metrics_{dsel}",
                    disabled=["metric", "sql", "base", "provenance"],
                    column_config={"status": st.column_config.SelectboxColumn(options=gv.STATUSES)})
                if st.button("Apply metric governance", key=f"apply_m_{dsel}"):
                    for r in ed_df.itertuples(index=False):
                        gv.certify(onto, "metric", r.metric, r.status, owner=r.owner or None,
                                   by=current_user(), domains=_split(r.domains))
                    _commit(onto, "Metric governance updated")
                st.caption("One certified definition per KPI: Ask prefers ✅ certified metrics and never sees ⛔ "
                           "deprecated ones. Deploy them as Unity Catalog metric views from the Metric views tab.")
            else:
                st.caption("No metrics in this domain.")
        with t3:
            for p in a["pages"]:
                st.markdown(f"{_icon(p.get('status'))} **{p['title']}** — {p.get('owner') or 'no owner'}")
            if not a["pages"]:
                st.caption("No pages yet — create one in the Knowledge tab.")
        with t4:
            for s in a["snippets"][:15]:
                st.markdown(f"{KIND_ICON.get(s['kind'], '')} `{s['text'][:120]}` · authority **{s.get('authority', 0):.2f}**")

    # ---- manage -------------------------------------------------------------------------
    with st.expander("Manage domains and subdomains"):
        with st.form("dom_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            dn = c1.text_input("Domain name", placeholder="Finance")
            parent = c2.selectbox("Parent (makes it a subdomain)", ["—"] + names)
            owner = c3.text_input("Owner", placeholder="finance-data@company.com")
            desc = st.text_input("Description")
            if st.form_submit_button("Add / update", type="primary") and dn:
                try:
                    gv.upsert_domain(onto, dn, None if parent == "—" else parent, desc, owner)
                    _commit(onto, f"Saved domain {dn}")
                except ValueError as e:
                    st.error(str(e))
        if names:
            c1, c2, c3 = st.columns([2, 2, 1])
            old = c1.selectbox("Domain", names, key="dom_edit")
            new = c2.text_input("Rename to", key="dom_rename")
            if c3.button("Rename", disabled=not new):
                try:
                    gv.rename_domain(onto, old, new)
                    _commit(onto, f"Renamed {old} → {new}")
                except ValueError as e:
                    st.error(str(e))
            if st.button(f"Delete domain “{old}”", key="dom_del"):
                gv.delete_domain(onto, old)
                _commit(onto, f"Deleted {old}")

    with st.expander("Assign tables to domains, certify tables"):
        rows = [{"class": cid, "table": c.get("source_fqn") or c.get("source_table"),
                 "domains": _csv(c.get("domains")),
                 "status": (c.get("certification") or {}).get("status", "draft"),
                 "owner": (c.get("certification") or {}).get("owner") or ""}
                for cid, c in onto["classes"].items()]
        ed_df = st.data_editor(pd.DataFrame(rows), hide_index=True, width="stretch", key="assign_editor",
                               disabled=["class", "table"],
                               column_config={"domains": st.column_config.TextColumn(help="Comma separated"),
                                              "status": st.column_config.SelectboxColumn(options=gv.STATUSES)})
        if st.button("Apply", key="assign_apply", type="primary"):
            for r in ed_df.itertuples(index=False):
                gv.certify(onto, "class", r[0], r.status, owner=r.owner or None, by=current_user(),
                           domains=_split(r.domains))
            gv.inherit_metric_domains(onto)
            _commit(onto, "Tables updated")

    with st.expander("Unity Catalog tags"):
        stmts = gv.uc_tag_statements(onto)
        st.caption(f"Writes `{gv.TAG_DOMAIN}`, `{gv.TAG_STATUS}` and `{gv.TAG_OWNER}` tags on the source tables, so "
                   "domains and certification are visible in Catalog Explorer and to other tools. "
                   "Needs APPLY TAG on the tables.")
        st.code(";\n".join(stmts) or "-- no tables with a 3-part name and domains yet", language="sql")
        can = runner.get("kind") == "uc" and on_databricks
        c1, c2 = st.columns(2)
        if c1.button("🏷️ Apply tags in Unity Catalog", disabled=not (can and stmts)):
            run, errs = uc_runner(), []
            for s in stmts:
                try:
                    run(s)
                except Exception as e:  # noqa: BLE001
                    errs.append(str(e)[:200])
            (st.error if errs else st.success)(f"{len(stmts) - len(errs)} of {len(stmts)} tables tagged."
                                               + (" " + errs[0] if errs else ""))
        if c2.button("⬇️ Read tags from Unity Catalog", disabled=not can):
            fqns = [c["source_fqn"] for c in onto["classes"].values() if str(c.get("source_fqn", "")).count(".") == 2]
            n = gv.apply_uc_tags(onto, gv.read_uc_tags(uc_runner(), fqns))
            _commit(onto, f"Read tags for {n} table(s)")


# =========================================================================== #
# Knowledge: pages, learned snippets, experts
# =========================================================================== #
def knowledge_tab(onto: dict, runner: dict, uc_runner, on_databricks: bool):
    gv.ensure(onto)
    _flash()
    view = st.radio("View", ["📄 Pages", "🧠 Learned snippets", "🧑‍🏫 Experts"], horizontal=True,
                    label_visibility="collapsed", key="know_view")
    if view.endswith("Pages"):
        _pages(onto)
    elif view.endswith("snippets"):
        _snippets(onto, runner, uc_runner, on_databricks)
    else:
        _experts(onto)


def _pages(onto):
    st.caption("A **page** is the qualitative counterpart of a metric: a governed wiki page that explains a concept "
               "(e.g. net vs gross revenue). Certified pages go to Ask first; deprecated ones are never used.")
    pages = onto["pages"]
    opts = ["＋ New page"] + [p["id"] for p in pages]
    titles = {p["id"]: f"{_icon(p.get('status'))} {p['title']}" for p in pages}
    pick = st.selectbox("Page", opts, format_func=lambda x: titles.get(x, x),
                        index=opts.index(ss.get("open_page")) if ss.get("open_page") in opts else 0)
    cur = next((p for p in pages if p["id"] == pick), {})
    links_all = ([f"class:{c}" for c in onto["classes"]] + [f"metric:{m['name']}" for m in onto.get("metrics", [])]
                 + [f"rule:{r['name']}" for r in onto.get("rules", [])])
    left, right = st.columns([1.1, 1])
    with left, st.form(f"page_{pick}"):
        title = st.text_input("Title", cur.get("title", ""))
        c1, c2 = st.columns(2)
        doms = c1.multiselect("Domains", gv.domain_names(onto),
                              default=[d for d in cur.get("domains") or [] if d in gv.domain_names(onto)])
        owner = c2.text_input("Owner", cur.get("owner") or "")
        c3, c4 = st.columns(2)
        status = c3.selectbox("Status", gv.STATUSES, index=gv.STATUSES.index(cur.get("status", "draft")))
        syn = c4.text_input("Synonyms", _csv(cur.get("synonyms")))
        links = st.multiselect("Linked assets", links_all, default=[x for x in cur.get("links") or [] if x in links_all])
        body = st.text_area("Body (Markdown)", cur.get("body") or "", height=300)
        b1, b2 = st.columns(2)
        save = b1.form_submit_button("Save", type="primary")
        delete = b2.form_submit_button("Delete", disabled=not cur)
    if save:
        try:
            pid = gv.upsert_page(onto, title, body, doms, owner, status, links, page_id=cur.get("id"),
                                 synonyms=_split(syn), by=current_user())
            ss.open_page = pid
            _commit(onto, f"Saved page {title}")
        except ValueError as e:
            st.error(str(e))
    if delete:
        gv.delete_page(onto, cur["id"])
        ss.open_page = None
        _commit(onto, "Page deleted")
    with right:
        st.markdown("**Preview**")
        with st.container(border=True, height=520):
            if cur.get("status") == "certified":
                st.caption(f"✅ Certified by {cur.get('certified_by') or '?'} · {cur.get('certified_at', '')}")
            st.markdown(body if save else (cur.get("body") or "_Nothing yet._"))
    if pages:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, text in gv.pages_markdown(onto).items():
                z.writestr(name, text)
        st.download_button("⬇️ All pages (.zip of Markdown)", buf.getvalue(), file_name="pages.zip",
                           mime="application/zip")


def _snippets(onto, runner, uc_runner, on_databricks):
    st.caption("**Learned knowledge**: joins, filters and named calculations mined from the SQL people actually run. "
               "Authority blends popularity, reach, author authority, sharing (dashboards, Genie, jobs), comments "
               "and recency. High-authority snippets go to Ask and show up in the review queue.")
    with st.expander("Learn from query history or a query log", expanded=not onto.get("snippets")):
        srcs = (["Query history (Unity Catalog)"] if runner.get("kind") == "uc" and on_databricks else []) + \
            ["Query log file (.sql / .csv)", "Sample: Northwind query log"]
        src = st.radio("Source", srcs, horizontal=True, key="snip_src")
        recs, warn, ingest = None, [], "default"
        if src.startswith("Query history"):
            days = st.slider("Window (days)", 7, 365, 90, key="snip_days")
            if st.button("🧠 Learn", type="primary", key="learn_uc"):
                fqns = [c["source_fqn"] for c in onto["classes"].values() if c.get("source_fqn")]
                with st.spinner("Reading system.query.history…"):
                    res = sn.read_query_history(uc_runner(), fqns, days)
                recs, warn, ingest = res["records"], res["warnings"], "query_history"
        elif src.startswith("Query log"):
            f = st.file_uploader("Queries", type=["sql", "csv", "txt"], key="snip_file",
                                 help="CSV columns: statement_text, executed_by, source (dashboard / notebook / genie "
                                      "/ job / sql_editor), source_id, start_time. A .sql file is split on ';'.")
            if f is not None and st.button("🧠 Learn", type="primary", key="learn_file"):
                try:
                    recs = sn.records_from_file(f.name, f.getvalue())
                    ingest = "file:" + __import__("hashlib").sha1(f.getvalue()).hexdigest()[:12]
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
        else:
            st.caption("60 queries by five people across dashboards, notebooks, Genie spaces and a job, written for "
                       "the classic Northwind tables (Orders, \"Order Details\", Products …).")
            if st.button("🧠 Learn from the sample", type="primary", key="learn_sample"):
                import pathlib
                p = pathlib.Path(__file__).resolve().parents[1] / "samples" / "query_logs" / "northwind_query_log.csv"
                recs, ingest = sn.records_from_file(p.name, p.read_bytes()), "file:northwind-sample"
        for w in warn:
            st.warning(w)
        if recs is not None:
            dialect = runner.get("dialect") or "databricks"
            mined = sn.mine(recs, onto, dialect=dialect if dialect != "auto" else "databricks")
            if not mined and dialect != "databricks":
                mined = sn.mine(recs, onto, dialect="databricks")
            res = sn.merge_into(onto, mined, ingest=ingest)
            _commit(onto, f"{len(recs)} queries → {res['added']} new, {res['updated']} updated snippets")
        trusted = st.text_input("Trusted authors (comma separated, +0.3 author authority)",
                                _csv(list((onto.get("trusted_authors") or {}).keys())), key="trusted")
        if st.button("Save trusted authors and re-score", key="save_trusted"):
            onto["trusted_authors"] = {u: 0.3 for u in _split(trusted)}
            sn.rescore(onto)
            _commit(onto, "Re-scored")

    snips = onto.get("snippets", [])
    if not snips:
        return
    c1, c2, c3, c4 = st.columns(4)
    kinds = c1.multiselect("Kind", ["join", "filter", "expression"], default=["join", "filter", "expression"])
    dom = c2.selectbox("Domain", ["All"] + gv.domain_names(onto), key="snip_dom")
    min_a = c3.slider("Min authority", 0.0, 1.0, 0.0, 0.05)
    show_dismissed = c4.checkbox("Show dismissed", False)
    rows = [s for s in snips if s["kind"] in kinds and s.get("authority", 0) >= min_a
            and (dom == "All" or dom in (s.get("domains") or []))
            and (show_dismissed or s.get("status") != "dismissed")]
    df = pd.DataFrame([{"authority": s.get("authority"), "kind": s["kind"], "name": s.get("name") or "",
                        "snippet": s["text"], "uses": s["uses"], "people": len(s.get("users") or {}),
                        "sources": _csv(f"{sn.SOURCE_LABELS.get(k, k)} ×{n}" for k, n in (s.get("sources") or {}).items()),
                        "domains": _csv(s.get("domains")), "status": s.get("status"),
                        "comment": (s.get("comments") or [""])[0]} for s in rows])
    if len(df):
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"authority": st.column_config.ProgressColumn(min_value=0, max_value=1,
                                                                                 format="%.2f")})
    sel = st.selectbox("Inspect a snippet", [s["id"] for s in rows],
                       format_func=lambda i: next(f"{KIND_ICON[s['kind']]} {s.get('name') or s['text'][:80]}"
                                                  for s in rows if s["id"] == i), key="snip_pick")
    s = gv.find_asset(onto, "snippet", sel) if sel else None
    if s:
        a, b = st.columns([1.2, 1])
        with a:
            st.code(s["text"], language="sql", wrap_lines=True)
            st.caption(f"Status **{s.get('status')}**" + (f" → {s['promoted_to']}" if s.get("promoted_to") else "")
                       + f" · first seen {s.get('first_seen') or '?'} · last seen {s.get('last_seen') or '?'}")
            if s.get("comments"):
                st.markdown("Author notes: " + " · ".join(f"“{c}”" for c in s["comments"]))
            st.markdown("**People**: " + ", ".join(f"{u} ({n})" for u, n in (s.get("users") or {}).items()))
            if s.get("assets"):
                st.caption("Seen in: " + ", ".join(s["assets"][:8]))
        with b:
            parts = s.get("authority_parts") or {}
            st.markdown(f"**Authority {s.get('authority', 0):.2f}** = weighted blend")
            st.dataframe(pd.DataFrame([{"signal": k, "value": v, "weight": sn.WEIGHTS[k],
                                        "contributes": round(v * sn.WEIGHTS[k], 3)} for k, v in parts.items()]),
                         hide_index=True, width="stretch")
        _promote_buttons(onto, s, key="k")


def _promote_buttons(onto, s, key):
    if s.get("status") == "promoted":
        return
    cols = st.columns(4)
    if s["kind"] == "expression" and len(s.get("classes") or []) == 1:
        if cols[0].button("➕ Promote to metric", key=f"{key}_pm_{s['id']}"):
            ss.gov_msg = rv.promote(onto, s["id"], "metric", by=current_user())
            _commit(onto, ss.gov_msg)
    if s["kind"] == "filter":
        if cols[0].button("➕ Promote to rule", key=f"{key}_pr_{s['id']}"):
            _commit(onto, rv.promote(onto, s["id"], "rule", by=current_user()))
    if s["kind"] == "join":
        if cols[0].button("➕ Promote to relationship", key=f"{key}_pj_{s['id']}"):
            try:
                _commit(onto, rv.promote(onto, s["id"], "relationship", by=current_user()))
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    if cols[1].button("📄 Draft a page", key=f"{key}_dp_{s['id']}"):
        item = {"type": "popular-snippet", "snippet": s["id"], "domains": s.get("domains") or []}
        _draft(onto, item)
    if s.get("status") != "dismissed" and cols[2].button("Dismiss", key=f"{key}_ds_{s['id']}"):
        rv.dismiss(onto, f"snip:{s['id']}")
        _commit(onto, "Dismissed")


def _draft(onto, item, use_llm: bool | None = None):
    use_llm = ss.get("draft_llm", False) if use_llm is None else use_llm
    with st.spinner("Drafting page…"):
        title, body, links = rv.draft_page(onto, item, use_llm=use_llm)
    links = [x for x in links if x.split(":", 1)[0] in ("class", "metric", "rule")]
    pid = gv.upsert_page(onto, title, body, item.get("domains") or [], "", "draft", links, provenance="review")
    ss.open_page = pid
    _commit(onto, f"Draft page “{title}” created — review it in Knowledge → Pages")


def _experts(onto):
    st.caption("Who knows the most about a table, from the learned snippets (authority-weighted uses).")
    if not onto.get("snippets"):
        st.info("Learn snippets first.")
        return
    cid = st.selectbox("Table", ["All"] + list(onto["classes"]), key="exp_cls")
    st.dataframe(pd.DataFrame(sn.experts(onto, None if cid == "All" else cid, top=10)), hide_index=True,
                 width="stretch")


# =========================================================================== #
# Review queue
# =========================================================================== #
TYPE_LABEL = {"popular-snippet": "🔥 Popular snippet", "conflict": "⚖️ Conflicting definitions",
              "uncertified": "📝 Needs certification", "negative-feedback": "👎 Feedback hotspot",
              "knowledge-gap": "🕳️ Knowledge gap", "unassigned": "🗂️ No domain"}


def review_tab(onto: dict, on_databricks: bool):
    gv.ensure(onto)
    _flash()
    items = rv.build_queue(onto, traces())
    c1, c2, c3 = st.columns([2, 2, 1.4])
    types = c1.multiselect("Show", list(TYPE_LABEL), default=list(TYPE_LABEL), format_func=TYPE_LABEL.get)
    dom = c2.selectbox("Domain", ["All"] + gv.domain_names(onto), key="rv_dom")
    c3.checkbox("Draft pages with the LLM", key="draft_llm", disabled=not on_databricks,
                help="Otherwise drafts use a template filled with the evidence.")
    items = [i for i in items if i["type"] in types and (dom == "All" or dom in i.get("domains", []) or not i.get("domains"))]
    st.caption(f"{len(items)} item(s). The queue is rebuilt from learned snippets, metric/page certification, "
               "Ask traces and feedback each time you open it.")
    if not items:
        st.success("Nothing to review.")
    for it in items[:40]:
        with st.container(border=True):
            h1, h2 = st.columns([5, 1])
            h1.markdown(f"**{it['title']}**  \n{TYPE_LABEL[it['type']]}"
                        + (f" · {', '.join(it['domains'])}" if it.get("domains") else ""))
            h2.progress(float(it["priority"]), f"priority {it['priority']:.2f}")
            st.caption(it["why"])
            _review_actions(onto, it)


def _review_actions(onto, it):
    k = it["id"]
    if it["type"] == "popular-snippet":
        s = gv.find_asset(onto, "snippet", it["snippet"])
        _promote_buttons(onto, s, key=f"rv_{k}")
        return
    cols = st.columns([1.3, 1.3, 1.3, 1])
    if it["type"] == "conflict":
        if cols[0].button("📄 Draft a page that settles it", key=f"dp_{k}"):
            _draft(onto, it)
        metrics = [d["where"].split(" ", 1)[1] for d in it["definitions"] if d["where"].startswith("metric ")]
        for i, mname in enumerate(metrics[:2]):
            if cols[1 + i].button(f"✅ Certify {mname}", key=f"cm_{k}_{mname}"):
                gv.certify(onto, "metric", mname, "certified", by=current_user())
                for other in metrics:
                    if other != mname:
                        gv.certify(onto, "metric", other, "deprecated")
                _commit(onto, f"Certified {mname}; deprecated the other definition(s)")
    elif it["type"] == "uncertified":
        a = gv.find_asset(onto, it["kind"], it["key"]) or {}
        owner = cols[0].text_input("Owner", a.get("owner") or current_user(), key=f"ow_{k}",
                                   label_visibility="collapsed")
        if cols[1].button("✅ Certify", key=f"ct_{k}", type="primary"):
            gv.certify(onto, it["kind"], it["key"], "certified", owner=owner, by=current_user())
            _commit(onto, f"Certified {it['kind']} {it['key']}")
        if cols[2].button("⛔ Deprecate", key=f"dep_{k}"):
            gv.certify(onto, it["kind"], it["key"], "deprecated", owner=owner)
            _commit(onto, f"Deprecated {it['kind']} {it['key']}")
        if it["kind"] == "metric":
            st.code(a.get("sql") or "", language="sql")
        elif it["kind"] == "page":
            with st.expander("Read the page"):
                st.markdown(a.get("body") or "")
    elif it["type"] in ("negative-feedback", "knowledge-gap"):
        if cols[0].button("📄 Draft a page", key=f"dp_{k}"):
            _draft(onto, it)
    elif it["type"] == "unassigned":
        if cols[0].button("✨ Suggest domains", key=f"sd_{k}"):
            gv.suggest_domains(onto)
            _commit(onto, "Domains suggested")
    if cols[-1].button("Dismiss", key=f"dis_{k}"):
        rv.dismiss(onto, k)
        _commit(onto, "Dismissed")


# =========================================================================== #
# Monitor: traces, cost, budgets
# =========================================================================== #
def monitor_tab(uc_runner, on_databricks: bool):
    ss.setdefault("price_in", tm.DEFAULT_PRICE_IN)
    ss.setdefault("price_out", tm.DEFAULT_PRICE_OUT)
    ss.setdefault("token_budget", tm.DEFAULT_DAILY_TOKEN_BUDGET)
    c1, c2, c3, c4 = st.columns(4)
    ss.price_in = c1.number_input("USD per 1M input tokens", 0.0, 100.0, float(ss.price_in), 0.05)
    ss.price_out = c2.number_input("USD per 1M output tokens", 0.0, 100.0, float(ss.price_out), 0.05)
    ss.token_budget = c3.number_input("Daily token budget per user (0 = none)", 0, 10_000_000,
                                      int(ss.token_budget), 1000,
                                      help="Ask refuses new questions once a user has spent this many tokens today.")
    if tm.table_enabled():
        if c4.button("Load stored traces", disabled=not on_databricks,
                     help=f"Reads the last 30 days from {tm.TELEMETRY_TABLE}"):
            try:
                ss.stored_traces = tm.load_traces(uc_runner())
            except Exception as e:  # noqa: BLE001
                st.error(str(e)[:300])
    else:
        c4.caption("Set TELEMETRY_TABLE (catalog.schema.table) in app.yaml to keep traces in a Delta table.")
    ts = traces()
    for t in ts:
        t["cost_usd"] = tm.price(t, ss.price_in, ss.price_out)
    if not ts:
        st.info("No requests yet. Ask a question in the Ask tab; every request is traced here.")
    else:
        df = pd.DataFrame(ts)
        df["tokens"] = df["prompt_tokens"].fillna(0) + df["completion_tokens"].fillna(0)
        f1, f2, f3, f4 = st.columns(4)
        users = f1.multiselect("User", sorted(df["user"].dropna().unique()))
        doms = f2.multiselect("Domain", sorted(df["domain"].dropna().unique()))
        stat = f3.multiselect("Status", sorted(df["status"].dropna().unique()))
        fb = f4.multiselect("Feedback", ["up", "down", "none"])
        if users:
            df = df[df["user"].isin(users)]
        if doms:
            df = df[df["domain"].isin(doms)]
        if stat:
            df = df[df["status"].isin(stat)]
        if fb:
            df = df[df["feedback"].fillna("none").isin(fb)]
        m = st.columns(5)
        m[0].metric("Requests", len(df))
        m[1].metric("Errors", int((df["status"] == "error").sum()))
        m[2].metric("👍 / 👎", f'{int((df["feedback"] == "up").sum())} / {int((df["feedback"] == "down").sum())}')
        m[3].metric("Tokens", f'{int(df["tokens"].sum()):,}')
        m[4].metric("Model cost (est.)", f'${df["cost_usd"].sum():.4f}')
        st.dataframe(df[["ts", "user", "domain", "mode", "question", "status", "rows", "tokens", "llm_ms", "sql_ms",
                         "cost_usd", "feedback"]], hide_index=True, width="stretch")
        g1, g2 = st.columns(2)
        with g1:
            st.markdown("**Cost by user**")
            st.bar_chart(df.groupby("user")["cost_usd"].sum(), horizontal=True)
        with g2:
            st.markdown("**Tokens by domain**")
            st.bar_chart(df.groupby("domain")["tokens"].sum(), horizontal=True)
        pick = st.selectbox("Open a request", df["id"].tolist(),
                            format_func=lambda i: next(f"{t['ts']} · {t['user']} · {t['question'][:70]}"
                                                       for t in ts if t["id"] == i))
        t = next(x for x in ts if x["id"] == pick)
        with st.container(border=True):
            st.markdown(f"**{t['question']}**  \n{t['user']} · {t['ts']} · scope **{t['domain']}** · mode {t['mode']}")
            steps = pd.DataFrame([
                {"step": "1 · scope context", "detail": f"domain {t['domain']}", "ms": None},
                {"step": "2 · assets in prompt",
                 "detail": "; ".join(f"{k}: {len(v) if isinstance(v, list) else v}" for k, v in (t.get("assets") or {}).items()),
                 "ms": None},
                {"step": "3 · model writes SQL", "detail": f"{t['prompt_tokens']} in / {t['completion_tokens']} out tokens",
                 "ms": t.get("llm_ms")},
                {"step": "4 · run on warehouse", "detail": f"{t.get('rows') if t.get('rows') is not None else '–'} rows",
                 "ms": t.get("sql_ms")},
                {"step": "5 · result", "detail": t["status"] + (f": {t['error']}" if t.get("error") else ""), "ms": None},
            ])
            st.dataframe(steps, hide_index=True, width="stretch")
            for k, v in (t.get("assets") or {}).items():
                if isinstance(v, list) and v:
                    st.caption(f"{k}: " + ", ".join(map(str, v[:30])))
            if t.get("sql"):
                st.code(t["sql"], language="sql", wrap_lines=True)
            if t.get("feedback"):
                st.caption(f"Feedback: {'👍' if t['feedback'] == 'up' else '👎'} {t.get('feedback_note') or ''}")

    st.divider()
    st.markdown("**SQL warehouse spend** (system.billing.usage, split per user by query time)")
    wid = __import__("os").getenv("SQL_WAREHOUSE_ID", "")
    days = st.slider("Days", 7, 90, 30, key="wh_days")
    if st.button("Load warehouse cost", disabled=not (on_databricks and wid)):
        try:
            with st.spinner("Reading system tables…"):
                daily, per_user = tm.warehouse_cost(uc_runner(), wid, days)
            ss.wh_cost = (daily, per_user)
        except Exception as e:  # noqa: BLE001
            st.error(f"Needs SELECT on system.billing.usage, system.billing.list_prices and system.query.history: "
                     f"{str(e)[:250]}")
    if ss.get("wh_cost"):
        daily, per_user = ss.wh_cost
        a, b = st.columns(2)
        with a:
            st.metric("Warehouse list-price cost", f'${float(daily["usd"].fillna(0).sum()):,.2f}',
                      f'{float(daily["dbus"].fillna(0).sum()):,.1f} DBUs', delta_color="off")
            if len(daily):
                st.line_chart(daily.set_index("usage_date")["usd"])
        with b:
            st.dataframe(per_user, hide_index=True, width="stretch")


# =========================================================================== #
# Ask (scoped, traced, with feedback)
# =========================================================================== #
def ask_tab(onto: dict, runner: dict, uc_runner, on_databricks: bool):
    from .ask import answer, answer_prompt, run_on_sqlite
    gv.ensure(onto)
    user = current_user()
    st.markdown("Ask a business question; the Foundation Model writes SQL **from the ontology** "
                "(optionally side by side with a schema-only baseline). Every request is traced in **Monitor**.")
    if not on_databricks:
        st.info("Needs the Foundation Model endpoint — available when running on Databricks.")
    c1, c2, c3 = st.columns([2, 1.2, 1.2])
    doms = gv.domain_names(onto)
    scope_dom = c1.selectbox("Domain scope", ["All domains"] + doms, key="ask_scope",
                             format_func=lambda n: n if n == "All domains" else gv.domain_path(onto, n),
                             help="Only this domain's tables (plus their direct joins), metrics, rules, pages and "
                                  "snippets go to the model.")
    use_snips = c2.checkbox("Use learned snippets", value=True, key="ask_snips")
    compare = c3.checkbox("Compare with schema-only", value=True, key="ask_compare")
    scoped = gv.scope(onto, None if scope_dom == "All domains" else scope_dom)
    snips = sn.top_for_context(scoped) if use_snips else []
    full_tokens = tm.estimate_tokens(answer_prompt("", onto, "ontology", runner.get("dialect") or "databricks",
                                                   snippets=sn.top_for_context(onto) if use_snips else []))
    ctx_tokens = tm.estimate_tokens(answer_prompt("", scoped, "ontology", runner.get("dialect") or "databricks",
                                                  snippets=snips))
    st.caption(f"Context ≈ {ctx_tokens:,} tokens · {len(scoped['classes'])} tables, "
               f"{len(scoped.get('metrics', []))} metrics, {len(scoped.get('pages', []))} pages, {len(snips)} snippets"
               + (f" · {100 - 100 * ctx_tokens // max(1, full_tokens)}% smaller than the whole ontology"
                  if scope_dom != "All domains" else ""))
    q = st.text_input("Question", placeholder="e.g. Net revenue by category last year", key="ask_q")
    budget = int(ss.get("token_budget", tm.DEFAULT_DAILY_TOKEN_BUDGET) or 0)
    used = tm.tokens_today(traces(), user)
    if budget and on_databricks and tm.table_enabled():     # include earlier sessions' traces
        try:
            used = max(used, tm.tokens_today_stored(uc_runner(), user))
        except Exception:  # noqa: BLE001
            pass
    blocked = bool(budget) and used >= budget
    if blocked:
        st.error(f"You have used your daily budget of {budget:,} tokens. An admin can raise it in Monitor.")
    if st.button("Ask", type="primary", disabled=not (q and on_databricks) or blocked):
        from .llm_enrich import chat
        results = []
        for mode in (["ontology", "baseline"] if compare else ["ontology"]):
            t = tm.new_trace(q, user, mode, None if scope_dom == "All domains" else scope_dom)
            ctx_onto = dict(scoped, _snippets_in_context=snips if mode == "ontology" else [])
            t["assets"] = tm.assets_used(ctx_onto) if mode == "ontology" else {
                "classes": sorted(scoped["classes"])}
            dialect = runner.get("dialect") or "databricks"
            if dialect == "auto":
                dialect = "databricks"
            execute = None
            if runner.get("kind") == "sqlite":
                db = open(runner["path"], "rb").read()
                execute = lambda sql, db=db: run_on_sqlite(sql, db_bytes=db)  # noqa: E731
            elif runner.get("kind") == "uc":
                execute = uc_runner()
            with st.spinner("Thinking, writing SQL" + (" and checking it on the data…" if execute else "…")):
                try:
                    a = answer(q, scoped, mode, dialect, execute=execute,
                               snippets=snips if mode == "ontology" else None, chat_fn=chat)
                except Exception as e:  # noqa: BLE001
                    a = {"status": "error", "error": f"SQL generation failed: {e}", "sql": None, "result": None,
                         "attempts": [], "prompt_tokens": 0, "completion_tokens": 0, "llm_ms": 0, "sql_ms": 0}
            res = a.get("result")
            for k in ("sql", "status", "error", "prompt_tokens", "completion_tokens", "llm_ms"):
                t[k] = a.get(k)
            t["sql_ms"] = a.get("sql_ms") or None
            t["rows"] = None if res is None else len(res)
            t["reasoning"], t["assumptions"] = a.get("reasoning"), a.get("assumptions") or []
            t["attempts"] = [{k: v for k, v in x.items()} for x in a.get("attempts", [])]
            if t.get("sql"):
                t["assets"]["sql_classes"] = tm.sql_classes(t["sql"], onto, dialect)
            if execute is None and t["status"] == "ok":
                t["rows"] = None
            t["cost_usd"] = tm.price(t, ss.get("price_in", tm.DEFAULT_PRICE_IN),
                                     ss.get("price_out", tm.DEFAULT_PRICE_OUT))
            ss.setdefault("traces", []).insert(0, t)
            if on_databricks and tm.table_enabled():
                err = tm.save_trace(uc_runner(), t)
                if err:
                    st.caption(err)
            results.append((t["id"], res))
        ss.ask_results = results
    for col, (tid, res) in zip(st.columns(max(1, len(ss.get("ask_results", [])))), ss.get("ask_results", [])):
        t = next((x for x in ss.get("traces", []) if x["id"] == tid), None)
        if t is None:
            continue
        with col:
            st.markdown("**With ontology**" if t["mode"] == "ontology" else "**Baseline (schema only)**")
            if t.get("sql"):
                st.code(t["sql"], language="sql", wrap_lines=True)
            fixes = sum(1 for x in t.get("attempts") or [] if x.get("error"))
            if t.get("reasoning") or t.get("assumptions") or fixes:
                with st.expander("How it answered" + (f" · self-corrected {fixes}×" if fixes else "")):
                    if t.get("reasoning"):
                        st.markdown(t["reasoning"].replace("*", "\\*").replace("_", "\\_"))
                    for x in t.get("assumptions") or []:
                        st.caption("Assumption: " + x)
                    for i, x in enumerate(t.get("attempts") or [], 1):
                        if x.get("error"):
                            st.caption(f"Attempt {i}: the query {x['error'][:300]}")
            if t["status"] == "error":
                st.error(t["error"])
            elif t["status"] == "refused":
                st.warning(t["error"])
            elif res is not None:
                st.dataframe(res, hide_index=True, width="stretch")
            else:
                st.caption("No live data for this source — SQL shown only.")
            st.caption(f"{t['prompt_tokens']:,} + {t['completion_tokens']:,} tokens · model {t.get('llm_ms')} ms · "
                       f"SQL {t.get('sql_ms') or '–'} ms · ${t['cost_usd']:.5f}")
            if t.get("feedback"):
                st.caption("Thanks — feedback saved." + (" Learned as a snippet." if t["feedback"] == "up" else ""))
            else:
                f1, f2 = st.columns([1, 3])
                note = f2.text_input("What was wrong?", key=f"fbn_{tid}", label_visibility="collapsed",
                                     placeholder="What was wrong? (optional)")
                b1, b2 = f1.columns(2)
                if b1.button("👍", key=f"up_{tid}"):
                    _feedback(onto, t, "up", None, uc_runner, on_databricks)
                if b2.button("👎", key=f"dn_{tid}"):
                    _feedback(onto, t, "down", note, uc_runner, on_databricks)


def _feedback(onto, t, value, note, uc_runner, on_databricks):
    t["feedback"], t["feedback_note"] = value, note or None
    if value == "up" and t.get("sql") and t["mode"] == "ontology":
        dialect = (ss.get("runner") or {}).get("dialect") or "databricks"
        mined = sn.mine([sn.ask_record(t["question"], t["sql"], t["user"])], onto, dialect=dialect)
        sn.merge_into(onto, mined, ingest=f"ask:{t['id']}")
    if on_databricks and tm.table_enabled():
        tm.save_trace(uc_runner(), t)
    _commit(onto, "Feedback saved")
