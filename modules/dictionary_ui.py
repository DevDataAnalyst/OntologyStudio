"""📘 Data dictionary tab: the ontology seen by a Power BI developer.

Search every table, column, measure and page; open a table to see its grain, size, freshness, owner,
certification, storage-mode advice, column-by-column modeling settings, relationships, DAX measures,
Power Query M, how people actually query it, and a live referential-integrity check. Everything can be
downloaded as a starter kit (dictionary, CSVs, DAX and M)."""
from __future__ import annotations

import os
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
    st.caption("Everything a Power BI developer needs about each data object: grain, size, freshness, owner, "
               "certification, Power BI data types and column settings, relationships, DAX measures, Power Query, "
               "data-quality notes and how people already query it. Names follow Power BI conventions "
               "(Customer ID, Unit Price) and the DAX and M below use the same names.")
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
            "Columns": t["columns"], "Grain": t["grain"], "Storage mode": t["storage_mode"],
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

    # ---------------- downloads ----------------
    st.divider()
    st.markdown("**Take it to Power BI**")
    host = os.getenv("DATABRICKS_HOST", "").replace("https://", "").rstrip("/") or "<workspace-host>"
    wid = os.getenv("SQL_WAREHOUSE_ID", "")
    path = f"/sql/1.0/warehouses/{wid}" if wid and not wid.startswith("REPLACE") else "/sql/1.0/warehouses/<id>"
    d1, d2, d3 = st.columns(3)
    name = ((onto.get("ontology") or {}).get("name") or "model").replace(" ", "_").lower()
    d1.download_button("⬇️ Power BI starter kit (.zip)", pb.starter_kit(onto, classes, host, path,
                                                                       _dialect(runner)),
                       file_name=f"{name}_powerbi_kit.zip", mime="application/zip", width="stretch",
                       help="Data dictionary, columns/relationships/measures CSVs, measures.dax for DAX query view, "
                            "a Date table and one Power Query script per table.")
    d2.download_button("⬇️ Data dictionary (.md)", pb.dictionary_markdown(onto, classes, _dialect(runner)),
                       file_name=f"{name}_data_dictionary.md", mime="text/markdown", width="stretch")
    d3.download_button("⬇️ All columns (.csv)", pd.DataFrame([dict({"Table": cards[c]["table"]}, **r)
                                                               for c in classes for r in pb.column_rows(onto, c)])
                       .to_csv(index=False), file_name=f"{name}_columns.csv", mime="text/csv", width="stretch")
    with st.expander("Date table (DAX)"):
        st.code(pb.date_table_dax(onto), language="sql")


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
             ("Certification", card["status"] + (f" by {card['certified_by']}" if card.get("certified_by") else "")),
             ("Storage mode", f"**{card['storage_mode']}**. {card['storage_why']}")]
    if card["upstream"] or card["downstream"]:
        facts.append(("Lineage", f"fed by {', '.join(card['upstream']) or 'nothing recorded'}; "
                                 f"feeds {', '.join(card['downstream']) or 'nothing recorded'}"))
    st.markdown("\n".join(f"- **{k}:** {v}" for k, v in facts))
    for n in card["notes"]:
        st.warning(n)
    if card["status"] == "deprecated":
        st.error("This table is deprecated: don't build new reports on it.")

    t_cols, t_rel, t_meas, t_m, t_use = st.tabs(["Columns", "Relationships", "Measures (DAX)",
                                                 "Power Query (M)", "Usage and docs"])
    with t_cols:
        rows = pb.column_rows(onto, cid)
        show = st.radio("Show", ["Modeling", "Profile", "Everything"], horizontal=True, key=f"dd_view_{cid}",
                        label_visibility="collapsed")
        cols = {"Modeling": ["Column", "Power BI type", "Kind", "Summarize by", "Hide", "Format", "Data category",
                             "References", "Q&A synonyms", "Notes"],
                "Profile": ["Column", "Source column", "SQL type", "Nullable", "Null %", "Distinct", "Min", "Max",
                            "Sample values", "Description"]}.get(show)
        df = pd.DataFrame(rows)
        st.dataframe(df[cols] if cols else df, hide_index=True, width="stretch",
                     column_config={"Hide": st.column_config.CheckboxColumn(help="Hide in report view"),
                                    "Null %": st.column_config.NumberColumn(format="%.1f %%"),
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

    with t_meas:
        ti = st.toggle("Add time intelligence (YTD, prior year, YoY %)", key=f"dd_ti_{cid}")
        ms = [r for r in pb.measures(onto, _dialect(runner), time_intelligence=ti) if r["Home table"] == card["table"]]
        if ms:
            st.dataframe(pd.DataFrame(ms)[["Measure", "DAX", "Format", "Display folder", "Status", "Description",
                                           "SQL", "Notes"]], hide_index=True, width="stretch")
            st.code(pb.measures_dax_query(ms), language="sql")
            st.caption("Paste into Power BI Desktop's DAX query view and click 'Update model with changes'. "
                       "Measures come from the ontology's metrics; certify them in the Review queue.")
        else:
            st.info("No metrics are based on this table. Add one in the Network inspector or promote a learned "
                    "expression in the Review queue.")

    with t_m:
        host = os.getenv("DATABRICKS_HOST", "").replace("https://", "").rstrip("/") or "<workspace-host>"
        wid = os.getenv("SQL_WAREHOUSE_ID", "")
        path = f"/sql/1.0/warehouses/{wid}" if wid and not wid.startswith("REPLACE") else "/sql/1.0/warehouses/<id>"
        inc = st.toggle("Incremental refresh filter", value=card["storage_mode"].startswith("Import + incremental"),
                        key=f"dd_inc_{cid}", disabled=not card["main_date"],
                        help="Adds the RangeStart/RangeEnd filter on " + (pb.friendly(card["main_date"])
                                                                         if card["main_date"] else "a date column"))
        st.code(pb.power_query_m(onto, cid, host, path, incremental=inc), language="powerquery")
        st.caption("Home > Get data > Blank query > Advanced editor. It keeps only the useful columns, renames them "
                   "to the business names and sets the Power BI data types. Server and HTTP path come from this "
                   "app's SQL warehouse.")

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
            st.caption("No learned usage yet: learn from query history in the Knowledge tab.")
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
