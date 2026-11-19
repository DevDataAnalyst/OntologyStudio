"""Versioned store, deployment plan and source registry. Run: python -m pytest tests/test_lifecycle.py"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import deploy, ossie, sources, store  # noqa: E402

SAMPLE = ROOT / "samples" / "ossie" / "tpcds_semantic_model.ossie.yaml"


def _use_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", str(tmp_path))
    assert store.enabled()


def test_save_history_diff_restore(tmp_path, monkeypatch):
    _use_store(tmp_path, monkeypatch)
    text = SAMPLE.read_text(encoding="utf-8")
    rep = ossie.validate_text(text, SAMPLE.name)
    v1 = store.save(text, "TPC-DS Sales", None, note="first", author="a@x.com", report=rep)
    assert v1.model == "tpc-ds-sales" and v1.path.endswith(".ossie.yaml") and "/versions/" in v1.path
    v2 = store.save(text.replace("store sales", "shop sales", 1), "TPC-DS Sales", None, note="rename")
    assert (tmp_path / "tpc-ds-sales" / "latest.ossie.yaml").read_text() == store.load(v2.path, None)
    hist = store.history("tpc-ds-sales", None)
    assert [h.note for h in hist] == ["rename", "first"]
    assert hist[1].saved_by == "a@x.com" and hist[1].errors == 0 and hist[1].spec_version
    d = store.diff(store.load(v1.path, None), store.load(v2.path, None))
    assert "-" in d and "shop sales" in d
    v3 = store.restore(hist[1], None)
    assert store.load(v3.path, None) == text and v3.restored_from == v1.path
    assert len(store.history("tpc-ds-sales", None)) == 3
    assert store.list_models(None) == ["tpc-ds-sales"]


def test_legacy_versions_are_listed(tmp_path, monkeypatch):
    _use_store(tmp_path, monkeypatch)
    (tmp_path / "hr").mkdir()
    (tmp_path / "hr" / "20261101T101010Z.yaml").write_text("classes: {}\nobject_properties: {}\n")
    (h,) = store.history("hr", None)
    assert h.legacy and h.kind == store.ONTOLOGY
    assert store.kind_of_text(store.load(h.path, None)) == store.ONTOLOGY
    assert store.kind_of_text(SAMPLE.read_text()) == store.OSSIE


def test_deploy_plan():
    doc = ossie.validate_text(SAMPLE.read_text(encoding="utf-8"), SAMPLE.name).document
    assert deploy.default_target(doc) == ("tpcds", "public")
    onto = ossie.to_ontology(doc, SAMPLE.name)
    p = deploy.plan(doc, onto, "tpcds", "public", version="20261119T000000Z", metric_views=True, constraints=True)
    assert p.folder == "/Volumes/tpcds/public/semantic_models/" + store.slug(doc["name"])
    assert f"{p.folder}/latest.ossie.yaml" in p.files and f"{p.folder}/20261119T000000Z.ossie.yaml" in p.files
    c = p.counts()
    assert c["volume"] == 1 and c["comment"] > 5 and c["tag"] == len(doc["datasets"])
    assert c.get("metric_view", 0) >= 1 and c.get("constraint", 0) >= 1
    sql = p.script()
    assert "COMMENT ON TABLE `tpcds`.`public`.`store_sales` IS" in sql
    assert "SET TAGS ('semantic_model' = " in sql and "WITH METRICS" in sql and "NOT ENFORCED" in sql
    assert deploy.lit("it's") == "'it\\'s'"


def test_deploy_execute_records_failures(monkeypatch):
    doc = ossie.validate_text(SAMPLE.read_text(encoding="utf-8"), SAMPLE.name).document
    p = deploy.plan(doc, None, "tpcds", "public", comments=False, tags=True)
    written = {}
    monkeypatch.setattr(store, "write_text", lambda path, text, token: written.setdefault(path, text) and "you")
    monkeypatch.setattr(store, "read_or_none", lambda path, token: None)
    ran = []

    def run(sql):
        ran.append(sql)
        if sql.startswith("ALTER TABLE") and "store_sales" in sql:
            raise RuntimeError("[EXPECT_TABLE_NOT_VIEW] is a view")
    res = deploy.execute(p, run, None, author="me")
    assert res["ok"]                                        # the view fallback rescued the failing tag
    assert any(s.startswith("ALTER VIEW") for s in ran)
    log = json.loads(written[f"{p.folder}/deployments.json"])
    assert log[0]["deployed_by"] == "me" and len(log[0]["files"]) == 2


def test_source_registry():
    assert [s.key for s in sources.for_family(sources.DATABRICKS)] == ["Unity Catalog"]
    non = sources.for_family(sources.NON_DATABRICKS)
    assert non[0].available and not all(s.available for s in non)
    assert sources.label("Snowflake").endswith("(coming soon)")


def test_sql_warehouse_fallback_statements(monkeypatch):
    """Without the files scope the viewer's token still reaches the volume through PUT / GET / LIST."""
    seen, blob = [], {}

    def fake_run(self, statement, staging_dir):
        seen.append(statement)
        import re as _re
        if statement.startswith("PUT"):
            local, target = _re.findall(r"'([^']*)'", statement)
            blob[target] = open(local, "rb").read()
        elif statement.startswith("GET"):
            src, local = _re.findall(r"'([^']*)'", statement)
            open(local, "wb").write(blob[src])
        elif statement.startswith("LIST"):
            return [("/Volumes/c/s/v/m/versions/", "versions/", 0, 0), ("/Volumes/c/s/v/m/latest.ossie.yaml", "x", 1, 0)]
        return []
    monkeypatch.setattr(store._SqlVolume, "_run", fake_run)
    b = store._SqlVolume("tok")
    b.write("/Volumes/c/s/v/m/latest.ossie.yaml", b"name: m\n")
    assert b.read("/Volumes/c/s/v/m/latest.ossie.yaml") == b"name: m\n"
    assert b.listdir("/Volumes/c/s/v/m") == [("/Volumes/c/s/v/m/versions", True),
                                             ("/Volumes/c/s/v/m/latest.ossie.yaml", False)]
    assert seen[0].endswith("INTO '/Volumes/c/s/v/m/latest.ossie.yaml' OVERWRITE")
    assert seen[1].startswith("GET '/Volumes/c/s/v/m/latest.ossie.yaml' TO ")
    assert seen[2] == "LIST '/Volumes/c/s/v/m/'"


def test_deploy_grants_the_app_access_to_its_volume():
    doc = ossie.validate_text(SAMPLE.read_text(encoding="utf-8"), SAMPLE.name).document
    p = deploy.plan(doc, None, "tpcds", "public", comments=False, tags=False, grant_app="3939594d-sp")
    kinds = [s.kind for s in p.steps]
    assert kinds == ["volume", "grant", "grant", "grant"]
    assert p.steps[-1].sql == ("GRANT READ VOLUME, WRITE VOLUME ON VOLUME `tpcds`.`public`.`semantic_models` "
                               "TO `3939594d-sp`")
    assert not [s for s in deploy.plan(doc, None, "tpcds", "public").steps if s.kind == "grant"]
