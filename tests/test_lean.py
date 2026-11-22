"""The lean layout: Genie space definition, the single deploy plan, the test set, and one home per job.
Run: python -m pytest tests/test_lean.py"""
import json
import pathlib
import re
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import deploy, genie, ossie, store, testset  # noqa: E402

SAMPLE = ROOT / "samples" / "ossie" / "tpcds_semantic_model.ossie.yaml"


def _model():
    doc = ossie.validate_text(SAMPLE.read_text(encoding="utf-8"), SAMPLE.name).document
    onto = ossie.to_ontology(doc, SAMPLE.name)
    onto["examples"] = [{"question": "Total store sales", "sql": "SELECT SUM(ss_net_paid)\nFROM store_sales"},
                        {"question": "Customers by state", "sql": "SELECT c_state, COUNT(*) FROM customer GROUP BY 1"}]
    onto["glossary"] = [{"name": "AOV", "definition": "average order value"}]
    return doc, onto


# --------------------------------------------------------------------------- #
# Genie space
# --------------------------------------------------------------------------- #
def _ids(defn):
    out = [q["id"] for q in (defn.get("config") or {}).get("sample_questions", [])]
    for k in ("text_instructions", "example_question_sqls", "join_specs"):
        out += [x["id"] for x in defn.get("instructions", {}).get(k, [])]
    out += [q["id"] for q in defn.get("benchmarks", {}).get("questions", [])]
    return out


def test_genie_space_definition_follows_the_api_rules():
    _, onto = _model()
    d = genie.space_definition(onto, ["tpcds.public.store_sales_metrics"])
    assert d["version"] == 2
    tables = d["data_sources"]["tables"]
    assert tables and all(t["identifier"].count(".") == 2 for t in tables)
    assert [t["identifier"] for t in tables] == sorted(t["identifier"] for t in tables)
    for t in tables:
        cols = [c["column_name"] for c in t.get("column_configs", [])]
        assert cols == sorted(cols)
    assert d["data_sources"]["metric_views"] == [{"identifier": "tpcds.public.store_sales_metrics"}]
    ids = _ids(d)
    assert ids and all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids) and len(ids) == len(set(ids))
    ins = d["instructions"]
    for k in ("example_question_sqls", "join_specs"):
        assert [x["id"] for x in ins[k]] == sorted(x["id"] for x in ins[k])
    assert len(ins["text_instructions"]) == 1 and "AOV means average order value" in ins["text_instructions"][0]["content"][0]
    for j in ins["join_specs"]:
        assert len(j["sql"]) == 2 and re.fullmatch(r"--rt=FROM_RELATIONSHIP_TYPE_[A-Z_]+--", j["sql"][1])
        assert j["sql"][0].startswith("`") and " = `" in j["sql"][0]
    ex = ins["example_question_sqls"][0]
    assert ex["sql"][0].endswith("\n") and not ex["sql"][-1].endswith("\n")
    assert len(d["benchmarks"]["questions"]) == 2
    assert d["benchmarks"]["questions"][0]["answer"][0]["format"] == "SQL"
    assert genie.space_definition(onto) == genie.space_definition(onto)          # stable ids: re-deploy updates
    assert genie.problems(d) == []
    json.dumps(d)


def test_genie_needs_unity_catalog_tables():
    _, onto = _model()
    for c in onto["classes"].values():
        c["source_fqn"] = c.get("source_table")
    d = genie.space_definition(onto)
    assert genie.problems(d) and not d["data_sources"]["tables"]


# --------------------------------------------------------------------------- #
# One deploy plan: tags, custom metric views, Genie
# --------------------------------------------------------------------------- #
def test_deploy_plan_writes_governance_tags_custom_views_and_genie():
    doc, onto = _model()
    fact = next(iter(onto["classes"]))
    onto["classes"][fact]["domains"] = ["Sales"]
    onto["classes"][fact]["certification"] = {"status": "certified", "owner": "ann@x.com"}
    from modules import metric_views as mv
    facts = mv.fact_classes(onto)
    onto["metric_views"] = {"my_view": {"fact": facts[0], "target": "tpcds.public.my_view",
                                        "view": mv.build_metric_view(onto, facts[0])}}
    p = deploy.plan(doc, onto, "tpcds", "public", metric_views=True, genie=True, genie_space_id="abc")
    sql = p.script()
    assert "'ontology_domain' = 'Sales'" in sql and "'ontology_certification' = 'certified'" in sql
    assert "`tpcds`.`public`.`my_view`" in sql
    assert p.genie and p.genie["space_id"] == "abc"
    mvs = [m["identifier"] for m in p.genie["definition"]["data_sources"]["metric_views"]]
    assert "tpcds.public.my_view" in mvs


