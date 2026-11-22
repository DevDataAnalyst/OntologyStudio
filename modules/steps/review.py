"""Review: one inbox for everything that needs a person's decision before the model is published.

  Readiness         one line: is the model ready, and the next thing to do
  🚧 Blockers        Apache Ossie export errors and model validation notes
  🔗 Suggested joins  joins discovery found but did not accept, with accept / reject, one by one or in bulk
  ✅ Queue           popular learned snippets, certification, conflicts, feedback hotspots, gaps, model checks
  🧭 Worklist        tables to describe, default joins, definitions to review
  📝 Change log      every edit, who made it and when
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import governance_ui as gui, lifecycle_ui as lc, modeling_ui as mu, ontology_edit as ed
from .. import workbench_ui as wb
from ..ontology_builder import validate
from .common import ON_DATABRICKS, commit, runner, ss, sub_view, uc_runner


def blockers(onto: dict) -> dict:
    """{"errors": [...], "notes": [...]} that should be fixed before publishing (cached with the Ossie export)."""
    _, _, rep = lc.ossie_text(onto)
    return {"errors": [f"{f.path}: {f.message}" if getattr(f, "path", None) else f.message for f in rep.errors],
            "notes": validate(onto)}


def pending(onto: dict) -> list[dict]:
    return [s for s in onto.get("suggested_relationships", []) if s.get("status") == "proposed"]


def render(onto: dict) -> None:
    wb.readiness_line(onto, runner())
    b, sug = blockers(onto), pending(onto)
    counts = {"blockers": len(b["errors"]), "joins": len(sug), "changes": len(onto.get("changes") or [])}
    names = {"blockers": "🚧 Blockers", "joins": "🔗 Suggested joins", "queue": "✅ Queue", "worklist": "🧭 Worklist",
             "changes": "📝 Change log"}
    view = sub_view("Review view", list(names), "rv_view",
                    format_func=lambda k: names[k] + (f" ({counts[k]})" if k in counts else ""))
    i = list(names).index(view)
    if i == 0:
        _blockers(b)
    elif i == 1:
        _suggestions(onto, sug)
    elif i == 2:
        gui.review_tab(onto, ON_DATABRICKS, runner(), uc_runner)
    elif i == 3:
        mu.worklist_view(onto, runner(), uc_runner, ON_DATABRICKS, commit, gui.traces())
    else:
        mu.changes_view(onto)


def _blockers(b: dict) -> None:
    if not b["errors"]:
        st.success("No blockers: the model exports as a valid Apache Ossie document.")
    for e in b["errors"]:
        st.error(e)
    if b["notes"]:
        st.markdown("**Model notes** (worth a look, they don't block publishing)")
        for n in b["notes"]:
            st.write("•", n)


def _suggestions(onto: dict, sug: list[dict]) -> None:
    if not sug:
        st.success("No suggested joins waiting.")
        return
    st.caption("Joins discovery found but didn't accept on its own. Accepted joins become relationships; rejected "
               "ones are remembered so they don't come back.")
    df = pd.DataFrame([{"accept": s["confidence"] >= 0.6, "reject": False, "from": f"{s['domain']}."
                        + ", ".join(s["from_cols"]), "to": f"{s['range']}." + ", ".join(s["to_cols"]),
                        "confidence": s["confidence"], "evidence": "; ".join(s.get("evidence") or [])[:300],
                        "id": s["id"]} for s in sorted(sug, key=lambda s: -s["confidence"])])
    edited = st.data_editor(df, hide_index=True, width="stretch", key=f"rv_sug_{len(sug)}",
                            disabled=["from", "to", "confidence", "evidence", "id"],
                            column_config={"id": None,
                                           "confidence": st.column_config.ProgressColumn(min_value=0, max_value=1)})
    if st.button("Apply", type="primary", key="rv_sug_apply"):
        acc = rej = 0
        for r in edited.itertuples(index=False):
            if r.accept and not r.reject:
                ed.accept_suggestion(onto, r.id)
                acc += 1
            elif r.reject:
                ed.reject_suggestion(onto, r.id)
                rej += 1
        ss.graph_key += 1
        commit(f"Suggested joins: {acc} accepted, {rej} rejected")
    st.caption("Ticked by default: suggestions with at least 60% confidence. Click a dashed join in the Model "
               "diagram to see it in context.")
