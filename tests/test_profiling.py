"""Profiling rules from NVIDIA Auto Ontology: personal data is tagged and its values never kept, text dates get
their notation, samples are the most common short values, FK columns to tables outside the model stay, tables
sharing a key are joined one-to-one (unless the overlap proves nothing), name-only descriptions count as missing,
and profiling stops when the warehouse stops answering. Run: python -m pytest tests"""
import copy
import pathlib
import sqlite3
import sys
from datetime import date

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import ask, discovery, llm_enrich, profiling as pf, semantics, uc  # noqa: E402
from modules.extractors import from_sql_script  # noqa: E402
from modules.ontology_builder import BuildOptions, build_ontology, refresh_index  # noqa: E402
from modules.ossie import from_ontology, to_ontology, validate_document  # noqa: E402
from modules.schema_model import Column, DatabaseSchema, Table  # noqa: E402

NOTE = "Prefers delivery after six in the evening, call first"         # longer than 30 characters
SCRIPT = f"""
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT, email TEXT, gender TEXT, city TEXT,
                        signup TEXT, note TEXT);
INSERT INTO customers VALUES (101, 'Ana Ruiz', 'ana@x.com', 'F', 'Paris', '07/13/2021', '{NOTE}'),
  (102, 'Bo Lin', 'bo@x.com', 'M', 'Lyon', '01/02/2022', '{NOTE}'), (103, 'Cy Doe', 'cy@x.com', 'M', 'Paris', '03/04/2022', '{NOTE}'),
  (104, 'Di Wu', 'di@x.com', 'F', 'Nice', '11/30/2022', '{NOTE}'), (105, 'Ed Kim', 'ed@x.com', 'M', 'Paris', '02/01/2023', '{NOTE}'),
  (106, 'Flo Ng', 'flo@x.com', 'F', 'Lyon', '12/12/2023', '{NOTE}');
CREATE TABLE customer_scores (customer_id INTEGER PRIMARY KEY, score INTEGER);
INSERT INTO customer_scores VALUES (101, 7), (102, 9), (103, 4);
CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(customer_id),
                     shipper_id INTEGER REFERENCES shippers(shipper_id), amount REAL);
INSERT INTO orders VALUES (5001, 101, 1, 10.5), (5002, 102, 2, 20.0), (5003, 101, 1, 7.25);
CREATE TABLE products (product_code TEXT PRIMARY KEY, title TEXT);
INSERT INTO products VALUES ('P-1001', 'Chai'), ('P-1002', 'Chang'), ('P-1003', 'Syrup'), ('P-1004', 'Tofu');
CREATE TABLE product_specs (product_code TEXT PRIMARY KEY, weight_g INTEGER);
INSERT INTO product_specs VALUES ('P-1001', 500), ('P-1002', 750);
CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region TEXT);
INSERT INTO regions VALUES (1, 'North'), (2, 'South'), (3, 'East'), (4, 'West');
CREATE TABLE region_targets (region_id INTEGER PRIMARY KEY, target REAL);
INSERT INTO region_targets VALUES (1, 100), (2, 200);
"""


def model():
    """Profile the script in SQLite, discover joins on the data, build and classify, like the app does."""
    ds = from_sql_script(SCRIPT, "shop", dialect="sqlite", profile=True, enum_max=2)
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCRIPT)
    sugg, _ = discovery.discover(ds, run_sql=lambda sql: pd.read_sql_query(sql, conn),
                                 table_ref=lambda t: f'"{t.name}"', quote=lambda c: f'"{c}"')
    onto = build_ontology(ds, BuildOptions(ontology_name="Shop"))
    onto["suggested_relationships"] = sugg
    return semantics.classify(onto), ds


def test_personal_data_rules_follow_auto_ontology():
    for table, col, cat in [("events", "email", "email_address"), ("events", "home_phone", "phone_number"),
                            ("events", "customer_ssn", "government_id"), ("people", "fullname", "person_name"),
                            ("customers", "postaladdressline1", "postal_address"), ("customers", "id", "identifier"),
                            ("employees", "salary", "financial"), ("Employees", "BirthDate", "date_of_birth"),
                            ("Employees", "HomePhone", "phone_number"), ("main.nw.Customers", "PostalCode",
                                                                         "postal_address"),
                            ("silver_customers", "Address", "postal_address"), ("dim_customer", "first_name",
                                                                                 "person_name")]:
        assert pf.pii_category(table, col) == cat, (table, col)
    for col in ["email_domain", "no_email", "email_address_hash", "customer_id_hash", "store_id", "username",
                "company_name", "ContactName", "City", "productname", "has_first_name", "average_age"]:
        assert pf.pii_category("customers", col) is None, col
    assert pf.pii_category("customer_orders", "address") is None        # not a table of people
    assert pf.pii_category("Orders", "CustomerID") is None and pf.pii_category("products", "name") is None


