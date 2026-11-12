"""Domains, certification, pages, learned snippets, review queue and Ossie round trip of governance.
Run: python -m pytest tests"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import governance as gv, review as rv, snippets as sn, telemetry as tm  # noqa: E402
from modules.ask import build_context  # noqa: E402
from modules.extractors import from_sql_script  # noqa: E402
from modules.ontology_builder import BuildOptions, build_ontology  # noqa: E402
from modules.ossie import from_ontology, to_ontology, validate_document  # noqa: E402
from modules import semantics  # noqa: E402

# A Northwind subset with the classic table and column names
DDL = """
CREATE TABLE Categories (CategoryID INTEGER PRIMARY KEY, CategoryName TEXT);
CREATE TABLE Customers (CustomerID TEXT PRIMARY KEY, CompanyName TEXT, Country TEXT);
CREATE TABLE Employees (EmployeeID INTEGER PRIMARY KEY, LastName TEXT);
CREATE TABLE Shippers (ShipperID INTEGER PRIMARY KEY, CompanyName TEXT);
CREATE TABLE Suppliers (SupplierID INTEGER PRIMARY KEY, CompanyName TEXT);
CREATE TABLE Products (ProductID INTEGER PRIMARY KEY, ProductName TEXT, SupplierID INTEGER REFERENCES Suppliers,
  CategoryID INTEGER REFERENCES Categories, UnitPrice REAL, UnitsInStock INTEGER, ReorderLevel INTEGER,
  Discontinued INTEGER);
CREATE TABLE Orders (OrderID INTEGER PRIMARY KEY, CustomerID TEXT REFERENCES Customers,
  EmployeeID INTEGER REFERENCES Employees, OrderDate TEXT, RequiredDate TEXT, ShippedDate TEXT,
  ShipVia INTEGER REFERENCES Shippers, Freight REAL, ShipCountry TEXT);
CREATE TABLE "Order Details" (OrderID INTEGER REFERENCES Orders, ProductID INTEGER REFERENCES Products,
  UnitPrice REAL, Quantity INTEGER, Discount REAL, PRIMARY KEY (OrderID, ProductID));
