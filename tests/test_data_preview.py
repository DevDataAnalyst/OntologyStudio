import sqlite3

import pandas as pd
import pytest

from modules.data_preview import fetch_page, page_query, table_reference


def test_table_reference_quotes_sqlite_and_unity_catalog_names():
    assert table_reference({"source_table": 'sales "daily"'}, "sqlite") == '"sales ""daily"""'
    assert table_reference({"source_fqn": "main.sales.order`lines"}, "uc") == \
        "`main`.`sales`.`order``lines`"


def test_table_reference_requires_a_live_source_table():
    with pytest.raises(ValueError, match="Unity Catalog"):
        table_reference({"source_fqn": "sales.orders"}, "uc")
    with pytest.raises(ValueError, match="SQLite"):
        table_reference({}, "sqlite")
    with pytest.raises(ValueError, match="live"):
        table_reference({"source_table": "orders"}, "none")


def test_page_query_is_bounded_and_validates_pagination():
    assert page_query('"orders"', page=3, page_size=20) == \
        'SELECT * FROM "orders" LIMIT 21 OFFSET 40'
    with pytest.raises(ValueError, match="Page number"):
        page_query('"orders"', page=0, page_size=20)
    with pytest.raises(ValueError, match="Rows per page"):
        page_query('"orders"', page=1, page_size=501)


def test_fetch_page_trims_lookahead_and_reports_next_page():
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE "order details" (id INTEGER, item TEXT)')
    conn.executemany('INSERT INTO "order details" VALUES (?, ?)',
                     [(1, "first"), (2, "second"), (3, "third")])

    def run(sql):
        return pd.read_sql_query(sql, conn)

    first, has_next = fetch_page(run, '"order details"', page=1, page_size=2)
    second, has_next_second = fetch_page(run, '"order details"', page=2, page_size=2)
    conn.close()

    assert first["id"].tolist() == [1, 2]
    assert has_next
    assert second["id"].tolist() == [3]
    assert not has_next_second