def test_date_formats_are_inferred_only_when_every_value_agrees():
    cases = [(["2012-08-24", "1990-01-01"], "YYYY-MM-DD"), (["2012-08-24T15:04:05"], "YYYY-MM-DDTHH:MM:SS"),
             (["20120824", "19900101"], "YYYYMMDD"), (["03/04/2011", "13/07/2011"], "DD/MM/YYYY"),
             (["04/03/2011", "07/13/2011"], "MM/DD/YYYY"), (["03/04/2011", "05/06/2011"], None),
             (["201208", "201301"], "YYYYMM"), (["950324", "970213"], "YYMMDD"), (["2012-08-24", "24/08/2012"], None),
             (["4/4/20", "12/25/20"], "M/D/YY"), (["2010-07-19 19:39:07.0", "2010-09-15 21:08:26.123"],
                                                  "YYYY-MM-DD HH:MM:SS.fff"), (["9503-01-01"], None),
             (["2024-07", "2024-11"], "YYYY-MM"), ([None, "", "not a date"], None), ([], None),
             ([date(2012, 8, 24)], "YYYY-MM-DD")]
    for values, expected in cases:
        assert pf.infer_date_format(values) == expected, values


def test_samples_keep_the_most_common_short_values_and_errors_are_sorted():
    assert pf.keep_samples(["a", "b", "b", "x" * 31, "c", "c", "c", None, b"z"]) == ["c", "b", "a"]
    assert pf.keep_samples([1, 2, 2, 3.5]) == [2, 1, 3.5]
    assert pf.transient(TimeoutError()) and pf.transient(RuntimeError("Error during request to server: timed out"))
    assert not pf.transient(RuntimeError("[INSUFFICIENT_PERMISSIONS] User does not have SELECT on table"))


def test_sqlite_profile_withholds_personal_values_and_reads_text_dates():
    onto, ds = model()
    cust = onto["classes"]["Customer"]
    props = {p["source_column"]: p for p in cust["data_properties"].values()}
    assert props["email"]["pii"] == "email_address" and "samples" not in props["email"]
    assert props["name"]["pii"] == "person_name" and props["gender"]["pii"] == "personal_attribute"
    assert "allowed_values" not in props["gender"] and not onto["value_meanings"]       # F/M never stored
    assert props["city"]["samples"] == ["Paris", "Lyon", "Nice"] and "pii" not in props["city"]
    assert props["signup"]["date_format"] == "MM/DD/YYYY"
    assert "samples" not in props["note"]                                               # prose is no sample
    assert "min" not in cust["column_profile"]["customer_id"] and cust["column_profile"]["customer_id"]["distinct"] == 6
    raw = ds.table("customers").column("email")
    assert raw.distinct_count == 6 and raw.samples is None and raw.distinct_values is None
    ctx = ask.ontology_context(onto)
    assert "ana@x.com" not in ctx and "Ana Ruiz" not in ctx and "pii: email_address" in ctx
    assert "date_format: MM/DD/YYYY" in ctx


def test_fk_to_a_table_outside_the_model_stays_a_key_column():
    onto, _ = model()
    order = onto["classes"]["Order"]["data_properties"]
    assert order["shipperId"]["references"] == "shippers.shipper_id"
    assert order["shipperId"]["semantic_role"] == "key"
    assert "customerId" not in order                                     # modeled FK: a relationship instead
    assert not any("shipper" in m["sql"].lower() for m in onto["metrics"])
    assert "shipper_id" in ask.baseline_context(onto)