def test_deploy_execute_runs_genie_and_logs_the_space(monkeypatch):
    doc, onto = _model()
    p = deploy.plan(doc, onto, "tpcds", "public", comments=False, tags=False, genie=True)
    written = {}
    monkeypatch.setattr(store, "write_text", lambda path, text, token: written.setdefault(path, text) and "you")
    monkeypatch.setattr(store, "read_or_none", lambda path, token: None)
    calls = []

    def fake_genie(defn, title, desc, sid):
        calls.append((title, sid))
        return {"space_id": "01f0abc", "url": "https://h/genie/rooms/01f0abc", "action": "created"}
    res = deploy.execute(p, lambda sql: None, None, genie_fn=fake_genie)
    assert res["ok"] and res["genie"]["space_id"] == "01f0abc" and calls[0][1] is None
    log = json.loads(written[f"{p.folder}/deployments.json"])
    assert deploy.last_genie_space(list(reversed(log))) == "01f0abc"

    res2 = deploy.execute(p, lambda sql: None, None,
                          genie_fn=lambda *a: (_ for _ in ()).throw(RuntimeError("missing scope genie")))
    assert not res2["ok"] and res2["genie"]["status"] == "failed" and "genie" in res2["genie"]["message"]


# --------------------------------------------------------------------------- #
# Test set
# --------------------------------------------------------------------------- #
def test_results_match_ignores_names_and_order():
    a = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    b = pd.DataFrame({"total": ["b", "a"], "n": [2.0, 1.0]})
    assert testset.results_match(a, b[["n", "total"]].iloc[::-1])[0]
    assert testset.results_match(a, b)[0]
    assert not testset.results_match(a, a.head(1))[0]
    assert not testset.results_match(a, pd.DataFrame({"x": [1, 3], "y": ["a", "b"]}))[0]


def test_run_set_scores_and_regressions():
    _, onto = _model()
    data = {"SELECT SUM(ss_net_paid)\nFROM store_sales": pd.DataFrame({"s": [10]}),
            "SELECT c_state, COUNT(*) FROM customer GROUP BY 1": pd.DataFrame({"st": ["CA"], "n": [3]}),
            "GOOD": pd.DataFrame({"x": [10]}), "BAD": pd.DataFrame({"x": [11]})}
    run = data.__getitem__
    first = testset.run_set(onto, lambda q: {"status": "ok", "sql": "GOOD"}, run,
                            baseline_fn=lambda q: {"status": "error", "error": "no idea"})
    assert first["total"] == 2 and first["passed"] == 1 and first["baseline_passed"] == 0
    assert testset.status(onto)["verdict"] == "1 of 2 pass"
    testset.run_set(onto, lambda q: {"status": "ok", "sql": "BAD"}, run)
    assert testset.regressions(onto) == ["Total store sales"]
    onto["examples"].append({"question": "New one", "sql": "GOOD"})
    assert testset.status(onto)["stale"]


# --------------------------------------------------------------------------- #
# One home per job
# --------------------------------------------------------------------------- #
def _calls(pattern: str) -> list[str]:
    hits = []
    for p in [ROOT / "app.py", *(ROOT / "modules").rglob("*.py")]:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(pattern, line) and not line.lstrip().startswith(("#", "def ")):
                hits.append(f"{p.relative_to(ROOT)}:{i}")
    return hits


def test_one_save_one_deploy_one_validator_entry():
    assert _calls(r"\bstore\.save\(") == [h for h in _calls(r"\bstore\.save\(") if "lifecycle_ui.py" in h]
    assert len(_calls(r"\bstore\.save\(")) == 1
    assert len(_calls(r"\bdp\.execute\(|\bdeploy\.execute\(")) == 1
    assert not _calls(r"WITH METRICS.*uc_runner|uc_runner\(\)\(ddl\)|deploy\(mv\.ddl")
    assert not [h for h in _calls(r"uc_tag_statements\(") if "deploy.py" not in h]
    uploaders = {pathlib.Path(h.split(":")[0]).name for h in _calls(r"file_uploader\(")}
    assert uploaders == {"connect.py", "ossie_form.py", "governance_ui.py"}     # model files: Connect only
    editors = [h for h in _calls(r"editor_tab\(") if "ossie_form.py" not in h]
    assert editors and all("allow_upload=False" in (ROOT / h.split(":")[0]).read_text().splitlines()[int(h.split(":")[1]) - 1]
                           for h in editors)
    assert not _calls(r'text_area\("Paste Ossie')                    # the old validate-only box is gone
    assert not (ROOT / "app.py").read_text().count("st.tabs(")


def test_test_runs_survive_the_ossie_save():
    _, onto = _model()
    testset.run_set(onto, lambda q: {"status": "ok", "sql": "x"}, None)
    doc, _ = ossie.from_ontology(onto, include_studio=True, include_layout=False)
    back = ossie.to_ontology(ossie.validate_text(ossie.dump_yaml(doc), "m.yaml").document, "m.yaml")
    assert back.get("test_runs") and back["test_runs"][0]["total"] == 2
    assert [e["question"] for e in back["examples"]] == [e["question"] for e in onto["examples"]]
