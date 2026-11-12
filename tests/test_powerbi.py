"""Power BI data dictionary: types, column settings, grain, relationships, SQL->DAX, M and the starter kit.
Run: python -m pytest tests"""
import io
import pathlib
import sqlite3
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import powerbi as pb  # noqa: E402
from modules.ask import build_context  # noqa: E402
from modules.extractors import from_sqlite_file  # noqa: E402
from modules.ontology_builder import BuildOptions, build_ontology  # noqa: E402
from modules.ossie import from_ontology, to_ontology  # noqa: E402
from modules import semantics  # noqa: E402
from tests.test_governance import DDL, learned, northwind  # noqa: E402


def profiled(tmp_path):
    db = tmp_path / "nw.db"
    con = sqlite3.connect(db)
    con.executescript(DDL)
    con.executemany("INSERT INTO Customers VALUES (?,?,?)", [("ALFKI", "Alfreds", "Germany"), ("BONAP", "Bon", "France")])
    con.executemany("INSERT INTO Orders VALUES (?,?,?,?,?,?,?,?,?)",
                    [(i, "ALFKI" if i % 3 else None, 1, f"1997-0{1 + i % 9}-1{i % 9}", "1997-12-01", None, 1 + i % 3,
                      10.5 * i, "Germany") for i in range(1, 121)])
    con.commit()
    con.close()
    ds = from_sqlite_file(db.read_bytes(), "northwind")
    onto = build_ontology(ds, BuildOptions(ontology_name="Northwind"))
    semantics.classify(onto)
    return onto


def test_types_and_names():
    assert pb.friendly("CustomerID") == "Customer ID" and pb.friendly("unit_price") == "Unit Price"
    assert pb.friendly("OrderDetail") == "Order Detail"
    assert pb.pbi_type("DECIMAL(19,4)")[0] == "Fixed decimal number"
    assert pb.pbi_type("DECIMAL(10,0)")[0] == "Whole number"
    assert pb.pbi_type("TIMESTAMP")[0] == "Date/Time" and pb.pbi_type("BIGINT")[0] == "Whole number"
    t, note = pb.pbi_type("DOUBLE", "freight")
    assert t == "Fixed decimal number" and "floating point" in note
    assert pb.pbi_type("STRING", "OrderDate") == ("Date", "dates stored as text: change the type to Date in Power Query")


def test_column_settings_grain_and_storage():
    onto = northwind()
    rows = {r["Source column"]: r for r in pb.column_rows(onto, "Order")}
    assert rows["CustomerID"]["Kind"] == "Foreign key" and rows["CustomerID"]["Hide"]
    assert rows["CustomerID"]["References"] == "Customer[Customer ID]"
    assert rows["Freight"]["Summarize by"] == "Sum" and rows["Freight"]["Format"].startswith("\\$")
    assert rows["ShipCountry"]["Data category"] == "Country/Region"
    assert rows["ShipVia"]["References"] == "Shipper[Shipper ID]"
    assert pb.grain(onto, "Order") == "One row per order (Order ID)."
    assert pb.grain(onto, "OrderDetail").startswith("One row per order and product combination")
    onto["classes"]["Order"]["instance_count"] = 40_000_000
    mode, why = pb.storage_mode(onto, "Order")
    assert mode == "Import + incremental refresh" and "Order Date" in why
    assert pb.storage_mode(onto, "Customer")[0] == "Import"


def test_profile_feeds_quality_notes(tmp_path):
    onto = profiled(tmp_path)
    rows = {r["Source column"]: r for r in pb.column_rows(onto, "Order")}
    assert rows["CustomerID"]["Null %"] == 33.3 and "(Blank)" in rows["CustomerID"]["Notes"]
    assert "always empty" in rows["ShippedDate"]["Notes"]
    assert rows["OrderDate"]["Min"].startswith("1997-01")
    assert pb.date_range(onto) == (1997, 1997)
    assert "CALENDAR(DATE(1997, 1, 1), DATE(1997, 12, 31))" in pb.date_table_dax(onto)
    # the dictionary detail stays out of the text-to-SQL prompt
    assert "column_profile" not in build_context("", onto)


