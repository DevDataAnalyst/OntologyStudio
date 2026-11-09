"""
Source-neutral relational schema model.

Every extractor (SQLite file, SQL DDL script, Unity Catalog schema) produces
a `DatabaseSchema`; the ontology builder only ever consumes this model, so a
new source can be added without touching the ontology logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Column:
    name: str
    sql_type: str = ""
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: str | None = None
    comment: str | None = None
    # Filled only when the source has data to profile
    distinct_values: list | None = None
    distinct_count: int | None = None
    samples: list | None = None
    non_null_count: int | None = None


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    name: str | None = None


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_sets: list[list[str]] = field(default_factory=list)
    comment: str | None = None
    row_count: int | None = None
    is_view: bool = False
    full_name: str | None = None      # catalog.schema.table for Unity Catalog sources
    table_type: str | None = None     # MANAGED, EXTERNAL, VIEW, METRIC_VIEW ...

    def column(self, name: str) -> Column | None:
        lname = name.lower()
        return next((c for c in self.columns if c.name.lower() == lname), None)


@dataclass
class DatabaseSchema:
    name: str
    source_type: str          # sqlite | sql_script | unity_catalog
    dialect: str | None = None
    tables: list[Table] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def table(self, name: str) -> Table | None:
        raw = name.lower().replace("`", "").replace('"', "").replace("[", "").replace("]", "")
        exact = next((t for t in self.tables
                      if (t.full_name or "").lower() == raw or t.name.lower() == raw), None)
        if exact:
            return exact
        short = raw.split(".")[-1]
        return next((t for t in self.tables if t.name.lower() == short
                     or (t.full_name or "").lower().endswith("." + short)), None)

    def finalize(self) -> "DatabaseSchema":
        """Normalise PK/unique flags so every extractor ends up consistent."""
        for t in self.tables:
            if not t.primary_key:
                t.primary_key = [c.name for c in t.columns if c.primary_key]
            for c in t.columns:
                if c.name in t.primary_key:
                    c.primary_key = True
                    c.nullable = False
                    if len(t.primary_key) == 1:
                        c.unique = True
            for us in t.unique_sets:
                if len(us) == 1 and (col := t.column(us[0])):
                    col.unique = True
            for fk in t.foreign_keys:
                if not fk.ref_columns and (ref := self.table(fk.ref_table)):
                    fk.ref_columns = list(ref.primary_key)
                if not self.table(fk.ref_table):
                    self.warnings.append(
                        f"{t.name}: foreign key references unknown table '{fk.ref_table}'"
                    )
        return self
