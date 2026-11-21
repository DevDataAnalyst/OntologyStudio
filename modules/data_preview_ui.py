"""Streamlit UI for the read-only, paginated data preview."""
from __future__ import annotations

import streamlit as st

from . import data_preview
from .workbench_ui import make_run

ss = st.session_state


def _reset_page() -> None:
    ss.data_preview_page = 1


def render(onto: dict, runner: dict, uc_runner) -> None:
    source_kind = runner.get("kind")
    run = make_run(runner, uc_runner)
    if run is None:
        st.info("Open a model from Unity Catalog or a SQLite database to preview its table data.")
        return

    classes = onto.get("classes") or {}
    if source_kind == "uc":
        tables = {name: model_class for name, model_class in classes.items()
                  if str(model_class.get("source_fqn") or "").count(".") == 2}
    else:
        tables = {name: model_class for name, model_class in classes.items()
                  if model_class.get("source_table")}
    if not tables:
        st.info("No source tables are available for this model.")
        return

    table_options = list(tables)
    table_signature = tuple((name, tables[name].get("source_fqn"), tables[name].get("source_table"))
                            for name in table_options)
    if ss.get("_data_preview_model") != table_signature:
        ss["_data_preview_model"] = table_signature
        ss.data_preview_page = 1
        ss.pop("data_preview_table", None)
    ss.setdefault("data_preview_page", 1)

    st.caption("Browse table rows without editing data. Results are read-only and loaded one page at a time.")
    table = st.selectbox(
        "Table",
        table_options,
        format_func=lambda name: str(tables[name].get("source_fqn") or tables[name].get("source_table") or name),
        key="data_preview_table",
        on_change=_reset_page,
    )
    page_size = st.number_input(
        "Rows per page",
        min_value=10,
        max_value=data_preview.MAX_PAGE_SIZE,
        value=50,
        step=10,
        key="data_preview_page_size",
        on_change=_reset_page,
    )

    try:
        reference = data_preview.table_reference(tables[table], source_kind)
        rows, has_next = data_preview.fetch_page(
            run, reference, ss.data_preview_page, int(page_size))
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not preview {table}: {e}")
        return

    previous, status, following = st.columns([1, 2, 1])
    if previous.button("← Previous", disabled=ss.data_preview_page <= 1, use_container_width=True,
                       key="data_preview_previous"):
        ss.data_preview_page -= 1
        st.rerun()
    status.caption(f"Page {ss.data_preview_page} · {len(rows)} row(s)")
    if following.button("Next →", disabled=not has_next, use_container_width=True,
                        key="data_preview_next"):
        ss.data_preview_page += 1
        st.rerun()

    if rows.empty:
        st.info("This table has no rows on the selected page.")
    else:
        st.dataframe(rows, hide_index=True, width="stretch")
