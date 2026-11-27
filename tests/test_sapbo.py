"""SAP BO POC pipeline on the synthetic CNR Finance bundle (no Databricks needed)."""
import json

import pytest

from modules import metric_views as mvlib
from modules.sapbo import bo_sql, bundle, consume, enrich, pipeline, webi_formula
from modules.sapbo import provenance as pv


@pytest.fixture(scope="module")
def res():
    return pipeline.run(bundle.sample(), "cnfinance", "semantic")


# --------------------------------------------------------------------------- provenance
def test_provenance_llm_never_replaces_extracted_and_needs_review():
    ext = pv.record("Freight revenue", pv.EXTRACTED, rule_id="bo_description")
    with pytest.raises(ValueError):
        pv.propose(ext, "Something else", "llm", "p@v1", 0.9, [])
    rec = pv.propose(None, "Revenue billed to customers", "llm", "p@v1", 0.8, ["sir:x"])
    assert rec["state"] == pv.LLM_PROPOSED
    assert pv.approved_value(rec) is None            # a proposal is not a definition
    pv.review(rec, "edit", "analyst", "Freight revenue billed, C$")
    assert rec["state"] == pv.HUMAN_EDITED
    assert pv.approved_value(rec) == "Freight revenue billed, C$"
    assert pv.klass(rec) == "LLM-enriched"


# --------------------------------------------------------------------------- BO SQL
def test_prompt_becomes_parameter():
    sql, notes = bo_sql.resolve("SELECT @Prompt('Fiscal year','N',,mono,free) FROM x")
    assert sql == "SELECT :fiscal_year FROM x"
    assert notes[0]["rule_id"] == "prompt_param"


def test_decode_and_boolean_flags():
    out, rules, err = bo_sql.to_databricks("DECODE(a,1,'x','y')", "oracle")
    assert err is None and out == "CASE WHEN a = 1 THEN 'x' ELSE 'y' END" and "decode_to_case" in rules
    out, _, _ = bo_sql.to_databricks("CASE WHEN t.IS_CLOSED = 1 THEN 1 END", "oracle",
                                     bool_cols={("T", "IS_CLOSED")})
    assert "= TRUE" in out.upper()


def test_decode_keeps_oracle_null_matching():
    out, _, _ = bo_sql.to_databricks("DECODE(a, NULL, 'none', b, 'same', 'other')", "oracle")
    assert "a IS NULL" in out and "a IS NOT DISTINCT FROM b" in out


def test_boolean_fix_resolves_table_aliases():
    out, err = bo_sql.transpile_statement("SELECT fp.FISCAL_YEAR FROM FISCAL_PERIODS fp WHERE fp.IS_CLOSED = 1",
                                          "oracle", {"FISCAL_PERIODS": "cnfinance.silver.fiscal_periods"},
                                          bool_cols={("FISCAL_PERIODS", "IS_CLOSED")})
    assert err is None and "= TRUE" in out.upper(), out


def test_recon_condition_operators():
    from modules.sapbo.recon import _condition
    assert _condition("r", "NotEqual", ["West"]) == "r <> 'West'"
    assert _condition("r", "NotInList", ["A", "B"]) == "r NOT IN ('A', 'B')"
    assert _condition("y", "Between", [2024, 2025]) == "y BETWEEN 2024 AND 2025"
    assert _condition("x", "Matches", ["A%"]) is None          # untranslated -> statement marked manual


def test_webi_formula_classifier():
    assert webi_formula.classify("=[Freight Revenue]/[Carloads]")["status"] == "Transformed"
    assert webi_formula.classify("=Rank([Revenue])")["kind"] == "presentation"
    assert webi_formula.classify("=[Revenue] ForEach ([Region])")["status"] == "Manual"


# --------------------------------------------------------------------------- extraction + normalization
def test_bundle_coverage_and_reports(res):
    assert len(res["sir"]["reports"]) == 10
    assert res["coverage"]["pct"] >= 95 and res["coverage"]["target_met"]


def test_planted_findings_are_reported(res):
    kinds = {f["kind"] for f in res["concepts"]["findings"]}
    for k in ("unmapped_table", "unmapped_object", "derived_cardinality", "theta_join", "aggregate_awareness",
              "projection_conflict", "ambiguous_name", "same_name_different_calc", "sql_conflict", "security"):
        assert k in kinds, k
    assert "## " in res["gap_report"]


def test_same_name_measures_are_disambiguated(res):
    m = res["concepts"]["metrics"]
    assert "revenue_freight_revenue" in m and "revenue_gl_entries" in m


def test_provenance_summary_uses_client_classes(res):
    p = res["provenance"]
    assert set(pv.CLASSES) <= set(p) and p["total"] == sum(p[k] for k in pv.CLASSES)
    assert p["Extracted directly"] > 0 and p["Derived deterministically"] > 0


# --------------------------------------------------------------------------- Ossie + metric views
def test_ossie_is_valid(res):
    assert res["ossie_validation"]["ok"], res["ossie_validation"]
    assert "SAP_BO" in res["ossie_yaml"]


def test_metric_views_pass_checks_and_stack_multi_fact(res):
    mv = res["mv"]
    assert mv["views"] and not any(mv["checks"].values()), mv["checks"]
    assert any(n.endswith("_stacked_metrics") for n in mv["views"])
    for name, view in mv["views"].items():
        assert "WITH METRICS" in mv["ddl"][name].upper()
        assert mvlib.to_yaml(view)
    statuses = {r["status"] for r in mv["report"]}
    assert statuses <= {"Direct", "Transformed", "Partial", "Unsupported", "Manual"}