def test_tables_sharing_a_key_join_one_to_one_unless_the_overlap_proves_nothing():
    onto, _ = model()
    rels = {(p["domain"], p["range"]): p for p in onto["object_properties"].values() if p.get("join")}
    spec = rels[("ProductSpec", "Product")]
    assert spec["cardinality"] == "one-to-one" and spec["join"]["from"] == ["product_specs.product_code"]
    assert spec["confidence"] >= 0.75 and spec["provenance"] == "discovered"
    sugg = {(s["from_table"], s["to_table"]): s for s in onto["suggested_relationships"]}
    assert ("region_targets", "regions") in sugg and ("region_targets", "regions") not in {
        (j["from"][0].split(".")[0], j["to"][0].split(".")[0]) for j in (p["join"] for p in rels.values())}
    assert "not counted as proof" in " ".join(sugg[("region_targets", "regions")]["evidence"])
    assert ("customer_scores", "customers") in sugg                       # personal key: values withheld, so ask
    def keyed(name, rows):
        return Table(name=name, primary_key=["customer_code"], row_count=rows,
                     columns=[Column("customer_code", "STRING", primary_key=True, samples=["C-0001"])])
    copies = DatabaseSchema(name="x", source_type="sqlite", tables=[keyed("customers", 9), keyed("customers_2024", 8),
                                                                   keyed("stg_customers", 9), keyed("customer_ext", 5)])
    assert [(c["from_table"], c["to_table"]) for c in discovery.name_candidates(copies).values()] == [
        ("customer_ext", "customers")]                                  # copies of a table are not its parts
    assert discovery.ambiguous(Column("ProductName", samples=["Chai"]))
    assert not discovery.ambiguous(Column("customer_id", distinct_count=500))
    assert discovery.ambiguous(Column("customer_id", distinct_count=40))


def test_protect_keeps_an_explicit_no_and_runs_on_every_refresh():
    onto, _ = model()
    p = next(p for p in onto["classes"]["Customer"]["data_properties"].values() if p["source_column"] == "email")
    p["samples"] = ["ana@x.com"]
    refresh_index(onto)
    assert "samples" not in p
    p["pii"], p["samples"] = False, ["ana@x.com"]
    refresh_index(onto)
    assert p["samples"] == ["ana@x.com"] and p["pii"] is False


def test_new_keys_travel_through_ossie_without_the_values():
    onto, _ = model()
    doc, _ = from_ontology(onto)
    assert not [f for f in validate_document(copy.deepcopy(doc)).findings if f.severity == "error"]
    assert "ana@x.com" not in str(doc) and "Ana Ruiz" not in str(doc)
    back = to_ontology(doc)
    props = {p["source_column"]: p for p in back["classes"]["Customer"]["data_properties"].values()}
    assert props["email"]["pii"] == "email_address" and props["signup"]["date_format"] == "MM/DD/YYYY"
    order = {p["source_column"]: p for p in back["classes"]["Order"]["data_properties"].values()}
    assert order["shipper_id"]["references"] == "shippers.shipper_id" and order["shipper_id"]["semantic_role"] == "key"


def test_name_only_descriptions_count_as_missing():
    from modules import answering, modeling
    onto, _ = model()
    cust, props = onto["classes"]["Customer"], onto["classes"]["Customer"]["data_properties"]
    cust["description"], cust["provenance"] = "customers", "schema+comment"     # a table comment naming the table
    props["city"]["description"] = cust["column_profile"]["city"]["comment"] = "City"     # a column comment too
    props["gender"]["description"] = "Gender"                                  # typed by a person: kept
    props["signup"]["description"] = "Day the customer signed up."
    assert llm_enrich.lacks_description(cust, "Customer", "customer", "customers")
    assert "Customer" in [r["class"] for r in modeling.worklist(onto) if "no description" in r["to do"]]
    assert answering.readiness(onto, "sqlite")["facts"].startswith("7 tables (0 described")
    llm_enrich._merge(onto, {"classes": {"Customer": {"description": "One row per customer."}},
                             "data_properties": {"Customer.city": {"description": "Town of the customer."},
                                                 "Customer.gender": {"description": "Gender of the customer."},
                                                 "Customer.signup": {"description": "Signup date."}}})
    assert cust["description"] == "One row per customer."
    assert props["city"]["description"] == "Town of the customer."
    assert props["gender"]["description"] == "Gender" and props["signup"]["description"] == "Day the customer signed up."
    assert llm_enrich._compact(onto, ["Customer"])["classes"]["Customer"]["properties"]["signup"]["date_format"]         == "MM/DD/YYYY"


def test_ask_prompt_explains_date_formats_and_default_joins():
    onto, _ = model()
    prompt = ask.answer_prompt("customers who signed up in 2022", onto, "ontology", "databricks")
    assert "date_format stores its dates as text" in prompt and "to_date(col, 'MM/dd/yyyy')" in prompt
    assert "marked default" in prompt and "COALESCE(x, 0)" in prompt


