"""⚙️ Settings: how this app is configured (app.yaml), Ask's prices and budgets, and warehouse spend."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from .. import governance_ui as gui, store, telemetry as tm
from .common import ON_DATABRICKS, uc_runner


def render() -> None:
    st.title("Settings")
    st.markdown("#### Configuration")
    wid = os.getenv("SQL_WAREHOUSE_ID", "")
    rows = [
        ("Running in Databricks Apps", "yes" if ON_DATABRICKS else "no (local)", ""),
        ("SQL warehouse", wid or "not set", "SQL_WAREHOUSE_ID (app resource, on-behalf-of-user)"),
        ("Foundation Model endpoint", os.getenv("FM_ENDPOINT_NAME", "") or "not set", "FM_ENDPOINT_NAME"),
        ("Version store", store.root() if store.enabled() else "off", "ONTOLOGY_STORE (a /Volumes/... path)"),
        ("Trace table", tm.TELEMETRY_TABLE or "off (traces kept for this session only)", "TELEMETRY_TABLE"),
        ("Default catalog / schema", f"{os.getenv('TARGET_CATALOG', '') or '–'} / {os.getenv('TARGET_SCHEMA', '') or '–'}",
         "TARGET_CATALOG, TARGET_SCHEMA"),
    ]
    st.dataframe(pd.DataFrame(rows, columns=["Setting", "Value", "Where to change it (app.yaml)"]),
                 hide_index=True, width="stretch")
    st.caption("Light and dark themes: the app menu (⋮ → Settings) switches between System, Light and Dark. "
               "**Advanced mode** in the sidebar shows expert options: IRIs, discovery thresholds and the studio "
               "model YAML.")
    st.markdown("#### Ask: prices, budgets and stored traces")
    gui.cost_settings(uc_runner, ON_DATABRICKS)
