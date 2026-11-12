"""Ask engine: Decimal-safe prompts, focus, JSON answers and self-correction. Run: python -m pytest tests"""
import json
import pathlib
import sqlite3
import sys
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from modules import ask, governance as gv  # noqa: E402
from tests.test_governance import DDL, northwind  # noqa: E402


def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.executescript("""
      INSERT INTO Categories VALUES (1, 'Beverages');
      INSERT INTO Products VALUES (1, 'Chai', NULL, 1, 18.0, 39, 10, 0);
      INSERT INTO Orders VALUES (10248, 'VINET', 5, '1997-01-04 00:00:00', '1997-02-01', '1997-01-16', 3, 32.38, 'France');
      INSERT INTO Orders VALUES (10249, 'TOMSP', 6, '1997-02-05 00:00:00', '1997-03-01', '1997-02-10', 1, 11.61, 'Germany');
      INSERT INTO "Order Details" VALUES (10248, 1, 18.0, 10, 0.0);
      INSERT INTO "Order Details" VALUES (10249, 1, 18.0, 5, 0.1);""")
    return lambda sql: pd.read_sql_query(sql, conn)


def test_decimal_and_date_samples_do_not_break_the_prompt():
    import datetime
    onto = northwind()
    p = onto["classes"]["OrderDetail"]["data_properties"]
    next(iter(p.values()))["samples"] = [Decimal("18.0000"), Decimal("0.05"), datetime.date(1997, 1, 1), b"\x00"]
    text = ask.answer_prompt("net revenue for each month in 1997", onto, "ontology", "databricks")
    assert "- 18\n" in text and "0.05" in text and "1997-01-01" in text


def test_focus_points_at_the_net_revenue_metric_and_its_tables():
    onto = northwind()
    gv.certify(onto, "metric", onto["metrics"][0]["name"], "draft")
    onto["metrics"].append({"name": "net_revenue", "base_class": "OrderDetail", "status": "certified",
                            "sql": "SUM(UnitPrice * Quantity * (1 - Discount))", "synonyms": ["sales"]})
    f = ask.focus("how much is the net revenue for each month in 1997", onto)
    assert f["metrics"][0]["name"] == "net_revenue" and f["metrics"][0]["certified"]
    assert "OrderDetail" in f["classes"]


def test_parse_answer_accepts_json_fenced_json_and_plain_sql():
    j = {"reasoning": "r", "assumptions": ["a"], "sql": "SELECT 1;"}
    assert ask.parse_answer(json.dumps(j))["sql"] == "SELECT 1"
    assert ask.parse_answer("```json\n" + json.dumps(j) + "\n```")["assumptions"] == ["a"]
    assert ask.parse_answer("Here you go:\nSELECT 2")["sql"] == "SELECT 2"


def test_the_engine_fixes_its_own_failing_query():
    onto = northwind()
    good = ('SELECT strftime(\'%Y-%m\', o.OrderDate) AS month, '
            'ROUND(SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)), 2) AS net_revenue '
            'FROM Orders o JOIN "Order Details" od ON od.OrderID = o.OrderID '
            "WHERE strftime('%Y', o.OrderDate) = '1997' GROUP BY 1 ORDER BY 1")
    calls = []

    def fake_chat(system, user, max_tokens=0):
        calls.append(user)
        if len(calls) == 1:     # wrong table name first
            return json.dumps({"reasoning": "first try", "assumptions": [], "sql": "SELECT * FROM OrderDetails"})
        assert "no such table" in user and "Previous query" in user
        return json.dumps({"reasoning": "monthly net revenue", "assumptions": ["net = after discount"], "sql": good})

    out = ask.answer("how much is the net revenue for each month in 1997", onto, "ontology", "sqlite",
                     execute=db(), chat_fn=fake_chat)
    assert out["status"] == "ok" and len(out["attempts"]) == 2 and out["attempts"][0]["error"]
    assert out["result"]["month"].tolist() == ["1997-01", "1997-02"]
    assert out["result"]["net_revenue"].tolist() == [180.0, 81.0]


def test_an_empty_result_gets_one_retry_and_writes_are_refused():
    onto = northwind()
    replies = iter([json.dumps({"sql": "SELECT * FROM Orders WHERE ShipCountry = 'france'"}),
                    json.dumps({"sql": "SELECT OrderID FROM Orders WHERE ShipCountry = 'France'"})])
    out = ask.answer("orders shipped to france", onto, "ontology", "sqlite", execute=db(),
                     chat_fn=lambda *a, **k: next(replies))
    assert out["status"] == "ok" and len(out["result"]) == 1 and len(out["attempts"]) == 2
    out = ask.answer("delete", onto, "ontology", "sqlite", execute=db(), max_attempts=1,
                     chat_fn=lambda *a, **k: '{"sql": "DELETE FROM Orders"}')
    assert out["status"] == "refused" and out["result"] is None
