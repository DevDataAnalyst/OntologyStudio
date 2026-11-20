"""
Streamlit views for the answering workbench (modes of the Ask tab) plus the glossary and readiness bits.
  check_sql_view   Check SQL: validate against the model and the data, run with a row cap, save as verified example
  join_path_view   the shortest chain of modeled joins between two classes, as a FROM/JOIN clause
  lookup_view      real values stored in a column
  examples_view    verified question + SQL pairs: inspect, reopen, delete
  glossary_box     glossary editor (Semantic layer tab)
  readiness_line   one line under the header metrics
"""
from __future__ import annotations

import sqlite3
import time

import pandas as pd
import streamlit as st

from . import answering as an
from . import sqlcheck as sc
from . import telemetry as tm

ss = st.session_state
MODES = ["💬 Ask", "🧪 Check SQL", "🔗 Join path", "🔎 Look up values", "✅ Verified examples"]


def make_run(runner: dict, uc_runner=None):
    """run(sql) -> DataFrame on the open model's data with the viewer's permissions, or None (no live data)."""
    if runner.get("kind") == "uc" and uc_runner is not None:
        return uc_runner()
    if runner.get("kind") == "sqlite" and runner.get("path"):
        path = runner["path"]

        def run(sql, params=None):
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return pd.read_sql_query(sql, conn, params=params)
            finally:
                conn.close()
        return run
    return None


def dialect_of(runner: dict) -> str:
    d = runner.get("dialect") or "databricks"
    return "databricks" if d == "auto" else d


def render(mode: str, onto: dict, runner: dict, uc_runner, user: str, commit) -> None:
    if mode == MODES[1]:
        check_sql_view(onto, runner, uc_runner, user, commit)
    elif mode == MODES[2]:
        join_path_view(onto, runner)
    elif mode == MODES[3]:
        lookup_view(onto, runner, uc_runner)
    elif mode == MODES[4]:
        examples_view(onto, commit)


def show_checks(r: dict) -> None:
    for e in r.get("errors") or []:
        st.error(e)
    for w in r.get("warnings") or []:
        st.warning(w)
    for n in r.get("notes") or []:
        st.caption("• " + n)
    g = r.get("grounding") or {}
    if g.get("classes"):
        st.caption("Reads " + ", ".join(g["classes"])
                   + (f" · not certified yet: {', '.join(g['draft_classes'])}" if g.get("draft_classes") else "")
                   + (" · metrics: " + ", ".join(f"{m['name']} ({m['status']})" for m in g["metrics"])
                      if g.get("metrics") else ""))


# --------------------------------------------------------------------------- #
# Check SQL
# --------------------------------------------------------------------------- #
def check_sql_view(onto: dict, runner: dict, uc_runner, user: str, commit) -> None:
    run, d = make_run(runner, uc_runner), dialect_of(runner)
    if "wb_prefill" in ss:                               # "Open in Check SQL" from a verified example
        ss.wb_sql, ss.wb_q = ss.pop("wb_prefill")
    st.caption("Paste any query. It is checked against the model (tables, columns, joins, fan-out) "
               + ("and against the data with small read-only probes (values, ranges, a dry run). "
                  if run else "only: open the model from Unity Catalog or a SQLite file to probe the data too. ")
               + "Nothing is ever written.")
    sql = st.text_area("SQL", key="wb_sql", height=200,
                       placeholder="SELECT c.region, SUM(s.net_amount) AS revenue\nFROM ... JOIN ... GROUP BY 1")
    q = st.text_input("Question it answers (optional; checks percentages, needed to save a verified example)",
                      key="wb_q")
    c1, c2, c3 = st.columns(3)
    if c1.button("🧪 Check", type="primary", disabled=not sql.strip(), use_container_width=True):
        with st.spinner("Checking against the model" + (" and the data…" if run else "…")):
            ss.wb_report = {"sql": sql, **sc.validate(sql, onto, d, run=run, question=q or None)}
        ss.pop("wb_result", None)
    if c2.button("▶️ Run", disabled=not (sql.strip() and run), use_container_width=True,
                 help="Checks the query, then runs it read-only with your permissions (at most 200 rows)."):
        with st.spinner("Checking and running…"):
            rep = ss.wb_report = {"sql": sql, **sc.validate(sql, onto, d, run=run, question=q or None, dry_run=False)}
            ss.wb_result = None
            if rep["valid"]:
                t = tm.new_trace(q or sql[:120], user, "check", None)
                t["sql"] = sql
                try:
                    t0 = time.time()
                    df, cut = sc.run_capped(run, sql, d)
                    t.update(status="ok", rows=len(df), sql_ms=int((time.time() - t0) * 1000))
                    ss.wb_result = (df, cut)
                except Exception as e:  # noqa: BLE001
                    t.update(status="error", error=str(e)[:500])
                    rep["errors"].append(f"Run failed: {str(e)[:400]}")
                ss.setdefault("traces", []).insert(0, t)
                if runner.get("kind") == "uc" and tm.table_enabled():
                    tm.save_trace(uc_runner(), t)
    if c3.button("✅ Save as verified example", disabled=not (sql.strip() and q.strip()), use_container_width=True,
                 help="Keeps the question and this SQL in the model. Ask reuses it for similar questions."):
        rep = sc.validate(sql, onto, d, run=run, question=q)
        if not rep["valid"]:
            ss.wb_report = {"sql": sql, **rep}
            st.error("Not saved: fix the errors below first.")
        else:
            an.save_example(onto, q, sql, by=user)
            commit(f"Saved verified example “{q.strip()}”")
    rep = ss.get("wb_report")
    if rep and rep.get("sql") == sql:
        if rep["valid"] and not rep["warnings"]:
            st.success("No problems found" + (f" ({rep['probes']} data probe(s))." if rep.get("probes") else "."))
        show_checks(rep)
        res = ss.get("wb_result")
        if res:
            st.dataframe(res[0], hide_index=True, width="stretch")
            st.caption(f"{len(res[0])} row(s)" + (" · cut at 200 rows: filter or aggregate for a complete answer"
                                                  if res[1] else ""))


