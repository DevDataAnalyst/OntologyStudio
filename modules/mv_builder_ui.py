"""Metric views tab → ➕ New metric view: a guided, form-based builder.

① pick the table the measures aggregate (facts first) · ② tick joins · ③ fields and ④ measures from
suggestions that say why they fit (the model's metrics, profiled values, what colleagues query), add
your own with quick helpers · ⑤ filter · ⑥ live checks and YAML preview, then add the view to the
model (it is saved with the model's versions), download it or deploy it to Unity Catalog."""
from __future__ import annotations

import hashlib
import json
from typing import Callable

import pandas as pd
import streamlit as st

from . import metric_views as mv

ss = st.session_state
SHOW = ["pick", "name", "why", "display_name", "expr"]
AGGS = {"SUM": "SUM({c})", "AVG": "AVG({c})", "MIN": "MIN({c})", "MAX": "MAX({c})",
        "COUNT DISTINCT": "COUNT(DISTINCT {c})"}
CONFIG = {"pick": st.column_config.CheckboxColumn("✓", default=True, width="small"),
          "name": st.column_config.TextColumn("Name"),
          "display_name": st.column_config.TextColumn("Display name"),
          "expr": st.column_config.TextColumn("SQL expression", width="large"),
          "why": st.column_config.TextColumn("Why it's suggested", width="large")}


def _sig(rows: list[dict]) -> str:
    return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _rows(df: pd.DataFrame) -> list[dict]:
    return [{k: (None if isinstance(v, float) and v != v else v) for k, v in r.items()}
            for r in df.to_dict("records")]


def _with_meta(rows: list[dict], suggested: list[dict]) -> list[dict]:
    """Give back the comment / synonyms of suggested rows whose expression is unchanged."""
    meta = {s["expr"]: s for s in suggested}
    return [{**r, "comment": meta.get(r.get("expr"), {}).get("comment"),
             "synonyms": meta.get(r.get("expr"), {}).get("synonyms")} for r in rows]


def _table(rows: list[dict], key: str) -> pd.DataFrame:
    """Tickable, editable suggestions (+ rows you add). The source lives in session state so the quick
    helpers can append a row without losing your ticks and edits."""
    if f"{key}_src" not in ss:
        ss[f"{key}_src"] = pd.DataFrame([{k: r.get(k) for k in SHOW} for r in rows], columns=SHOW)
    return st.data_editor(ss[f"{key}_src"], key=f"{key}_v{ss.get(f'{key}_n', 0)}", hide_index=True,
                          width="stretch", num_rows="dynamic", disabled=["why"], column_config=CONFIG)


def _append(key: str, edited: pd.DataFrame, row: dict) -> None:
    ss[f"{key}_src"] = pd.concat([edited, pd.DataFrame([row], columns=SHOW)], ignore_index=True)
    ss[f"{key}_n"] = ss.get(f"{key}_n", 0) + 1
    st.rerun()


def _set(key: str, value) -> None:
    ss[key] = value


