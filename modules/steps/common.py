"""Shared state and helpers for the six steps (Connect, Model, Describe, Review, Test, Publish) and Settings."""
from __future__ import annotations

import os

import streamlit as st

ON_DATABRICKS = bool(os.getenv("DATABRICKS_APP_NAME") or os.getenv("DATABRICKS_HOST"))
DEFAULT_CATALOG = os.getenv("TARGET_CATALOG", "")
DEFAULT_SCHEMA = os.getenv("TARGET_SCHEMA", "")
MAX_TABLES = 100

STEPS = {"connect": "1 · Connect", "model": "2 · Model", "describe": "3 · Describe", "review": "4 · Review",
         "test": "5 · Test", "publish": "6 · Publish", "settings": "⚙️ Settings"}
NEEDS_MODEL = {"model", "describe", "review", "test"}

ss = st.session_state


def init_state() -> None:
    for k, v in (("onto", None), ("selection", {"kind": None}), ("last_nonce", None), ("graph_msg", None),
                 ("graph_key", 0), ("uc_cache", {}), ("runner", {}), ("advanced", False)):
        ss.setdefault(k, v)


def goto(step: str) -> None:
    """Switch step on the next run (a widget's own key can't be set after it is drawn)."""
    ss["_goto"] = step


def advanced() -> bool:
    return bool(ss.get("advanced"))


def user_token() -> str | None:
    if not ON_DATABRICKS:
        return None
    from modules.db_connection import get_user_token
    return get_user_token()


def uc_runner():
    from modules.db_connection import make_runner
    return make_runner()


def runner() -> dict:
    return ss.get("runner") or {}


def live_run():
    """run(sql) on the open model's data with the viewer's permissions, or None."""
    from modules import workbench_ui as wb
    return wb.make_run(runner(), uc_runner)


def cached(key, fn):
    if key not in ss.uc_cache:
        ss.uc_cache[key] = fn()
    return ss.uc_cache[key]


def commit(msg: str | None = None, rerun: bool = True):
    """Persist a mutation of ss.onto, log it in the model's change log, and redraw."""
    from modules import lifecycle_ui as lc
    from modules import modeling as md
    from modules.ontology_builder import refresh_index
    refresh_index(ss.onto)
    if msg:
        ss.graph_msg = msg
        md.log_change(ss.onto, msg, lc.current_user())
    if rerun:
        st.rerun()


def sub_view(label: str, options: list[str], key: str, format_func=None) -> str:
    """A row of buttons for the views inside a step (only the picked one runs, which keeps the app fast).
    The pick is remembered under <key>_pick, because a widget's state is dropped on runs that don't draw it."""
    default = ss.get(f"{key}_pick")
    if default not in options:
        default = options[0]
        ss.pop(key, None)
    pick = st.segmented_control(label, options, default=default, key=key, label_visibility="collapsed",
                                format_func=format_func or str) or default
    ss[f"{key}_pick"] = pick
    return pick
