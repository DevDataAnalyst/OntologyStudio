"""
Source types offered on the first screen.

Two families: **Databricks** (Unity Catalog tables and views, read with the viewer's
permissions) and **Non-Databricks** (everything else). Only some sources are built today;
the rest are placeholders so the screen already shows where new connectors will plug in
(see plans/multi-source-cdgc-plan.md: a connector protocol + registry under modules/).
"""
from __future__ import annotations

from dataclasses import dataclass

DATABRICKS = "Databricks"
NON_DATABRICKS = "Non-Databricks"
FAMILIES = [DATABRICKS, NON_DATABRICKS]


@dataclass(frozen=True)
class Source:
    key: str            # what app.py branches on
    label: str
    family: str
    available: bool = True
    note: str = ""


SOURCES = [
    Source("Unity Catalog", "Unity Catalog tables & views", DATABRICKS,
           note="Pick up to 100 tables or views in any catalog; runs with your permissions."),
    Source("Upload file", "SQLite database or .sql script", NON_DATABRICKS,
           note="Upload a SQLite file or a DDL script in any common SQL dialect."),
    Source("SAP BusinessObjects", "SAP BusinessObjects (Universe + reports)", NON_DATABRICKS,
           note="Migrate a BO Universe and its Web Intelligence reports: extract bundle -> gap report -> "
                "Apache Ossie -> Databricks metric views, Power BI helpers and reconciliation queries."),
    Source("Snowflake", "Snowflake", NON_DATABRICKS, available=False,
           note="Planned: read INFORMATION_SCHEMA through a Snowflake connection stored as an app secret."),
    Source("PostgreSQL", "PostgreSQL", NON_DATABRICKS, available=False,
           note="Planned: read the catalog of a Postgres database (or Lakebase) through a secret."),
    Source("Informatica CDGC", "Informatica CDGC (glossary overlay)", NON_DATABRICKS, available=False,
           note="Planned: glossary terms, owners and certification from Informatica Cloud Data "
                "Governance & Catalog, layered on top of the tables."),
]


def for_family(family: str) -> list[Source]:
    return [s for s in SOURCES if s.family == family]


def get(key: str) -> Source | None:
    return next((s for s in SOURCES if s.key == key), None)


def label(key: str) -> str:
    s = get(key)
    if s is None:
        return key
    return s.label if s.available else f"{s.label} (coming soon)"