def _lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def builder(onto: dict, commit: Callable[[str], None], deploy: Callable[[str], None] | None) -> None:
    classes = onto["classes"]
    facts = mv.fact_classes(onto)
    order = facts + sorted(c for c in classes if c not in facts)
    if not order:
        st.info("Open or build a model first.")
        return
    st.caption("Build a metric view step by step. Every step is pre-filled from your model, its data profile and "
               "the queries your team runs, and says why. Untick what you don't need, add your own, check, save.")

    def label(c: str) -> str:
        n = sum(1 for m in onto.get("metrics", []) if m.get("base_class") == c)
        return f"{c} — {'fact table · ' if c in facts else ''}{n} metric(s) in the model"

    pick = ss.get("mvb_fact_pick")
    fact = st.selectbox("① Start from: the table whose rows the measures add up", order, format_func=label,
                        index=order.index(pick) if pick in order else 0,
                        help="Fact tables come first: they hold the events (sales, invoices, shipments…) you sum "
                             "and count.")
    ss.mvb_fact_pick = fact      # survives an st.rerun() earlier in the run, which wipes widget state
    sug = mv.suggest(onto, fact)

    st.markdown("**② Joins** · the tables you can slice by")
    names = [j["name"] for j in sug["joins"]]
    joins = []
    if names:
        joins = st.pills("Joins", names, selection_mode="multi", default=names, key=f"mvb_j_{fact}",
                         label_visibility="collapsed") or []
        st.caption("  \n".join(f"**{j['name']}**: {j['why']}" for j in sug["joins"]))
    else:
        st.caption("No many-to-one relationships from this table: the view uses its own columns.")

    st.markdown("**③ Fields** · what people group and filter by (ticked: the generated view's fields)")
    frows = [r for r in sug["fields"] if set(r["via"]) <= set(joins)]
    fed = _table(frows, f"mvb_f_{fact}_{_sig(frows)}")

    st.markdown("**④ Measures** · the numbers (ticked: the model's metrics; more ideas below them)")
    mrows = [r for r in sug["measures"] if set(r["via"]) <= set(joins)]
    mkey = f"mvb_m_{fact}_{_sig(mrows)}"
    med = _table(mrows, mkey)
    with st.container(border=True):
        cols = [c for c in sug["columns"] if set(c["via"]) <= set(joins)]
        q1, q2, q3 = st.columns([1.3, 3, 1.2], vertical_alignment="bottom")
        agg = q1.selectbox("Quick add: aggregate", list(AGGS), key=f"{mkey}_agg")
        pool = [c for c in cols if c["numeric"] or agg == "COUNT DISTINCT"]
        col = q2.selectbox("of column", pool, format_func=lambda c: ("📏 " if c["measure"] else "") + c["label"],
                           key=f"{mkey}_col", help="📏 = measure columns of the model. SUM / AVG / MIN / MAX take "
                                                   "numeric non-key columns; COUNT DISTINCT takes any column.")
        if q3.button("➕ Add measure", key=f"{mkey}_add", disabled=col is None, use_container_width=True):
            stem = col["expr"].replace(".", "_").strip("()`")
            _append(mkey, med, {"pick": True, "name": mv._snake(f"{agg}_{stem}"), "display_name": None,
                                "expr": AGGS[agg].format(c=col["expr"]), "why": "added by you"})
        ticked = [r["name"] for r in _rows(med) if r.get("pick") and r.get("name")]
        r1, r2, r3 = st.columns([2.15, 2.15, 1.2], vertical_alignment="bottom")
        a = r1.selectbox("Quick add: ratio of", ticked, key=f"{mkey}_ra")
        b = r2.selectbox("divided by", ticked, index=min(1, len(ticked) - 1) if ticked else None, key=f"{mkey}_rb")
        if r3.button("➕ Add ratio", key=f"{mkey}_radd", disabled=not (a and b) or a == b, use_container_width=True):
            _append(mkey, med, {"pick": True, "name": f"{a}_per_{b}", "display_name": None,
                                "expr": f"MEASURE({a}) / NULLIF(MEASURE({b}), 0)",
                                "why": "added by you: a ratio of two measures (e.g. revenue per order)"})

    st.markdown("**⑤ Filter** · optional, keeps only some source rows in every query")
    fk = f"mvb_flt_{fact}"
    if sug["filters"]:
        why_f = {f["expr"]: f["why"] for f in sug["filters"]}
        s1, s2 = st.columns([5, 1], vertical_alignment="bottom")
        pick = s1.selectbox("Your team often filters on", list(why_f), format_func=lambda e: f"{e}  ({why_f[e]})",
                            key=f"{fk}_s")
        s2.button("Use", key=f"{fk}_use", on_click=_set, args=(fk, pick), use_container_width=True)
    if sug["values"]:
        v1, v2, v3 = st.columns([1.5, 3.5, 1], vertical_alignment="bottom")
        vcol = v1.selectbox("Keep rows where", list(sug["values"]), key=f"{fk}_c")
        vals = v2.multiselect("is one of (profiled values)", sug["values"][vcol]["values"], key=f"{fk}_v_{vcol}")
        cond = f"{sug['values'][vcol]['expr']} IN ({', '.join(_lit(v) for v in vals)})" if vals else ""
        v3.button("Use", key=f"{fk}_vuse", disabled=not vals, on_click=_set, args=(fk, cond),
                  use_container_width=True)
    flt = st.text_input("Filter (SQL condition on the source table's columns)", key=fk,
                        placeholder="e.g. order_status <> 'Cancelled'")

    st.markdown("**⑥ Name, check and save**")
    fq = classes[fact].get("source_fqn") or ""
    prefix = ".".join(fq.split(".")[:2]) + "." if fq.count(".") == 2 else ""
    n1, n2 = st.columns([3, 2])
    target = n1.text_input("Metric view (catalog.schema.view)", f"{prefix}{mv._snake(fact)}_custom_metrics",
                           key=f"mvb_t_{fact}")
    comment = n2.text_input("Description (optional)", key=f"mvb_c_{fact}")
    view = mv.compose(sug["base"], joins, _with_meta(_rows(fed), sug["fields"]),
                      _with_meta(_rows(med), sug["measures"]), flt, comment)
    problems = mv.check(view, sug["base"])
    y = mv.to_yaml(view)
    vname = mv._snake(target.rpartition(".")[2]) or "metric_view"
    m = st.columns(4)
    m[0].metric("Joins", len(view.get("joins", [])))
    m[1].metric("Fields", len(view.get("fields", [])))
    m[2].metric("Measures", len(view["measures"]))
    m[3].metric("Filter", "yes" if view.get("filter") else "none")
    for p in problems:
        st.error(p)
    if not problems:
        st.success("Ready: this view passes the metric view checks (unique names, aggregating measures, "
                   "joins in place).")
    st.code(y, language="yaml")
    saved = vname in (onto.get("metric_views") or {})
    b1, b2, b3 = st.columns(3)
    if b1.button("💾 Update in the model" if saved else "💾 Add to the model", type="primary",
                 disabled=bool(problems), use_container_width=True, key="mvb_save",
                 help="Lists it under 📄 Views and saves it with the model (Ossie export, versions)."):
        onto.setdefault("metric_views", {})[vname] = json.loads(json.dumps(
            {"fact": fact, "target": target, "view": view}))           # a plain copy: no shared objects
        commit(f"Metric view {vname} {'updated' if saved else 'added'}: see 📄 Views")
    b2.download_button("⬇️ YAML", y, file_name=f"{vname}.yaml", mime="application/x-yaml",
                       disabled=bool(problems), use_container_width=True, key="mvb_dl")
    if b3.button("🚀 Deploy to Unity Catalog", disabled=bool(problems) or deploy is None or target.count(".") != 2,
                 use_container_width=True, key="mvb_deploy",
                 help="Runs CREATE OR REPLACE VIEW … WITH METRICS as you (needs CREATE TABLE on the schema). "
                      "Available when the model's tables are in Unity Catalog."):
        try:
            deploy(mv.ddl(y, target))
            st.success(f"Created metric view {target}")
        except Exception as e:  # noqa: BLE001
            st.error(f"Deploy failed: {e}")
    with st.expander("Deploy SQL"):
        st.code(mv.ddl(y, target), language="sql")
