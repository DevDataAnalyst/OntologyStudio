"""SQL checker, join paths, coverage check, verified examples, glossary and readiness (ideas from the
auto-ontology MCP). Run: python -m pytest tests"""
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import answering as an, ask, sqlcheck as sc  # noqa: E402
from modules.ossie import from_ontology, to_ontology, validate_document  # noqa: E402
from tests.test_ask import db  # noqa: E402
from tests.test_governance import northwind  # noqa: E402

OD = '"Order Details"'


def check(sql, onto=None, run=None, **kw):
    return sc.validate(sql, onto or northwind(), "sqlite", run=run, **kw)


def test_checker_refuses_writes_and_names_outside_the_model():
    assert "Only SELECT" in check("DELETE FROM Orders")["errors"][0]
    assert "exactly one statement" in check("SELECT 1; SELECT 2")["errors"][0]
    r = check("SELECT * FROM OrderDetails")
    assert not r["valid"] and "Did you mean Order Details" in r["errors"][0]
    assert "Did you mean OrderDate" in check("SELECT o.OrderDat FROM Orders o")["errors"][0]
    assert "belongs to Order Details" in check("SELECT p.ProductName FROM Products p WHERE od.Discount > 0")["errors"][0]
    r = check("SELECT o.OrderID FROM Orders o JOIN Employees e ON e.EmployeeID = o.ShipVia")
    assert "not how the model joins" in r["errors"][0]
    ok = check(f"SELECT o.ShipCountry, SUM(od.Quantity) AS qty FROM Orders o JOIN {OD} od ON od.OrderID = o.OrderID "
               "GROUP BY o.ShipCountry")
    assert ok["valid"] and not ok["warnings"] and ok["grounding"]["classes"] == ["Order", "OrderDetail"]


def test_checker_warns_on_fan_out_near_miss_values_scale_and_integer_division():
    run = db()
    fan = check(f"SELECT o.ShipCountry, SUM(o.Freight) FROM Orders o JOIN {OD} od ON od.OrderID = o.OrderID "
                "GROUP BY o.ShipCountry")
    assert any(w.startswith("Fan-out") and "Orders.Freight" in w for w in fan["warnings"])
    near = check("SELECT OrderID FROM Orders WHERE ShipCountry = 'france'", run=run)
    assert "Close real values: 'France'" in near["warnings"][0] and near["probes"] >= 1
    pct = check(f"SELECT od.OrderID FROM {OD} od WHERE od.Discount > 10", run=run)
    assert "Did you mean 0.1?" in pct["warnings"][0]
    assert "Integer division" in check(f"SELECT SUM(od.Quantity) / COUNT(*) FROM {OD} od")["warnings"][0]
    assert check("SELECT OrderID FROM Orders WHERE 1 = 0")["errors"][0].startswith("WHERE 1 = 0 is never true")
    bad = check("SELECT OrderID FROM Orders WHERE ShipCountry = 'France'", run=lambda sql: (_ for _ in ()).throw(
        RuntimeError("TABLE_OR_VIEW_NOT_FOUND")) if "LIMIT 0" in sql else run(sql))
    assert "The database rejected the query" in bad["errors"][0]


def test_unity_catalog_three_part_names():
    onto = northwind()
    for c in onto["classes"].values():
        c["source_fqn"] = f"main.nw.{c['source_table']}"
    cat = sc.catalog(onto)
    assert cat["three"] and "main.nw.orders" in cat["tables"]
    r = sc.validate("SELECT o.ShipCountry, COUNT(*) FROM main.nw.Orders o JOIN main.nw.Customers c "
                    "ON o.CustomerID = c.CustomerID GROUP BY 1", onto, "databricks")
    assert r["valid"] and not r["warnings"], r
    assert sc.validate("SELECT ShipCountry FROM Orders", onto, "databricks")["valid"]      # short names resolve
    assert "Did you mean" in sc.validate("SELECT * FROM main.nw.Order", onto, "databricks")["errors"][0]


def test_join_paths_connect_classes_and_warn_about_fan_out():
    onto = northwind()
    v = an.path_view(onto, "Product", "Customer", "sqlite")
    assert [h["to"] for h in v["hops"]] == ["OrderDetail", "Order", "Customer"]
    assert v["sql"].splitlines()[0] == "FROM Products AS products"
    assert 'JOIN "Order Details" AS order_details ON order_details.ProductID = products.ProductID' in v["sql"]
    assert any("one-to-many" in n for n in v["notes"])
    onto["classes"]["Island"] = {"source_table": "island", "data_properties": {}}
    hops, unreachable = an.connect(onto, ["Order", "Island", "Customer"])
    assert unreachable == ["Island"] and [h["to"] for h in hops] == ["Customer"]
    assert an.join_path(onto, "Order", "Order") == [] and an.join_path(onto, "Order", "Island") is None