def test_relationships_role_playing_dates():
    onto = northwind()
    rels = pb.relationships(onto)
    dates = [r for r in rels if r["To table"] == "Date" and r["From table"] == "Order"]
    assert [r["From column"] for r in dates if r["Active"]] == ["Order Date"]
    assert all("USERELATIONSHIP" in r["Notes"] for r in dates if not r["Active"])
    assert any(r["From table"] == "Order Detail" and r["To table"] == "Product" and r["Active"] for r in rels)


def test_sql_to_dax():
    onto = learned(northwind())
    dax, _ = pb.sql_to_dax(onto, "OrderDetail", "SUM(UnitPrice * Quantity * (1 - Discount))")
    assert dax == ("SUMX('Order Detail', 'Order Detail'[Unit Price] * 'Order Detail'[Quantity] * "
                   "(1 - 'Order Detail'[Discount]))")
    assert pb.sql_to_dax(onto, "Order", "COUNT(DISTINCT CustomerID)")[0] == "DISTINCTCOUNT('Order'[Customer ID])"
    assert pb.sql_to_dax(onto, "Order", "COUNT(*)")[0] == "COUNTROWS('Order')"
    assert pb.sql_to_dax(onto, "Order", "SUM(Freight) / COUNT(1)")[0] == "DIVIDE(SUM('Order'[Freight]), COUNTROWS('Order'))"
    dax, _ = pb.sql_to_dax(onto, "OrderDetail", "SUM(CASE WHEN Product.Discontinued = 1 THEN Quantity ELSE 0 END)")
    assert dax == ("SUMX('Order Detail', SWITCH(TRUE(), RELATED('Product'[Discontinued]) = 1, "
                   "'Order Detail'[Quantity], 0))")
    assert pb.sql_to_dax(onto, "Product", "SUM(OrderDetail.Quantity)")[0] is None   # wrong direction
    ms = {m["Measure"]: m for m in pb.measures(onto, time_intelligence=True)}
    assert ms["Total Freight"]["DAX"] == "SUM('Order'[Freight])"
    assert ms["Total Freight YTD"]["DAX"] == "TOTALYTD([Total Freight], 'Date'[Date])"
    assert ms["Order Count"]["Format"] == "#,0"
    script = pb.measures_dax_query(list(ms.values()))
    assert "DEFINE" in script and "MEASURE 'Order'[Total Freight] = SUM('Order'[Freight])" in script


def test_power_query_and_starter_kit():
    onto = northwind()
    for c in onto["classes"].values():
        c["source_fqn"] = f"northwind.silver.{c['source_table'].lower().replace(' ', '_')}"
    m = pb.power_query_m(onto, "Order", "dbc-x.cloud.databricks.com", "/sql/1.0/warehouses/abc")
    assert 'Databricks.Catalogs("dbc-x.cloud.databricks.com", "/sql/1.0/warehouses/abc"' in m
    assert '{[Name = "silver", Kind = "Schema"]}' in m and '{"CustomerID", "Customer ID"}' in m
    assert '{"Freight", Currency.Type}' in m
    m2 = pb.power_query_m(onto, "Order", incremental=True)
    assert "RangeStart" in m2 and "[OrderDate] >= RangeStart" in m2
    kit = zipfile.ZipFile(io.BytesIO(pb.starter_kit(onto)))
    names = set(kit.namelist())
    assert {"data_dictionary.md", "columns.csv", "relationships.csv", "measures.dax", "date_table.dax",
            "power_query/Order.pq"} <= names
    md = kit.read("data_dictionary.md").decode()
    assert "## Order" in md and "One row per order (Order ID)." in md
    hits = pb.search(onto, "country")
    assert {h["Table"] for h in hits} >= {"Customer", "Order"}


def test_dictionary_metadata_survives_ossie(tmp_path):
    onto = profiled(tmp_path)
    onto["classes"]["Order"]["table_meta"] = {"owner": "sales-eng", "last_altered": "2026-11-01"}
    doc, _ = from_ontology(onto)
    back = to_ontology(doc)
    assert back["classes"]["Order"]["table_meta"]["owner"] == "sales-eng"
    assert back["classes"]["Order"]["column_profile"]["Freight"]["max"] == 1260.0