# --------------------------------------------------------------------------- #
# Join path, values, examples
# --------------------------------------------------------------------------- #
def join_path_view(onto: dict, runner: dict) -> None:
    names = list(onto.get("classes", {}))
    if len(names) < 2:
        st.info("The model needs at least two classes.")
        return
    st.caption("The shortest chain of modeled relationships between two classes, as a FROM/JOIN clause you can "
               "paste into a query. Ask uses the same paths.")
    c1, c2 = st.columns(2)
    a = c1.selectbox("From", names, key="jp_a")
    b = c2.selectbox("To", names, index=1, key="jp_b")
    v = an.path_view(onto, a, b, dialect_of(runner))
    if not v["found"]:
        st.warning(v["notes"][0])
        return
    if v["hops"]:
        st.dataframe(pd.DataFrame(v["hops"]), hide_index=True, width="stretch")
    st.code(v["sql"], language="sql")
    for n in v["notes"]:
        st.caption("• " + n)


def lookup_view(onto: dict, runner: dict, uc_runner) -> None:
    run = make_run(runner, uc_runner)
    classes = onto.get("classes", {})
    if not classes:
        return
    st.caption("Real values stored in a column: use them for literal filters (a place, product or status).")
    c1, c2, c3 = st.columns([1.2, 1.2, 1.6])
    cid = c1.selectbox("Class", list(classes), key="lv_cls")
    props = {p.get("source_column") or pid: p for pid, p in (classes[cid].get("data_properties") or {}).items()
             if not p.get("expression")}
    col = c2.selectbox("Column", list(props), key=f"lv_col_{cid}")
    word = c3.text_input("Containing (optional)", key="lv_word", placeholder="e.g. fran")
    if col is None:
        return
    p = props[col]
    if p.get("allowed_values") or p.get("samples"):
        st.caption(("All values: " if p.get("allowed_values") else "Profiled samples: ")
                   + ", ".join(map(str, p.get("allowed_values") or p.get("samples"))))
    if run is None:
        st.info("Live lookups need the model's data: open it from Unity Catalog tables or a SQLite file.")
        return
    if st.button("🔎 Look up", type="primary"):
        try:
            with st.spinner("Reading values…"):
                ss.lv_result = sc.lookup_values(run, onto, cid, col, contains=word or None,
                                                dialect=dialect_of(runner))
        except Exception as e:  # noqa: BLE001
            st.error(str(e)[:400])
    res = ss.get("lv_result")
    if res and res["column"].endswith(f".{col}"):
        rows = res.get("matches", res.get("top_values")) or []
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        elif res.get("contains"):
            st.warning(f"No value of {res['column']} contains '{res['contains']}'."
                       + (f" Close spellings: {', '.join(map(str, res['close_values']))}."
                          if res.get("close_values") else ""))
        if res.get("more_values_exist"):
            st.caption("More values exist; type part of one to search.")


def examples_view(onto: dict, commit) -> None:
    ex = onto.get("examples") or []
    st.caption("Questions a person confirmed, with the SQL that answered them. Ask starts from the closest one; "
               "they are saved with the model and exported to Ossie (the questions as ai_context examples).")
    if not ex:
        st.info("No verified examples yet. Give an Ask answer a 👍, or save one from 🧪 Check SQL.")
        return
    st.dataframe(pd.DataFrame([{"question": e.get("question"), "note": e.get("note") or "", "by": e.get("by") or "",
                                "saved": e.get("at") or ""} for e in ex]), hide_index=True, width="stretch")
    pick = st.selectbox("Example", [e["question"] for e in ex], key="ex_pick")
    e = next(x for x in ex if x["question"] == pick)
    st.code(e["sql"], language="sql", wrap_lines=True)
    c1, c2 = st.columns(2)
    if c1.button("🧪 Open in Check SQL", use_container_width=True):
        ss.wb_prefill = (e["sql"], e["question"])
        ss.ask_mode_next = MODES[1]
        st.rerun()
    if c2.button("🗑️ Delete", use_container_width=True):
        an.delete_example(onto, pick)
        commit("Verified example deleted")


# --------------------------------------------------------------------------- #
# Glossary and readiness
# --------------------------------------------------------------------------- #
def glossary_box(onto: dict, commit) -> None:
    st.markdown("**Glossary** — abbreviations and jargon (MRR → monthly recurring revenue). Ask expands them "
                "before it looks a question up.")
    df = pd.DataFrame(onto.get("glossary") or [], columns=["name", "definition"])
    edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch",
                            key=f"glossary_{ss.get('glossary_rev', 0)}",
                            column_config={"name": st.column_config.TextColumn("Term", help="e.g. MRR"),
                                           "definition": st.column_config.TextColumn(
                                               "Means", help="e.g. monthly recurring revenue")})
    if st.button("Apply glossary", key="glossary_apply"):
        an.set_glossary(onto, edited.to_dict("records"))
        ss.glossary_rev = ss.get("glossary_rev", 0) + 1      # a fresh editor, so rows are not added twice
        commit("Glossary updated")


def readiness_line(onto: dict, runner: dict) -> None:
    r = an.readiness(onto, runner.get("kind"))
    st.caption(f"{'✅' if r['ready'] else '🟡'} **Readiness: {r['verdict']}.** {r['next_step']}  \n{r['facts']}")