"""


def northwind():
    ds = from_sql_script(DDL, "northwind", dialect="sqlite", profile=False)
    onto = build_ontology(ds, BuildOptions(ontology_name="Northwind"))
    semantics.classify(onto)
    return gv.ensure(onto)


def learned(onto):
    recs = sn.records_from_file("northwind_query_log.csv",
                                (ROOT / "samples" / "query_logs" / "northwind_query_log.csv").read_bytes())
    sn.merge_into(onto, sn.mine(recs, onto, dialect="sqlite"))
    return onto


def test_domains_are_suggested_and_scope_shrinks_context():
    onto = northwind()
    guess = gv.suggest_domains(onto)
    assert "Sales" in guess["Order"] and "Product" in guess["Product"]
    assert "Supply chain" in guess["Supplier"] and "People" in guess["Employee"]
    assert all(m.get("domains") for m in onto["metrics"])
    scoped = gv.scope(onto, "Product")
    assert "Product" in scoped["classes"] and "Category" in scoped["classes"]
    assert "Employee" not in scoped["classes"]
    full = tm.estimate_tokens(build_context("", onto))
    small = tm.estimate_tokens(build_context("", scoped))
    assert small < full * 0.8


def test_subdomains_inherit_into_parent_scope():
    onto = northwind()
    gv.suggest_domains(onto)
    gv.upsert_domain(onto, "Logistics", parent="Supply chain")
    gv.set_class_domains(onto, "Shipper", ["Logistics"])
    assert gv.subdomains(onto, "Supply chain") == {"Supply chain", "Logistics"}
    assert "Shipper" in gv.assets_in(onto, "Supply chain")["classes"]
    assert gv.domain_path(onto, "Logistics") == "Supply chain/Logistics"
    gv.rename_domain(onto, "Logistics", "Freight")
    assert onto["classes"]["Shipper"]["domains"] == ["Freight"]


def test_snippets_are_mined_and_ranked_by_authority():
    onto = learned(northwind())
    by_text = {s["text"]: s for s in onto["snippets"]}
    net = by_text["SUM(OrderDetail.UnitPrice * OrderDetail.Quantity * (1 - OrderDetail.Discount))"]
    naive = by_text["SUM(OrderDetail.UnitPrice * OrderDetail.Quantity)"]
    assert net["name"] == "net_revenue" and net["comments"]
    assert net["authority"] > 0.8 > 0.4 > naive["authority"]
    assert "Order.ShippedDate > Order.RequiredDate" in by_text
    assert any(s["kind"] == "join" and s["text"] == "Category.CategoryID = Product.CategoryID" for s in onto["snippets"])
    assert sn.experts(onto)[0]["person"] == "chung.lee"
    # a trusted-author boost moves an author's snippets up
    before = naive["authority"]
    onto["trusted_authors"] = {"intern.joe": 0.3}
    sn.rescore(onto)
    assert next(s for s in onto["snippets"] if s["id"] == naive["id"])["authority"] > before


def test_review_queue_promotes_and_settles_conflicts():
    onto = learned(northwind())
    gv.suggest_domains(onto)
    q = rv.build_queue(onto)
    types = {i["type"] for i in q}
    assert {"popular-snippet", "conflict"} <= types
    conflict = next(i for i in q if i["type"] == "conflict" and i["name"] == "revenue")
    assert len(conflict["definitions"]) >= 2
    snip = next(i for i in q if i["type"] == "popular-snippet" and i["action"] == "metric")
    msg = rv.promote(onto, snip["snippet"], "metric")
    assert msg.startswith("Promoted to metric:")
    m = onto["metrics"][-1]
    assert m["provenance"] == "learned" and m["status"] == "draft" and m["domains"]
    # the promoted metric now waits for certification
    q2 = rv.build_queue(onto)
    assert any(i["type"] == "uncertified" and i["key"] == m["name"] for i in q2)
    gv.certify(onto, "metric", m["name"], "certified", owner="sales-data", by="admin")
    assert not any(i["type"] == "uncertified" and i["key"] == m["name"] for i in rv.build_queue(onto))
    filt = next(s for s in onto["snippets"] if s["text"] == "Order.ShippedDate > Order.RequiredDate")
    rv.promote(onto, filt["id"], "rule")
    assert onto["rules"][-1]["sql_hint"] == filt["text"]


def test_negative_feedback_and_gaps_become_page_drafts():
    onto = northwind()
    traces = [dict(tm.new_trace(q, "ana", "ontology", "Sales"), feedback="down", status="ok",
                   assets={"classes": ["Order"]}) for q in ("How many returns last month", "Return rate by product")]
    q = rv.build_queue(onto, traces)
    neg = next(i for i in q if i["type"] == "negative-feedback")
    title, body, _ = rv.draft_page(onto, neg)
    assert "returns" in body.lower()
    pid = gv.upsert_page(onto, title, body, ["Sales"], "ana", "draft", provenance="review")
    assert any(i["type"] == "uncertified" and i["key"] == pid for i in rv.build_queue(onto, traces))
    gv.upsert_page(onto, title, body, ["Sales"], "ana", "certified", page_id=pid, by="admin")
    ctx = build_context("", onto)
    assert "certified: true" in ctx and title in ctx


def test_deprecated_metrics_never_reach_the_model():
    onto = northwind()
    name = onto["metrics"][0]["name"]
    gv.certify(onto, "metric", name, "deprecated")
    assert f"name: {name}\n" not in build_context("", onto)


def test_governance_survives_an_ossie_round_trip():
    onto = learned(northwind())
    gv.suggest_domains(onto)
    gv.certify(onto, "class", "Order", "certified", owner="sales-data", by="admin")
    gv.certify(onto, "metric", onto["metrics"][0]["name"], "certified", by="admin")
    gv.upsert_page(onto, "Net vs gross revenue", "Net revenue subtracts discounts.", ["Sales"], "fin", "certified")
    doc, _ = from_ontology(onto)
    assert not [f for f in validate_document(doc).findings if f.severity == "error"]
    back = to_ontology(doc)
    assert back["classes"]["Order"]["certification"]["status"] == "certified"
    assert back["classes"]["Order"]["domains"] == onto["classes"]["Order"]["domains"]
    assert back["pages"][0]["title"] == "Net vs gross revenue"
    assert len(back["snippets"]) == len(onto["snippets"])
    assert {d["name"] for d in back["domains"]} == {d["name"] for d in onto["domains"]}
    m0 = next(m for m in back["metrics"] if m["name"] == onto["metrics"][0]["name"])
    assert m0["status"] == "certified"


def test_uc_tag_statements():
    onto = northwind()
    gv.suggest_domains(onto)
    for c in onto["classes"].values():
        c["source_fqn"] = f"northwind.silver.{c['source_table'].lower().replace(' ', '_')}"
    gv.certify(onto, "class", "Order", "certified", owner="sales-data")
    stmts = gv.uc_tag_statements(onto)
    order = next(s for s in stmts if "`orders`" in s)
    assert "'ontology_domain' = 'Sales'" in order and "'ontology_certification' = 'certified'" in order
    tags = {"northwind.silver.orders": {"ontology_domain": "Finance,Sales", "ontology_certification": "deprecated"}}
    gv.apply_uc_tags(onto, tags)
    assert onto["classes"]["Order"]["domains"] == ["Finance", "Sales"]
    assert onto["classes"]["Order"]["certification"]["status"] == "deprecated"


def test_budget_and_cost():
    t = dict(tm.new_trace("q", "ana", "ontology", None), prompt_tokens=1_000_000, completion_tokens=100_000)
    assert abs(tm.price(t, 0.5, 1.5) - 0.65) < 1e-9
    assert tm.over_budget([t], "ana", 500_000) and not tm.over_budget([t], "bob", 500_000)
    assert sn.classify_source('{"dashboard_id": "abc"}') == ("dashboard", "abc")
    assert sn.classify_source(None, "Databricks SQL Editor")[0] == "sql_editor"


def test_relearning_the_same_source_does_not_double_count():
    onto = learned(northwind())
    uses = {s["id"]: s["uses"] for s in onto["snippets"]}
    recs = sn.records_from_file("northwind_query_log.csv",
                                (ROOT / "samples" / "query_logs" / "northwind_query_log.csv").read_bytes())
    sn.merge_into(onto, sn.mine(recs, onto, dialect="sqlite"))          # same ingest key again
    assert {s["id"]: s["uses"] for s in onto["snippets"]} == uses
    sn.merge_into(onto, sn.mine(recs[:5], onto, dialect="sqlite"), ingest="file:other")
    assert sum(s["uses"] for s in onto["snippets"]) > sum(uses.values())


def test_inherited_domains_follow_the_class_but_explicit_ones_stay():
    onto = northwind()
    gv.suggest_domains(onto)
    m = next(x for x in onto["metrics"] if x["base_class"] == "Order")
    gv.set_class_domains(onto, "Order", ["Finance"])
    gv.inherit_metric_domains(onto)
    assert m["domains"] == ["Finance"]
    gv.certify(onto, "metric", m["name"], "draft", domains=["Sales"])
    gv.set_class_domains(onto, "Order", ["Logistics"])
    gv.inherit_metric_domains(onto)
    assert m["domains"] == ["Sales"]


def test_deprecated_tables_never_reach_the_model():
    onto = northwind()
    gv.certify(onto, "class", "Shipper", "deprecated")
    ctx = build_context("", onto)
    assert "Shippers" not in ctx
