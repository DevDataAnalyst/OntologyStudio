"""
Ontology Studio — Databricks App (Streamlit)

One journey, six steps (a row of buttons at the top; only the open step runs, which keeps the app fast):

  1 Connect   Unity Catalog tables, a saved model, an imported file (Apache Ossie, studio YAML, SQLite,
              .sql) or a blank form                                        modules/steps/connect.py
  2 Model     the editable network diagram + inspector, a list view, the form editor, (Advanced) YAML
                                                                           modules/steps/model.py
  3 Describe  dictionary, code values, glossary, rules, metrics and metric views, domains, knowledge
                                                                           modules/steps/describe.py
  4 Review    one inbox: blockers, suggested joins, the review queue, the worklist, the change log
                                                                           modules/steps/review.py
  5 Test      the test set (golden questions), Ask and its tools, traces modules/steps/testing.py
  6 Publish   save a version, deploy to Databricks (YAML, comments, tags, metric views, keys, Genie
              space), exports                                              modules/steps/publish.py
  Settings    configuration, Ask prices and budgets, warehouse spend      modules/steps/settings.py
"""
import streamlit as st

from modules import guide_ui, lifecycle_ui as lc, store, testset, theme
from modules.steps import common
from modules.steps.common import NEEDS_MODEL, STEPS, goto, ss

st.set_page_config(page_title="Ontology Studio", page_icon="🕸️", layout="wide")
common.init_state()


def tab_css(c: dict) -> str:
    """Chromium-style tab strip for every st.tabs: separate rounded tabs on a bar; the active tab joins the
    panel below it with the curved 'flares' Chrome draws at its bottom corners. Colours come from the active
    light/dark theme in .streamlit/config.toml (see modules/theme.py)."""
    bg, txt = c["backgroundColor"], c["textColor"]
    strip, active = c["secondaryBackgroundColor"], bg
    hover = f"color-mix(in srgb, {txt} 7%, {strip})"
    sep = f"color-mix(in srgb, {txt} 28%, {strip})"
    border = f"color-mix(in srgb, {c['borderColor']} 70%, {bg})"
    muted = f"color-mix(in srgb, {txt} 72%, {bg})"
    tl, tab = '.stTabs [role="tablist"]', '.stTabs [role="tab"]'   # react-aria (1.5x+) and baseweb DOMs
    return f"""<style>
{tl} {{
  gap: 0; background: {strip}; padding: 7px 14px 0 14px; border-radius: 12px 12px 0 0;
  border: 1px solid {border}; border-bottom: none; box-shadow: none; overflow-x: auto; overflow-y: hidden;
  scrollbar-width: thin; }}
.stTabs [data-baseweb="tab-border"], .stTabs [data-baseweb="tab-highlight"],
.stTabs .react-aria-SelectionIndicator {{ display: none !important; }}
{tab} {{
  position: relative; height: 38px; margin: 0; padding: 0 15px; flex: 0 0 auto; display: flex;
  align-items: center; background: transparent; border: none; border-radius: 10px 10px 0 0; color: {muted};
  transition: background .12s ease; }}
{tab} p {{ font-size: .9rem; white-space: nowrap; margin: 0; }}
{tab}:not(:first-of-type)::before {{
  content: ""; position: absolute; left: 0; top: 11px; bottom: 11px; width: 1px; background: {sep}; }}
{tab}:hover {{ background: {hover}; color: {txt}; }}
{tab}[aria-selected="true"] {{
  background: {active}; color: {txt}; z-index: 1; box-shadow: 0 -1px 3px rgba(0,0,0,.10); }}
{tab}[aria-selected="true"] p {{ font-weight: 650; }}
{tab}[aria-selected="true"]::before, {tab}[aria-selected="true"] + [role="tab"]::before,
{tab}:hover::before, {tab}:hover + [role="tab"]::before {{ opacity: 0; }}
{tab}[aria-selected="true"]::after {{
  content: ""; position: absolute; bottom: 0; left: -10px; right: -10px; height: 10px; pointer-events: none;
  background:
    radial-gradient(circle at 0 0, transparent 9.5px, {active} 10px) left bottom / 10px 10px no-repeat,
    radial-gradient(circle at 100% 0, transparent 9.5px, {active} 10px) right bottom / 10px 10px no-repeat; }}
.st-key-theme_watcher {{ display: none; }}
h1, h2, h3, h4, h5, h6 {{ font-variant-numeric: lining-nums; }}  /* Raleway defaults to old-style figures */
.st-key-step p, .st-key-step span, .st-key-step button {{ font-size: 16px; }}
.stTabs [role="tabpanel"] {{
  background: {active}; border: 1px solid {border}; border-top: none; border-radius: 0 0 12px 12px;
  padding: 18px 18px 14px 18px; margin-top: 0; }}
</style>"""



st.markdown(tab_css(theme.colors()), unsafe_allow_html=True)
theme.watch()

if "_goto" in ss:                                   # a step asked to move on (e.g. Connect -> Model)
    ss["step"] = ss.pop("_goto")
ss.setdefault("step", "connect")
step = st.segmented_control("Step", list(STEPS), format_func=STEPS.get, required=True, key="step",
                            label_visibility="collapsed") or "connect"
onto = ss.onto


# =========================================================================== #
# Sidebar: the open model at a glance
# =========================================================================== #
with st.sidebar:
    guide_ui.sidebar_button()
    st.title("🕸️ Ontology Studio")
    if onto is None:
        st.caption("No model open. Start in **1 · Connect**.")
    else:
        from modules import answering as an
        name = (onto.get("ontology") or {}).get("name") or "model"
        ref = ss.get("model_ref") or {}
        st.markdown(f"**{name}**  \n{len(onto['classes'])} tables · {len(onto.get('metrics', []))} metrics")
        if ref.get("version"):
            changed = ref.get("text") and lc.ossie_text(onto)[0] != ref["text"]
            st.caption(f"Version {store.pretty_stamp(ref['version'])}" + (" · ✏️ unsaved changes" if changed else
                                                                          " · saved"))
        else:
            st.caption("Not saved yet")
        r = an.readiness(onto, common.runner().get("kind"))
        st.caption(f"{'✅' if r['ready'] else '🟡'} Readiness: {r['verdict']}")
        t = testset.status(onto)
        st.caption(f"🎯 Test set: {t['verdict']}")
        if st.button("💾 Save or publish", use_container_width=True, disabled=step == "publish"):
            goto("publish")
            st.rerun()
    st.divider()
    st.toggle("Advanced mode", key="advanced",
              help="Shows expert options: IRIs and naming, discovery thresholds, and the studio model YAML.")


# =========================================================================== #
# The open step
# =========================================================================== #
if step in NEEDS_MODEL and onto is None:
    st.info("Open or build a model first.")
    if st.button("Go to Connect", type="primary"):
        goto("connect")
        st.rerun()
    st.stop()

if onto is not None:
    from modules import governance as gv
    gv.ensure(onto)

if step == "connect":
    from modules.steps import connect
    connect.render()
elif step == "model":
    from modules.steps import model
    model.render(onto)
elif step == "describe":
    from modules.steps import describe
    describe.render(onto)
elif step == "review":
    from modules.steps import review
    review.render(onto)
elif step == "test":
    from modules.steps import testing
    testing.render(onto)
elif step == "publish":
    from modules.steps import publish
    publish.render(onto)
else:
    from modules.steps import settings
    settings.render()
