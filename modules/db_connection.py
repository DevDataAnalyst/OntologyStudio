"""
On-behalf-of-user SQL Warehouse connection for Databricks Apps.

Databricks Apps forwards the viewer's OAuth token in the
`X-Forwarded-Access-Token` header when the SQL Warehouse resource is configured
for on-behalf-of-user auth, so every query runs with the *viewer's* Unity
Catalog permissions, not the app's service principal.

`make_runner()` must be called on the Streamlit script thread (it reads the
request headers). The function it returns is thread-safe — each worker thread
gets its own connection — so profiling 100 tables can run in parallel.
"""
from __future__ import annotations

import os
import threading

import pandas as pd
import streamlit as st

WAREHOUSE_ID = os.getenv("SQL_WAREHOUSE_ID")
_local = threading.local()


def get_user_token() -> str | None:
    try:
        return st.context.headers.get("X-Forwarded-Access-Token")
    except Exception:  # noqa: BLE001
        return None


def _connect(token: str):
    from databricks import sql
    from databricks.sdk.core import Config

    cfg = Config()  # DATABRICKS_HOST etc. come from the Apps runtime env
    return sql.connect(server_hostname=cfg.host,
                       http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
                       access_token=token)


def _conn(token: str):
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    if token not in conns:
        conns[token] = _connect(token)
    return conns[token]


def _execute(token: str, query: str, params=None) -> pd.DataFrame:
    with _conn(token).cursor() as cur:
        cur.execute(query, params or None)
        if not cur.description:          # DDL / statements without a result set
            return pd.DataFrame()
        cols = [c[0] for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def make_runner():
    """Return run(query, params=None) -> DataFrame, bound to the current viewer."""
    token = get_user_token()
    if not token:
        st.error("No forwarded user token found. Run inside Databricks Apps and add the SQL "
                 "Warehouse as an app resource with **on-behalf-of-user** authorization.")
        st.stop()
    if not WAREHOUSE_ID or WAREHOUSE_ID.startswith("REPLACE"):
        st.error("SQL_WAREHOUSE_ID is not set in app.yaml.")
        st.stop()

    def run(query: str, params=None) -> pd.DataFrame:
        return _execute(token, query, params)

    return run


def run_query(query: str, params=None) -> pd.DataFrame:
    """Convenience wrapper for one-off queries from the script thread."""
    return make_runner()(query, params)
