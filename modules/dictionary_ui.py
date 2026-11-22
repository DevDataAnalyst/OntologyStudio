"""Describe → 📖 Dictionary: every table, column, metric and page in one searchable place.

Search every table, column, measure and page; open a table to see its grain, size, freshness, owner,
certification, columns with their profile, relationships, how people actually query it, and a live
referential-integrity check. The Power BI starter kit (DAX measures, Power Query M, CSVs) is a download in
Publish → Exports (modules/powerbi.py)."""
from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from . import governance as gv
from . import powerbi as pb
from . import snippets as sn

ICON = {"certified": "✅", "deprecated": "⛔", "draft": "📝"}


def _runner_sql(runner: dict, uc_runner, on_databricks: bool):
    if runner.get("kind") == "sqlite" and runner.get("path"):
        def run(sql):
            conn = sqlite3.connect(f"file:{runner['path']}?mode=ro", uri=True)
            try:
                return pd.read_sql_query(sql, conn)
            finally:
                conn.close()
        return run, "sqlite"
    if runner.get("kind") == "uc" and on_databricks:
        return uc_runner(), "databricks"
    return None, None


def _num(v):
    return "–" if v is None else f"{v:,}"


def dictionary_tab(onto: dict, runner: dict, uc_runner, on_databricks: bool):
    ss = st.session_state
    st.caption("Everything about each table in business terms: grain, size, freshness, owner, certification, "
               "columns, relationships, data-quality notes and how people already query it. Edit descriptions and "
               "synonyms in the Model step's inspector; the Power BI pack (DAX, Power Query) is in "
               "**Publish → Exports**.")
    c1, c2 = st.columns([1, 2.2])
    doms = ["All domains"] + gv.domain_names(onto)
    dom = c1.selectbox("Domain", doms, key="dd_domain")
    classes = list(onto["classes"]) if dom == "All domains" else gv.assets_in(onto, dom)["classes"]
    q = c2.text_input("Search tables, columns, measures and pages", key="dd_q",
                      placeholder="e.g. country, revenue, ship date")
    if not classes:
        st.info("No tables in this domain yet.")
        return

    if q:
        hits = [h for h in pb.search(onto, q) if not h["_class"] or h["_class"] in classes or h["Type"] == "Page"]
        if hits:
            st.dataframe(pd.DataFrame(hits).drop(columns=["_class", "_s"]), hide_index=True, width="stretch",
                         height=min(38 + 35 * len(hits), 320))
            hit_tables = list(dict.fromkeys(h["_class"] for h in hits if h["_class"] in classes))
            if hit_tables and ss.get("dd_table") not in hit_tables:
                ss["dd_table"] = hit_tables[0]
        else:
            st.info("Nothing matches. Try a business word or part of a column name.")

    # ---------------- model overview ----------------
    cards = {c: pb.table_card(onto, c) for c in classes}
    with st.expander(f"Model overview: {len(classes)} tables", expanded=not q):
        st.dataframe(pd.DataFrame([{
            "Table": f"{ICON.get(t['status'], '')} {t['table']}", "Role": t["role"], "Rows": t["rows"],
            "Columns": t["columns"], "Grain": t["grain"],
            "Last changed": t["last_altered"], "Owner": t["owner"], "Domains": ", ".join(t["domains"]),
            "Source": t["source"]} for t in sorted(cards.values(), key=lambda t: (t["role"] != "fact", t["table"]))]),
            hide_index=True, width="stretch",
            column_config={"Rows": st.column_config.NumberColumn(format="localized")})
        lo, hi = pb.date_range(onto)
        st.caption("Star schema: facts hold the events and measures, dimensions describe them. "
                   + (f"Profiled dates run from {lo} to {hi}." if lo else ""))

    sel = ss.get("selection") or {}
    order = sorted(classes, key=lambda c: (cards[c]["role"] != "fact", cards[c]["table"]))
    if ss.get("dd_table") not in classes:
        ss["dd_table"] = sel.get("id") if sel.get("kind") == "node" and sel.get("id") in classes else order[0]
    cid = st.selectbox("Table", order, key="dd_table",
                       format_func=lambda c: f"{ICON.get(cards[c]['status'], '')} {cards[c]['table']}  "
                                             f"({cards[c]['role']}, {cards[c]['source']})")
    _table_detail(onto, cid, cards[cid], runner, uc_runner, on_databricks)


def _dialect(runner: dict) -> str:
    d = runner.get("dialect") or "databricks"
    return "databricks" if d == "auto" else d


