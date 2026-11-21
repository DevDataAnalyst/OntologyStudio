"""Read-only, paginated previews of tables in an open model."""
from __future__ import annotations

import pandas as pd


MAX_PAGE_SIZE = 500


def table_reference(model_class: dict, source_kind: str) -> str:
    """Return a safely quoted source table reference for the active live connection."""
    if source_kind == "uc":
        name = str(model_class.get("source_fqn") or "")
        parts = name.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise ValueError("This class does not have a Unity Catalog catalog.schema.table source.")
        return ".".join(f"`{part.replace('`', '``')}`" for part in parts)
    if source_kind == "sqlite":
        name = str(model_class.get("source_table") or "")
        if not name:
            raise ValueError("This class does not have a SQLite table source.")
        return '"' + name.replace('"', '""') + '"'
    raise ValueError("A live Unity Catalog or SQLite connection is required to preview table data.")


def page_query(reference: str, page: int, page_size: int) -> str:
    """Build a bounded read-only query; fetch one extra row to detect a following page."""
    if page < 1:
        raise ValueError("Page number must be at least 1.")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"Rows per page must be between 1 and {MAX_PAGE_SIZE}.")
    offset = (page - 1) * page_size
    return f"SELECT * FROM {reference} LIMIT {page_size + 1} OFFSET {offset}"


def fetch_page(run, reference: str, page: int, page_size: int) -> tuple[pd.DataFrame, bool]:
    """Read one bounded page and return its rows and whether another page exists."""
    rows = run(page_query(reference, page, page_size))
    has_next = len(rows) > page_size
    return rows.iloc[:page_size].reset_index(drop=True), has_next
