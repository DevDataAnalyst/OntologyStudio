"""Editing rules of the YAML form editor (modules/ossie_form.py)."""
import copy
from pathlib import Path

import pandas as pd
import yaml

from modules import ossie
from modules import ossie_form as fe

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "ossie" / "retail_star.ossie.yaml"


def sample():
    return yaml.safe_load(SAMPLE.read_text())


def test_unchanged_tables_round_trip_exactly():
    doc = sample()
    before = copy.deepcopy(doc)
    fe.apply_datasets(doc, fe.dataset_table(doc))
    for ds in doc["datasets"]:
        fe.apply_fields(doc, ds["name"], fe.field_table(ds))
        if ds.get("metrics"):
            fe.apply_metrics(doc, ds["name"], fe.metric_table(doc, ds["name"]))
    fe.apply_relationships(doc, fe.relationship_table(doc))
    fe.apply_metrics(doc, fe.MODEL_SCOPE, fe.metric_table(doc, fe.MODEL_SCOPE))
    assert doc == before
    assert ossie.validate_text(ossie.dump_yaml(fe.tidy(doc)), "x.yaml").ok


def test_field_rename_follows_into_keys_and_relationships():
    doc = sample()
    ds = doc["datasets"][0]
    pk = ds["primary_key"][0]
    df = fe.field_table(ds)
    df.loc[df["name"] == pk, "name"] = "customer_id"
    notes = fe.apply_fields(doc, ds["name"], df)
    assert doc["datasets"][0]["primary_key"] == ["customer_id"]
    to_cols = [c for r in doc.get("relationships", []) if r["to"] == ds["name"] for c in r["to_columns"]]
    assert pk not in to_cols and "customer_id" in to_cols
    assert any("Renamed field" in n for n in notes)
    assert ossie.validate_text(ossie.dump_yaml(doc), "x.yaml").ok


def test_dataset_rename_follows_into_relationships_and_new_row_added():
    doc = sample()
    df = fe.dataset_table(doc)
    old = df.loc[0, "name"]
    df.loc[0, "name"] = "Client"
    df = pd.concat([df, pd.DataFrame([{"name": "Promo", "source": "main.sales.promo", "primary_key": "promo_id"}])],
                   ignore_index=True)
    fe.apply_datasets(doc, df)
    names = [d["name"] for d in doc["datasets"]]
    assert "Client" in names and old not in names and names[-1] == "Promo"
    assert doc["datasets"][-1]["primary_key"] == ["promo_id"]
    assert all(old not in (r["from"], r["to"]) for r in doc["relationships"])


def test_new_field_metric_and_relationship_from_blank_model():
    doc = fe.new_model()
    fe.apply_model_info(doc, "shop", "Online shop", "Amounts in CAD", "store, webshop", "Revenue last month?")
    assert doc["ai_context"] == {"instructions": "Amounts in CAD", "synonyms": ["store", "webshop"],
                                 "examples": ["Revenue last month?"]}
    fe.apply_datasets(doc, pd.DataFrame([
        {"name": "orders", "source": "main.shop.orders", "primary_key": "order_id"},
        {"name": "customers", "source": "main.shop.customers", "primary_key": "id"}]))
    fe.apply_fields(doc, "orders", pd.DataFrame([
        {"name": "order_id", "expression": "order_id", "datatype": "Integer"},
        {"name": "customer_id", "expression": "customer_id", "datatype": "Integer"},
        {"name": "order_date", "expression": "order_date", "datatype": "Date", "is_time": True},
        {"name": "amount", "expression": "amount", "datatype": "Decimal", "description": "Order total"}]))
    fe.apply_fields(doc, "customers", pd.DataFrame([{"name": "id", "expression": "id", "datatype": "Integer"}]))
    fe.apply_relationships(doc, pd.DataFrame([{"from": "orders", "from_columns": "customer_id",
                                               "to": "customers", "to_columns": "id"}]))
    fe.apply_metrics(doc, fe.MODEL_SCOPE, pd.DataFrame([{"name": "revenue", "expression": "SUM(orders.amount)",
                                                         "datatype": "Decimal", "synonyms": "sales"}]))
    order_date = doc["datasets"][0]["fields"][2]
    assert "dimension" not in order_date          # time is the default for Date: nothing written
    assert doc["relationships"][0]["name"] == "orders_to_customers"
    rep = ossie.validate_text(ossie.dump_yaml(fe.tidy(doc)), "shop.yaml")
    assert rep.ok, [f.message for f in rep.errors]
    onto = ossie.to_ontology(fe.tidy(doc), "shop.yaml")
    assert len(onto["classes"]) == 2


def test_dialect_only_expression_is_edited_in_place():
    f = {"name": "x", "expression": {"dialects": [{"dialect": "TSQL", "expression": "[X]"},
                                                  {"dialect": "DAX", "expression": "T[X]"}]}}
    assert fe.expr_get(f) == ("[X]", "TSQL")
    fe.expr_set(f, "[Y]")
    assert f["expression"]["dialects"][0]["expression"] == "[Y]" and "sql" not in f["expression"]


def test_ai_context_keeps_string_form_and_extra_keys():
    o = {"ai_context": "just text"}
    fe.ctx_set(o, "new text")
    assert o["ai_context"] == "new text"
    o = {"ai_context": {"instructions": "a", "custom": 1}}
    fe.ctx_set(o, "", [], [])
    assert o["ai_context"] == {"custom": 1}
    o = {"ai_context": "x"}
    fe.ctx_set(o, "")
    assert "ai_context" not in o