def test_deploy_script(res):
    sql = pipeline.deploy_sql(res)
    assert sql.count("WITH METRICS") == len(res["mv"]["views"])
    assert "CREATE SCHEMA IF NOT EXISTS cnfinance.semantic" in sql


# --------------------------------------------------------------------------- consumption + reconciliation
def test_reconciliation_plan_ready(res):
    plan = res["recon_plan"]
    assert plan and all(i["status"] == "ready" for i in plan), [i for i in plan if i["status"] != "ready"]
    assert all("MEASURE(" in i["l3_sql"] for i in plan)


def test_pattern3_classifier(res):
    assert all(r["classification"] == r["expected"] for r in res["pattern3"])
    governed = {m["name"]: m["label"] for m in res["concepts"]["metrics"].values() if m["scope"] == "universe"}
    # a brand-new ratio is a business definition: flagged for review, not silently accepted
    assert consume.classify_dax("x", "DIVIDE([freight_revenue], [carloads])", governed)["classification"] == "review"
    assert consume.classify_dax("y", "SUM('freight_revenue'[linehaul_revenue])", governed)["classification"] \
        == "semantic override"


def test_powerbi_helpers(res):
    assert res["dax"] and any(r.get("dax") for r in res["dax"])
    assert "measure" in consume.tmdl_measures(res["dax"])
    assert res["p2_views"] and all(v["ddl"].upper().startswith("CREATE") for v in res["p2_views"])


# --------------------------------------------------------------------------- enrichment
def test_enrichment_with_stub_then_review():
    r = pipeline.run(bundle.sample())
    c = r["concepts"]
    tg = enrich.targets(c)
    assert tg

    def chat(system, user, **kw):
        items = json.loads(user)["items"]
        return json.dumps({"items": [{"id": it["id"], "description": f"About {it['name']}.",
                                      "synonyms": ["s1"], "confidence": 0.6} for it in items]})
    stats = enrich.propose(c, chat=chat, model="stub", limit=5)
    assert stats["proposed"] == 5
    props = enrich.proposals(c)
    assert props
    before = pipeline.rebuild(r)["ossie_yaml"]
    p = next(x for x in props if x["property"] == "description")
    assert "About" not in before or p["proposal"] not in before.split("description:")[0]
    enrich.review(c, p["id"], "description", "approve", "tester")
    after = pipeline.rebuild(r)
    assert p["proposal"] in after["ossie_yaml"]
    assert enrich.acceptance(c)["approved"] == 1
    assert after["provenance"]["LLM-enriched"] >= 1


# --------------------------------------------------------------------------- live-check regressions (2026-11-27)
def test_number_format_uses_yaml_1_1_keys(res):
    for v in res["mv"]["views"].values():
        for m in v["measures"]:
            assert "thousand_separators" not in (m.get("format") or {}), m["name"]
    from modules.sapbo.to_mv import _format
    assert _format("#,##0") == {"type": "number", "decimal_places": {"type": "exact", "places": 0}}
    assert _format("0.0")["hide_group_separator"] is True


def test_windows_follow_the_webi_dimension(res):
    w = {m["name"]: m["window"] for v in res["mv"]["views"].values() for m in v["measures"] if m.get("window")}
    assert w["revenue_py"] == [{"order": "fiscal_year", "range": "current", "offset": -1}]
    assert w["revenue_ytd"] == [{"order": "fiscal_period", "range": "cumulative"},
                                {"order": "fiscal_year", "range": "current"}]
    assert w["revenue_rolling_3m"][0]["range"] == "trailing 3 month inclusive"
    dax = {d["measure"]: d["dax"] for d in res["dax"]}
    assert "-3, MONTH" in dax["revenue_rolling_3m"] and "-1, YEAR" in dax["revenue_py"]


LIVE_CNFINANCE = {   # tables in the live cnfinance catalog (read from information_schema on 2026-11-27)
    "silver": {"ap_invoices", "ar_invoices", "budget", "commodities", "cost_centers", "customers",
               "freight_revenue", "gl_accounts", "gl_entries"},
    "gold": {"aging", "budget_vs_actual", "customer_revenue", "kpi_summary", "monthly_performance",
             "revenue_breakdown"},
}


def test_fixture_maps_only_to_live_cnfinance_tables():
    from modules.sapbo import fixture
    for bo, uc in fixture.UC_MAPPING.items():
        if uc is None:
            continue
        cat, sch, tbl = uc.split(".")
        assert cat == "cnfinance" and tbl in LIVE_CNFINANCE[sch], (bo, uc)


def test_inner_join_semantics_and_p2_filters(res):
    r04 = next(i for i in res["recon_plan"] if i["report"] == "R04" and i["data_provider"] == "DP0")
    assert "cost_center IS NOT NULL" in r04["l3_sql"]           # BO inner join vs metric view LEFT join
    p2 = {v["name"].split(".")[-1]: v["select"] for v in res["p2_views"]}
    assert "period_start_date < " in p2["v_r02_b1"]              # predefined filter: closed periods
    assert "open_amount > 0" in p2["v_r05_b1"]                   # predefined filter: open invoices
    assert "is_key_account = TRUE" in p2["v_r09_b1"]
    assert "fiscal_year AS `Fiscal Year`" in p2["v_r07_b1"]      # prompted field exposed, not hard-coded
    assert "= 2025" not in p2["v_r07_b1"]
