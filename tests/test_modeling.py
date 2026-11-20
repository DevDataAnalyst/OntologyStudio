"""Modeling assistant (ideas from the auto-ontology MCP): re-scan drift, table filters, worklist, relationship
review with roles and default joins, filter suggestions, expression tests, review lints, change log, LLM drafts.
Run: python -m pytest tests"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import answering as an, llm_enrich, modeling as md, ontology_edit as ed, review as rv  # noqa: E402
from modules import sqlcheck as sc  # noqa: E402
from modules.ossie import from_ontology, to_ontology  # noqa: E402
from modules.schema_model import Column, DatabaseSchema, Table  # noqa: E402
from tests.test_ask import db  # noqa: E402
from tests.test_governance import northwind  # noqa: E402


def uc_northwind():
    onto = northwind()
    for c in onto["classes"].values():
        c["source_fqn"] = f"main.nw.{c['source_table']}"
    return onto


def fresh_tables(onto, drop_class=None):
    """What a re-scan would read: Orders gained Channel, lost ShipCountry, Freight became DECIMAL."""
    out = []
    for cid, c in onto["classes"].items():
        types = {str(p.get("source_column")): p.get("sql_type") for p in c["data_properties"].values()}
        t = Table(name=c["source_table"], full_name=c["source_fqn"], row_count=10,
                  columns=[Column(name=x, sql_type=types.get(x) or "STRING") for x in c["source_columns"]])
        if cid == "Order":
            t.columns = [x for x in t.columns if x.name != "ShipCountry"] + [
                Column(name="Channel", sql_type="STRING", distinct_values=["store", "web"], samples=["web"])]
            next(x for x in t.columns if x.name == "Freight").sql_type = "DECIMAL(10,2)"
        if cid != drop_class:
            out.append(t)
    return DatabaseSchema(name="rescan", source_type="unity_catalog", tables=out)


def test_rescan_reports_and_applies_drift_keeping_the_model():
    onto = uc_northwind()
    onto["classes"]["Order"]["description"] = "One row per order."
    ds = fresh_tables(onto, drop_class="Shipper")
    rep = md.drift(onto, ds)
    assert rep["changed"] == {"Order": {"new": ["Channel"], "dropped": ["ShipCountry"],
                                        "retyped": [{"column": "Freight", "was": "REAL", "now": "DECIMAL(10,2)"}]}}
    assert rep["missing"] == ["Shipper"] and rep["unchanged"] == 6
    md.apply_drift(onto, ds, rep)
    o = onto["classes"]["Order"]
    cols = {p["source_column"]: p for p in o["data_properties"].values()}
    assert "ShipCountry" not in cols and cols["Channel"]["allowed_values"] == ["store", "web"]
    assert cols["Freight"]["sql_type"] == "DECIMAL(10,2)" and o["new_columns"] == ["Channel"]
    assert o["description"] == "One row per order." and o["instance_count"] == 10
    assert md.worklist(onto)[0]["class"] == "Order" and "Channel" in md.worklist(onto)[0]["to do"]
    assert md._matches("c.sales.SALES_2024", "SALES_*, !*_LOG") and not md._matches("c.sales.sales_log", "SALES_*, !*_LOG")
    assert md._matches("c.hr.emp_x", "HR.EMP*") and not md._matches("c.ops.emp_x", "HR.EMP%")


def test_roles_and_default_joins_steer_paths_the_checker_and_ossie():
    onto = uc_northwind()
    second = ed.add_relationship(onto, "Order", "Employee", ["ShipVia"], ["EmployeeID"])
    onto["object_properties"][second]["role"] = "shipped by"
    m = md.multi_role(onto)
    assert m[0]["relationships"] == ["hasEmployee", second] and m[0]["default"] is None
    assert any(i["id"] == "lint:default:Order:Employee" for i in rv.build_queue(onto))
    md.set_default(onto, second)
    assert an.join_path(onto, "Order", "Employee")[0]["pid"] == second              # the default wins
    r = sc.validate("SELECT o.OrderID FROM main.nw.Orders o JOIN main.nw.Employees e ON e.EmployeeID = o.EmployeeID",
                    onto, "databricks")
    assert r["valid"] and "not the default join" in r["notes"][0]
    back = to_ontology(from_ontology(onto)[0])
    assert back["object_properties"][second]["role"] == "shipped by" and back["object_properties"][second]["default"]
    new = ed.update_relationship(onto, second, ["ShipVia"], ["EmployeeID"], "many-to-one")
    assert onto["object_properties"][new]["role"] == "shipped by" and onto["object_properties"][new]["default"]


def test_unlinked_ids_observed_joins_and_containment():
    onto = northwind()
    onto["classes"]["Order"]["source_columns"].append("SalesRepId")
    onto["classes"]["Order"]["data_properties"]["salesRepId"] = {"source_column": "SalesRepId", "sql_type": "INTEGER",
                                                                "range": "xsd:integer"}
    un = md.unlinked_ids(onto)
    assert [(u["class"], u["column"]) for u in un] == [("Order", "SalesRepId")] and "Employee" in un[0]["targets"]
    assert "Customer" not in un[0]["targets"]                                     # TEXT key, INTEGER column
    sql = "SELECT o.OrderID FROM Orders o JOIN Employees e ON e.EmployeeID = o.ShipVia"      # ShipVia is a shipper
    assert md.observed_joins(onto, ["SELECT 1 FROM Orders o JOIN Shippers s ON s.ShipperID = o.ShipVia"], "sqlite") == []
    obs = md.observed_joins(onto, [sql, sql], "sqlite")
    assert obs[0]["uses"] == 2 and obs[0]["join"] == "Employees.EmployeeID = Orders.ShipVia"
    pid = md.add_observed(onto, obs[0])
    p = onto["object_properties"][pid]
    assert (p["domain"], p["range"], p["join"]["from"], p["join"]["to"]) == (
        "Order", "Employee", ["Orders.ShipVia"], ["Employees.EmployeeID"])
    assert md.observed_joins(onto, [sql], "sqlite") == []
    assert md.containment(db(), onto, "Order", "ShipVia", "Employee", "sqlite") == 0.0    # no employees in the sample


def test_filter_suggestions_are_tested_and_saved_as_rules():
    onto, run = northwind(), db()
    onto["classes"]["Order"]["data_properties"]["shipCountry"]["allowed_values"] = ["France", "Germany"]
    vf = md.value_filters(onto)
    assert [f["filter"] for f in vf] == ["Orders.ShipCountry = 'France'", "Orders.ShipCountry = 'Germany'"]
    assert md.test_expression(run, onto, "Order", vf[0]["filter"], "filter", "sqlite") is None
    assert "no such column" in md.test_expression(run, onto, "Order", "SUM(Frieght)", "measure", "sqlite")
    assert md.save_rule(onto, vf[0]) == "ship_country_france" and onto["rules"][-1]["status"] == "draft"
    assert [f["filter"] for f in md.value_filters(onto)] == ["Orders.ShipCountry = 'Germany'"]


def test_lints_change_log_and_llm_drafts(monkeypatch):
    onto = northwind()
    onto["metrics"].append({"name": "broken", "base_class": "Order", "sql": "SUM(Frieght)", "status": "draft"})
    onto["metrics"].append({"name": "orphan", "base_class": "Order", "sql": "SUM(Orders.Freight) / COUNT(Islands.id)"})
    ids = {i["id"]: i for i in md.lint(onto, "sqlite")}
    assert "lint:grain" in ids and "Did you mean Freight" in ids["lint:metric:broken"]["why"]
    assert "lint:metric:orphan" not in ids                                         # unknown qualifier: left to a person
    onto["classes"]["Order"]["description"] = "One row per order placed by a customer."
    assert "Order" not in md.lint(onto, "sqlite")[0]["why"].split(". ")[0].split(", ")
    md.log_change(onto, "Renamed X", "ana")
    back = to_ontology(from_ontology(onto)[0])
    assert back["changes"][-1]["what"] == "Renamed X" and back["changes"][-1]["by"] == "ana"
    reply = {"classes": {"Customer": {"description": "One row per customer.", "synonyms": ["client"]}},
             "data_properties": {"Customer.country": {"description": "Country of the customer."}}}
    calls = []
    monkeypatch.setattr(llm_enrich, "chat", lambda system, user, **k: calls.append(user) or json.dumps(reply))
    llm_enrich.enrich(onto, classes=["Customer"])
    assert len(calls) == 1 and '"Customer"' in calls[0] and '"Order"' not in calls[0]
    assert onto["classes"]["Customer"]["description"] == "One row per customer."
    assert onto["classes"]["Order"]["description"] == "One row per order placed by a customer."
    assert "the grain" in llm_enrich.SYSTEM_PROMPT
    slipped = '{"classes": {"T": {"description": "One row per t."}}}", "rules": [],}'    # Llama-style JSON slips
    assert llm_enrich.parse_json(slipped, llm_enrich.SECTIONS) == {"classes": {"T": {"description": "One row per t."}},
                                                                 "rules": []}
