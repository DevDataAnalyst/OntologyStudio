"""Test: prove the model answers the questions the business asks.

  🎯 Test set      the golden questions (= verified examples): run them all, ontology vs schema-only,
                   and see what passes, what fails and what regressed since the last run
  💬 Ask           one question at a time (and Check SQL, Join path, Look up values, Verified examples)
  📈 Traces        every request, its prompt assets, timing, tokens, cost and feedback
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import answering as an, governance_ui as gui, testset as ts, workbench_ui as wb
from .common import ON_DATABRICKS, commit, live_run, runner, ss, sub_view, uc_runner

VIEWS = ["🎯 Test set", "💬 Ask", "📈 Traces"]


def render(onto: dict) -> None:
    view = sub_view("Test view", VIEWS, "ts_view")
    if view == VIEWS[0]:
        _test_set(onto)
    elif view == VIEWS[1]:
        gui.ask_tab(onto, runner(), uc_runner, ON_DATABRICKS)
    else:
        gui.traces_view(uc_runner, ON_DATABRICKS)


def _answer_fn(onto: dict, mode: str, run):
    from ..ask import answer
    from ..llm_enrich import chat
    dialect = wb.dialect_of(runner())

    def fn(question: str) -> dict:     # hide the question's own verified SQL, or the test only copies it
        held_out = dict(onto, examples=[e for e in onto.get("examples") or []
                                        if e.get("question", "").casefold() != question.casefold()])
        return answer(question, held_out, mode, dialect, execute=run, chat_fn=chat)
    return fn


def _test_set(onto: dict) -> None:
    qs = ts.questions(onto)
    st.markdown("The **test set** is the model's golden questions: each one a business question plus the SQL a "
                "person confirmed. Run them after every change; Publish shows the result, and the questions also "
                "go to the Genie space as sample questions and benchmarks.")
    with st.expander("➕ Add a test question", expanded=not qs):
        with st.form("ts_add", clear_on_submit=True):
            q = st.text_input("Question", placeholder="e.g. Operating ratio by region last quarter")
            sql = st.text_area("SQL that answers it", height=120, placeholder="SELECT …")
            if st.form_submit_button("Add", type="primary") and q and sql:
                try:
                    an.save_example(onto, q, sql, by=gui.current_user())
                    commit(f"Test question added: {q[:60]}")
                except ValueError as e:
                    st.error(str(e))
        st.caption("Questions you give a 👍 in Ask, or save from Check SQL, are added here too.")
    if not qs:
        return
    run = live_run()
    c1, c2, c3 = st.columns([1.2, 1.6, 3])
    baseline = c2.checkbox("Also run schema-only (shows what the model adds)", value=True, key="ts_baseline")
    if not ON_DATABRICKS:
        c3.info("Running the test set needs the Foundation Model endpoint (Databricks).")
    if c1.button(f"▶ Run {len(qs)} question(s)", type="primary", disabled=not ON_DATABRICKS):
        bar = st.progress(0.0, "Running the test set…")
        res = ts.run_set(onto, _answer_fn(onto, "ontology", run), run,
                         baseline_fn=_answer_fn(onto, "baseline", run) if baseline else None,
                         by=gui.current_user(), progress=lambda f, m: bar.progress(f, m))
        bar.empty()
        commit(f"Test set run: {res['passed']} of {res['total']} pass")
    r = ts.last_run(onto)
    status = ts.status(onto)
    if r:
        m = st.columns(4)
        m[0].metric("With the model", f"{r['passed']} / {r['total']}")
        if r.get("baseline_passed") is not None:
            m[1].metric("Schema only", f"{r['baseline_passed']} / {r['total']}")
        m[2].metric("Last run", (r.get("at") or "")[:16].replace("T", " "))
        reg = ts.regressions(onto)
        m[3].metric("Regressions", len(reg))
        if status["stale"]:
            st.warning("The model or its questions changed since the last run. Run the set again.")
        for q in reg:
            st.error(f"Passed before, fails now: {q}")
    by_q = {x["question"]: x for x in (r or {}).get("items", [])}
    rows = []
    for e in qs:
        x = by_q.get(e["question"], {})
        o, b = x.get("ontology") or {}, x.get("baseline") or {}
        rows.append({"question": e["question"],
                     "with model": "–" if not o else ("✅" if o.get("passed") else "❌"),
                     "why": o.get("why") or "", "schema only": "–" if not b else ("✅" if b.get("passed") else "❌"),
                     "by": e.get("by") or "", "saved": e.get("at") or ""})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    pick = st.selectbox("Open a question", [e["question"] for e in qs], key="ts_pick")
    e = next(x for x in qs if x["question"] == pick)
    x = by_q.get(pick, {})
    a, b = st.columns(2)
    a.markdown("**Expected SQL**")
    a.code(e["sql"], language="sql", wrap_lines=True)
    if (x.get("ontology") or {}).get("sql"):
        b.markdown("**SQL from the model (last run)**")
        b.code(x["ontology"]["sql"], language="sql", wrap_lines=True)
    d1, d2, _ = st.columns([1, 1, 3])
    if d1.button("🧪 Open in Check SQL", use_container_width=True):
        ss.wb_prefill = (e["sql"], e["question"])
        ss.ask_mode_next = wb.MODES[1]
        ss["ts_view_pick"] = VIEWS[1]
        ss.pop("ts_view", None)
        st.rerun()
    if d2.button("🗑️ Remove", use_container_width=True):
        an.delete_example(onto, pick)
        commit("Test question removed")