def _uc_runner(fail_with=None, calls=None):
    """information_schema answers for six one-column tables; profiling queries go to `fail_with`."""
    names = [f"t{i}" for i in range(6)]

    def run(sql, params=None):
        if "information_schema.tables" in sql:
            return pd.DataFrame({"table_name": names, "table_type": ["MANAGED"] * 6, "comment": [None] * 6})
        if "information_schema.columns" in sql:
            return pd.DataFrame({"table_name": names, "column_name": ["x"] * 6, "full_data_type": ["int"] * 6,
                                 "is_nullable": ["YES"] * 6, "column_default": [None] * 6, "comment": [None] * 6})
        if "table_constraints" in sql:
            return pd.DataFrame(columns=["table_name", "constraint_name", "constraint_type", "column_name",
                                         "ordinal_position"])
        if "referential_constraints" in sql:
            return pd.DataFrame(columns=["constraint_name", "rc", "rs", "rn"])
        calls.append(sql)
        raise fail_with
    return run


def test_profiling_stops_after_three_timeouts_in_a_row():
    calls = []
    ds = uc.extract_objects(_uc_runner(TimeoutError("timed out"), calls), [f"main.s.t{i}" for i in range(6)],
                            workers=1)
    assert len(calls) == 3 and sum("profiling skipped" in w for w in ds.warnings) == 3
    assert any("3 more table(s) were not profiled" in w for w in ds.warnings)
    calls = []
    ds = uc.extract_objects(_uc_runner(RuntimeError("[INSUFFICIENT_PERMISSIONS] no SELECT"), calls),
                            [f"main.s.t{i}" for i in range(6)], workers=1)
    assert len(calls) == 6 and not any("not profiled" in w for w in ds.warnings)       # not the warehouse


def test_overlap_checks_stop_when_the_warehouse_stops_answering():
    b = Table(name="b", primary_key=["b_id"], columns=[Column("b_id", "INTEGER", primary_key=True)])
    ds = DatabaseSchema(name="x", source_type="sqlite",
                        tables=[b] + [Table(name=f"a{i}", columns=[Column("b_id", "INTEGER")]) for i in range(5)])
    cands, calls = discovery.name_candidates(ds), []
    notes = discovery.verify_overlap(lambda sql: calls.append(sql) or (_ for _ in ()).throw(TimeoutError("timed out")),
                                     ds, cands, lambda t: t.name, lambda c: c)
    assert len(cands) == 5 and len(calls) == 3 and "skipped" in notes[-1] and "failed" in notes[-1]


def test_unity_catalog_profile_never_reads_personal_values():
    calls = []
    t = Table(name="customers", full_name="main.crm.customers",
              columns=[Column("customer_id", "BIGINT"), Column("email", "STRING"), Column("signup", "STRING"),
                       Column("city", "STRING"), Column("note", "STRING")])

    def run(sql, params=None):
        calls.append(sql)
        if "COUNT(*) AS `__n`" in sql:                  # only city is small enough to list all its values
            return pd.DataFrame([{"__n": 50, **{f"d{i}": n for i, n in enumerate([50, 50, 40, 2, 30])},
                                  **{f"nn{i}": 50 for i in range(5)}}])
        if sql.startswith("DESCRIBE"):
            raise RuntimeError("not a Delta table")
        if "array_sort" in sql:
            return pd.DataFrame([{"e0": ["Lyon", "Paris"]}])
        return pd.DataFrame({"signup": ["07/13/2021", "01/02/2022", "03/04/2022", None],
                             "city": ["Paris", "Lyon", "Paris", "Paris"], "note": [NOTE] * 4})
    uc._profile_table(run, t, enum_max=12)
    cols = {c.name: c for c in t.columns}
    assert "MIN(`customer_id`)" not in calls[0] and "approx_count_distinct(`email`)" in calls[0]
    sample_sql = next(s for s in calls if "LIMIT 200" in s)
    assert "`email`" not in sample_sql and "`customer_id`" not in sample_sql and "`city`" in sample_sql
    assert cols["email"].distinct_count == 50 and cols["email"].samples is None
    assert cols["signup"].date_format == "MM/DD/YYYY" and cols["city"].samples == ["Paris", "Lyon"]
    assert cols["note"].samples == [] and cols["city"].distinct_values == ["Lyon", "Paris"]
