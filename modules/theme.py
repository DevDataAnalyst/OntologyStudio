"""
Light / dark theme support.

The two themes themselves live in .streamlit/config.toml ([theme.light] and [theme.dark]); users switch
between them in the app menu (System / Light / Dark). This module gives the app's own HTML/CSS (the
Chromium-style tabs, the D3 network diagram) the same colours:

  is_dark()  which theme the browser is showing right now
  colors()   the active theme's colours, read from config.toml so there is one source of truth
  watch()    invisible helper component: when the user switches theme, Streamlit does not rerun the
             script and st.context.theme lags behind, so CSS emitted from Python would keep the old
             colours. The helper hears the switch in the browser and triggers a rerun.
"""
from __future__ import annotations

import os

import streamlit as st
import streamlit.components.v1 as components

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "components", "theme_watcher")
_watcher = components.declare_component("theme_watcher", path=_DIR)
_KEY = "_theme_base"

# Used when an option is not set in config.toml (Streamlit's own defaults).
_DEFAULTS = {
    "light": {"primaryColor": "#ff4b4b", "backgroundColor": "#ffffff", "secondaryBackgroundColor": "#f0f2f6",
              "textColor": "#31333f", "borderColor": "#d5d7de", "font": "Arial, Helvetica, sans-serif"},
    "dark": {"primaryColor": "#ff4b4b", "backgroundColor": "#0e1117", "secondaryBackgroundColor": "#262730",
             "textColor": "#fafafa", "borderColor": "#3d4049", "font": "Arial, Helvetica, sans-serif"},
}


def is_dark() -> bool:
    base = st.session_state.get(_KEY)
    if base in ("light", "dark"):
        return base == "dark"
    try:
        return st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001
        return False


def _opt(mode: str, name: str) -> str:
    for key in (f"theme.{mode}.{name}", f"theme.{name}"):
        try:
            v = st.get_option(key)
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
    return _DEFAULTS[mode][name]


def colors(dark: bool | None = None) -> dict:
    mode = "dark" if (is_dark() if dark is None else dark) else "light"
    return {"mode": mode, **{k: _opt(mode, k) for k in _DEFAULTS[mode]}}


def watch() -> None:
    """Render the (zero-height) theme watcher; rerun once when the browser's theme differs from ours."""
    known = "dark" if is_dark() else "light"
    st.session_state.setdefault(_KEY, known)
    seen = _watcher(known=known, key="theme_watcher", default=None)
    if seen in ("light", "dark") and seen != st.session_state[_KEY]:
        st.session_state[_KEY] = seen
        st.rerun()
