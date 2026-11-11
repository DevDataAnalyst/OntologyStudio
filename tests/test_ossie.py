"""Apache Ossie checks and round trip. Run: python -m pytest tests  (or: python tests/test_ossie.py)"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.ossie import from_ontology, to_ontology, upgrade, validate_document, validate_text  # noqa: E402

SAMPLES = ROOT / "samples"


def check(name):
    p = SAMPLES / "ossie" / name
    return validate_text(p.read_text(encoding="utf-8"), p.name)


def test_official_example_is_valid():
    rep = check("tpcds_semantic_model.ossie.yaml")
    assert rep.ok and not rep.warnings, rep.findings


def test_broken_files_fail_with_the_expected_checks():
    rep = check("broken_example.ossie.yaml")
    assert {f.check for f in rep.errors} == {"Schema"} and len(rep.errors) == 4
    rep = check("semantic_errors.ossie.yaml")
    assert {f.check for f in rep.errors} == {"Scope", "SQL", "Arity"}


def test_legacy_file_passes_and_upgrades():
    rep = check("sales_analytics_0.2.0.dev0.yaml")
    assert rep.ok and rep.legacy and [f.check for f in rep.warnings] == ["Version"]
    docs, _ = upgrade(rep.document)
    assert validate_document(docs[0]).ok and "nonportable_extensions" in docs[0]
    assert check("sales_analytics_0.2.0.dev0.yaml").ok
    strict = validate_text((SAMPLES / "ossie" / "sales_analytics_0.2.0.dev0.yaml").read_text(encoding="utf-8"),
                           strict=True)
    assert not strict.ok


def test_studio_ontology_round_trip():
    onto = yaml.safe_load((SAMPLES / "retail_star.ontology.yaml").read_text(encoding="utf-8"))
    doc, _ = from_ontology(onto)
    assert validate_document(doc).ok
    back = to_ontology(doc)
    assert set(back["classes"]) == set(onto["classes"])
    assert {c: x.get("role") for c, x in back["classes"].items()} == \
        {c: x.get("role") for c, x in onto["classes"].items()}
    assert validate_document(from_ontology(back)[0]).ok


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