def test_coverage_check_finds_terms_values_and_gaps():
    onto = northwind()
    an.set_glossary(onto, [{"name": "MRR", "definition": "freight"}, {"name": "", "definition": "ignored"}])
    c = an.check_answerable("q", onto, entities=["orders", "Chai", "France", "MRR", "net revenue"], run=db(),
                            dialect="sqlite")
    state = {e["entity"]: e["state"] for e in c["entities"]}
    assert state == {"orders": "covered", "Chai": "covered_by_value", "France": "covered_by_value",
                     "MRR": "covered_via_glossary", "net revenue": "not_covered"}
    assert c["entities"][0]["matches"][0]["name"] == "Order"              # the tight match wins
    assert c["verdict"] == "partially_answerable" and "net revenue" in c["next_step"]
    assert an.check_answerable("q", onto, entities=["weather"])["verdict"] == "not_answerable"
    assert an.extract_entities("How many orders shipped to France in 1997?", onto) == ["orders", "shipped", "France"]
    fake = lambda system, user, max_tokens=0: 'Sure: ["net revenue", "Chai"]'  # noqa: E731
    assert an.extract_entities("net revenue of Chai", onto, fake) == ["net revenue", "Chai"]


def test_examples_and_glossary_travel_through_ossie():
    onto = northwind()
    an.save_example(onto, "Freight by country", "SELECT ShipCountry, SUM(Freight) FROM Orders GROUP BY 1;")
    an.save_example(onto, "freight by country", "SELECT ShipCountry, SUM(Freight) AS f FROM Orders GROUP BY 1")
    assert len(onto["examples"]) == 1 and onto["examples"][0]["sql"].endswith("AS f FROM Orders GROUP BY 1")
    an.set_glossary(onto, [{"name": "MRR", "definition": "monthly recurring revenue"}])
    doc, _ = from_ontology(onto)
    assert doc["ai_context"]["examples"] == ["freight by country"]
    assert not [f for f in validate_document(copy.deepcopy(doc)).findings if f.severity == "error"]
    back = to_ontology(doc)
    assert back["examples"][0]["question"] == "freight by country" and back["glossary"][0]["name"] == "MRR"
    again, _ = from_ontology(back)
    assert again["ai_context"]["examples"] == ["freight by country"]          # not duplicated on a round trip
    assert an.similar_examples(back, "total freight for each country")[0]["question"] == "freight by country"
    ctx = an.context_for("freight per country and product", back, ["Order", "Product"], "sqlite")
    assert ctx["verified_examples"] and ctx["glossary"] == {"MRR": "monthly recurring revenue"}
    assert "JOIN Products AS products" in ctx["join_path"]


def test_ask_sends_checker_problems_back_and_accepts_a_kept_query():
    onto = northwind()
    bad = "SELECT p.ProductName, c.CategoryName FROM Products p JOIN Categories c ON c.CategoryID = p.SupplierID"
    good = "SELECT p.ProductName, c.CategoryName FROM Products p JOIN Categories c ON c.CategoryID = p.CategoryID"
    replies = iter([json.dumps({"sql": bad}), json.dumps({"sql": good})])
    out = ask.answer("products with their category", onto, "ontology", "sqlite", execute=db(),
                     chat_fn=lambda *a, **k: next(replies))
    assert out["status"] == "ok" and len(out["attempts"]) == 2
    assert "not how the model joins" in out["attempts"][0]["error"] and out["attempts"][0]["rows"] is None
    fan = (f"SELECT o.ShipCountry, SUM(o.Freight) AS f FROM Orders o JOIN {OD} od ON od.OrderID = o.OrderID "
           "GROUP BY o.ShipCountry")
    runs, base = [], db()
    out = ask.answer("freight by country of ordered lines", onto, "ontology", "sqlite",
                     execute=lambda sql: runs.append(sql) or base(sql), chat_fn=lambda *a, **k: json.dumps({"sql": fan}))
    assert out["status"] == "ok" and len(out["attempts"]) == 2 and "Fan-out" in out["attempts"][0]["error"]
    assert runs.count(fan) == 1                                               # the kept query is not run twice
    prompt = ask.answer_prompt("freight for each product and customer", onto, "ontology", "sqlite")
    assert "JOIN PATH" in prompt and "hop by hop" in prompt


def test_lookup_values_capped_runs_and_readiness():
    onto, run = northwind(), db()
    assert sc.lookup_values(run, onto, "Order", "ShipCountry", "fran", dialect="sqlite")["matches"] == [
        {"value": "France", "rows": 1}]
    top = sc.lookup_values(run, onto, "Orders", "ShipCountry", dialect="sqlite")
    assert {v["value"] for v in top["top_values"]} == {"France", "Germany"} and not top["more_values_exist"]
    assert sc.lookup_values(run, onto, "Order", "ShipCountry", "Frnace", dialect="sqlite")["close_values"] == ["France"]
    df, cut = sc.run_capped(run, "SELECT * FROM Orders", "sqlite", cap=1)
    assert len(df) == 1 and cut
    assert an.readiness(onto, None)["verdict"] == "no live data"
    assert an.readiness(onto, "sqlite")["verdict"] == "partly described"
    for c in onto["classes"].values():
        c["description"] = "x"
    assert an.readiness(onto, "sqlite")["verdict"] == "nothing certified"