def _table_detail(onto, cid, card, runner, uc_runner, on_databricks):
    st.subheader(f"{ICON.get(card['status'], '')} {card['table']}")
    if card["description"]:
        st.write(card["description"])
    m = st.columns(5)
    m[0].metric("Rows", _num(card["rows"]))
    m[1].metric("Columns", card["columns"])
    m[2].metric("Size", pb.human_size(card["size_bytes"]) if card["size_bytes"] else "–")
    m[3].metric("Last changed", (card["last_altered"] or "–")[:10])
    m[4].metric("Owner", card["owner"] or "–")
    facts = [("Source", f"`{card['source']}`" + (f" ({card['table_type']})" if card["table_type"] else "")),
             ("Grain", card["grain"]), ("Role", card["role"]),
             ("Domains", ", ".join(card["domains"]) or "not assigned"),
             ("Certification", card["status"] + (f" by {card['certified_by']}" if card.get("certified_by") else ""))]
    if card["upstream"] or card["downstream"]:
        facts.append(("Lineage", f"fed by {', '.join(card['upstream']) or 'nothing recorded'}; "
                                 f"feeds {', '.join(card['downstream']) or 'nothing recorded'}"))
    st.markdown("\n".join(f"- **{k}:** {v}" for k, v in facts))
    for n in card["notes"]:
        st.warning(n)
    if card["status"] == "deprecated":
        st.error("This table is deprecated: don't build new reports on it.")

    t_cols, t_rel, t_use = st.tabs(["Columns", "Relationships", "Usage and docs"])
    with t_cols:
        rows = pb.column_rows(onto, cid)
        cols = ["Column", "Description", "Kind", "Source column", "SQL type", "Null %", "Distinct", "Min", "Max",
                "Sample values", "References", "Q&A synonyms", "Notes"]
        df = pd.DataFrame(rows)
        st.dataframe(df[[c for c in cols if c in df.columns]], hide_index=True, width="stretch",
                     column_config={"Null %": st.column_config.NumberColumn(format="%.1f %%"),
                                    "Distinct": st.column_config.NumberColumn(format="localized")})
        flagged = [r for r in rows if r["Notes"]]
        if flagged:
            st.caption(f"{len(flagged)} column(s) carry modeling or data-quality notes.")
        if not any(r["Distinct"] is not None for r in rows):
            st.caption("Rebuild with profiling switched on to see null %, distinct counts and min/max.")

    with t_rel:
        rels = [r for r in pb.relationships(onto) if card["table"] in (r["From table"], r["To table"])]
        if rels:
            st.dataframe(pd.DataFrame(rels)[["From table", "From column", "To table", "To column", "Cardinality",
                                             "Cross filter", "Active", "Notes"]], hide_index=True, width="stretch")
        else:
            st.info("No relationships touch this table.")
        run, dialect = _runner_sql(runner, uc_runner, on_databricks)
        ops = [pid for pid, p in onto.get("object_properties", {}).items()
               if p.get("domain") == cid and pb.integrity_sql(onto, pid)]
        if ops:
            st.markdown("**Referential integrity**: rows whose key is empty or has no match land in a "
                        "(Blank) member in every visual.")
            if st.button("Check now", key=f"dd_ri_{cid}", disabled=run is None,
                         help=None if run else "Connect a database to run the check."):
                out = []
                for pid in ops:
                    p = onto["object_properties"][pid]
                    try:
                        r = run(pb.integrity_sql(onto, pid, dialect)).iloc[0]
                        tot, nul, orph = (int(r["rows_total"] or 0), int(r["rows_null_key"] or 0),
                                          int(r["rows_orphan"] or 0))
                        out.append({"Relationship": f"{card['table']} → {pb.table_name(onto, p['range'])}",
                                    "Rows": tot, "Empty key": nul, "No match": orph,
                                    "Result": "✅ clean" if not (nul or orph) else
                                    f"⚠️ {100 * (nul + orph) / max(tot, 1):.1f}% go to (Blank)"})
                    except Exception as e:  # noqa: BLE001
                        out.append({"Relationship": pid, "Result": f"failed: {e}"})
                ss = st.session_state
                ss[f"dd_ri_res_{cid}"] = out
            res = st.session_state.get(f"dd_ri_res_{cid}")
            if res:
                st.dataframe(pd.DataFrame(res), hide_index=True, width="stretch")

    with t_use:
        snips = sorted([s for s in onto.get("snippets", []) if cid in (s.get("classes") or [])
                        and s.get("status") != "dismissed"], key=lambda s: -s.get("authority", 0))
        if snips:
            st.markdown("**How people query it** (learned from query history, by authority)")
            st.dataframe(pd.DataFrame([{"Kind": s["kind"], "Name": s.get("name") or "", "SQL": s["text"],
                                        "Authority": s.get("authority"), "Uses": s.get("uses"),
                                        "Users": len(s.get("users") or {})} for s in snips[:15]]),
                         hide_index=True, width="stretch",
                         column_config={"Authority": st.column_config.ProgressColumn(min_value=0, max_value=1)})
            ex = sn.experts(onto, cid, top=5)
            if ex:
                st.markdown("**Who to ask:** " + ", ".join(f"{e['person']} ({e['snippet_uses']} uses)" for e in ex))
        else:
            st.caption("No learned usage yet: learn from query history in **Describe → Knowledge**.")
        words = {cid.lower(), card["table"].lower(), str(onto["classes"][cid].get("label", "")).lower()}
        pages = [p for p in onto.get("pages", []) if p.get("status") != "deprecated" and
                 (cid in (p.get("classes") or []) or any(w and w in f"{p.get('title', '')} {p.get('body', '')}".lower()
                                                         for w in words))]
        for p in pages:
            with st.expander(f"{ICON.get(p.get('status'), '')} {p['title']}"):
                st.markdown(p.get("body") or "")
        vm = [v for v in onto.get("value_meanings", []) if v.get("class") == cid and v.get("values")]
        if vm:
            st.markdown("**Code values**")
            for v in vm:
                st.markdown(f"- `{v.get('column')}`: " + ", ".join(
                    f"`{k}`" + (f" = {d}" if d else "") for k, d in v["values"].items()))
        rules = [r for r in onto.get("rules", []) if cid in (r.get("applies_to") or [])
                 and r.get("status") != "deprecated"]
        if rules:
            st.markdown("**Business rules**")
            for r in rules:
                st.markdown(f"- **{r.get('name')}**: {r.get('description') or ''} `{r.get('sql_hint') or ''}`")
