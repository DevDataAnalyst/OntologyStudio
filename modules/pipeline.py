"""
Getting a model into the studio: build one from tables, or open a saved / imported / pasted one.

Every path ends in `ss.onto` (the studio's model) plus `ss.runner` (how to reach its data) and
`ss.model_ref` (where it came from, so saves go to its folder and changes can be diffed).
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st
import yaml

from . import discovery, governance as gv, lifecycle_ui as lc, ossie, semantics, store
from .extractors import from_sql_script, from_sqlite_file, is_sqlite_bytes
from .ontology_builder import BuildOptions, attach_lineage, attach_suggestions, build_ontology, refresh_index
from .steps.common import ON_DATABRICKS, ss, uc_runner


@dataclass
class BuildRequest:
    source: str                                  # "uc" | "file"
    fqns: list = field(default_factory=list)
    upload: object = None                        # streamlit UploadedFile for a SQLite / .sql file
    dialect: str = "auto"
    profile: bool = True
    verify: bool = True
    use_history: bool = False
    history_days: int = 90
    use_lineage: bool = False
    lineage_days: int = 180
    threshold: float = 0.75
    enum_max: int = 12
    name: str = "MyOntology"
    base_iri: str = "http://example.org/ontology/"
    prefix: str = "ex"
    naming: str = "semantic"
    strip: bool = True
    collapse: bool = True
    include_views: bool = True
    enrich: bool = False
    docs: list = field(default_factory=list)     # uploaded business documents for the LLM pass


def iri_options() -> tuple[str, str]:
    return ss.get("opt_base_iri", "http://example.org/ontology/"), ss.get("opt_prefix", "ex")


def _sqlite_runner(path):
    def run_sql(q):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return pd.read_sql_query(q, conn)
        finally:
            conn.close()
    return run_sql


def _enrich(onto: dict, docs, bar, start: float = 0.8) -> dict:
    text = {f.name: f.getvalue().decode("utf-8-sig", errors="replace") for f in docs or []}
    try:
        from .llm_enrich import enrich as llm_enrich
        return llm_enrich(onto, text, progress=lambda f, m: bar.progress(start + (1 - start) * f, m))
    except Exception as e:  # noqa: BLE001
        onto.setdefault("warnings", []).append(f"LLM enrichment skipped: {e}")
        return onto


def build(r: BuildRequest) -> dict:
    status = st.status("Building the model…", expanded=True)
    bar = st.progress(0.0)
    lineage, joins, run_sql, table_ref, quote, run, history = None, None, None, None, None, None, None
    if r.source == "uc":
        from . import uc
        run = uc_runner()
        status.write(f"Reading {len(r.fqns)} objects from Unity Catalog…")
        ds = uc.extract_objects(run, r.fqns, profile=r.profile, enum_max=r.enum_max,
                                progress=lambda f, m: bar.progress(f * 0.6, m))
        if r.use_lineage:
            status.write("Reading lineage…")
            lineage = uc.read_lineage(run, r.fqns, r.lineage_days)
            ds.warnings += lineage.pop("warnings", [])
        if r.use_history:                      # one read of system.query.history feeds joins and snippets
            from . import snippets as sn
            status.write("Reading query history…")
            history = sn.read_query_history(run, r.fqns, r.history_days)
            ds.warnings += history["warnings"]
            texts = [x["text"] for x in history["records"] if "join" in str(x["text"]).lower()]
            joins = uc.mine_joins(texts, r.fqns) if texts else None
            if history["records"]:
                status.write(f"Parsed {len(history['records'])} queries, {len(joins or {})} distinct join pairs.")
        run_sql = run
        table_ref = lambda t: uc.bq(*t.full_name.split("."))  # noqa: E731
        quote = lambda c: f"`{c}`"  # noqa: E731
        ss.runner = {"kind": "uc", "dialect": "databricks"}
    else:
        data = r.upload.getvalue()
        name = re.sub(r"\.[^.]+$", "", r.upload.name)
        path = os.path.join(tempfile.gettempdir(), f"onto_{hashlib.sha1(data).hexdigest()[:16]}.db")
        if is_sqlite_bytes(data):
            open(path, "wb").write(data)
            ds = from_sqlite_file(data, name, profile=r.profile, enum_max=r.enum_max)
            sqlite_ok = True
        else:
            text = data.decode("utf-8-sig", errors="replace")
            ds = from_sql_script(text, name, dialect=r.dialect, profile=r.profile, enum_max=r.enum_max)
            sqlite_ok = ds.dialect == "sqlite"
            if sqlite_ok:
                if os.path.exists(path):
                    os.remove(path)
                conn = sqlite3.connect(path)
                conn.executescript(text)
                conn.close()
        if sqlite_ok:
            run_sql = _sqlite_runner(path)
            table_ref = lambda t: '"' + t.name.replace('"', '""') + '"'  # noqa: E731
            quote = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
            ss.runner = {"kind": "sqlite", "path": path, "dialect": "sqlite"}
        else:
            ss.runner = {"kind": None, "dialect": ds.dialect}
    bar.progress(0.65, "Discovering relationships…")
    status.write("Discovering relationships…")
    suggestions, notes = discovery.discover(ds, run_sql=run_sql, table_ref=table_ref, quote=quote, joins=joins,
                                            verify=r.verify and run_sql is not None, threshold=r.threshold)
    ds.warnings += [n for n in notes if "failed" in n]
    opts = BuildOptions(ontology_name=r.name, base_iri=r.base_iri, prefix=r.prefix, naming=r.naming,
                        strip_prefixes=r.strip, collapse_junction_tables=r.collapse,
                        include_views=r.include_views, enum_detection=r.profile, enum_max=r.enum_max)
    onto = build_ontology(ds, opts)
    attach_suggestions(onto, suggestions)
    if lineage:
        attach_lineage(onto, lineage)
    semantics.classify(onto)
    onto.setdefault("ontology", {})["discovery_notes"] = notes or None
    gv.ensure(onto)
    if r.source == "uc":                                   # domains / certification already tagged in UC
        status.write("Reading ontology tags…")
        n_tagged = gv.apply_uc_tags(onto, gv.read_uc_tags(run, r.fqns))
        if n_tagged:
            status.write(f"{n_tagged} table(s) carry domain / certification tags.")
        if history and history["records"]:              # the same history becomes learned snippets
            try:
                from . import snippets as sn
                sn.merge_into(onto, sn.mine(history["records"], onto, dialect="databricks"), ingest="query_history")
            except Exception as e:  # noqa: BLE001
                onto.setdefault("warnings", []).append(f"Learned snippets skipped: {str(e)[:200]}")
    if r.enrich:
        bar.progress(0.8, "LLM enrichment…")
        onto = _enrich(onto, r.docs, bar)
    refresh_index(onto)
    status.update(label="Model built", state="complete", expanded=False)
    bar.empty()
    ss.onto = onto
    ss.pop("ossie_loaded", None)
    ss.pop("model_ref", None)                              # a brand-new model
    _fresh(None)
    return onto


def _fresh(msg: str | None) -> None:
    ss.selection, ss.last_nonce, ss.graph_msg = {"kind": None}, None, msg
    ss.graph_key += 1


def _runner_for(new: dict) -> None:
    three_part = all(str(c.get("source_fqn") or "").count(".") == 2 for c in new["classes"].values())
    ss.runner = {"kind": "uc" if ON_DATABRICKS and three_part else None, "dialect": "databricks"}


def load_ossie(rep: ossie.Report, model_index: int = 0, enrich: bool = False, docs=None) -> dict:
    """Validated Ossie model -> studio model (0.1.1 files are unwrapped first)."""
    doc, notes = rep.document, []
    if rep.profile is not None and rep.profile.wrapped:
        docs_, notes = ossie.upgrade(doc)
        doc = docs_[min(model_index, len(docs_) - 1)]
    base_iri, prefix = iri_options()
    new = ossie.to_ontology(doc, rep.file_name, base_iri=base_iri, prefix=prefix)
    if enrich:
        bar = st.progress(0.0, "LLM enrichment…")
        new = _enrich(new, docs, bar, start=0.0)
        bar.empty()
    _runner_for(new)
    ss.ossie_loaded = {"file": rep.file_name, "notes": notes}
    return refresh_index(new)


def remember_model(path: str | None = None, model: str | None = None) -> None:
    """What the open model was loaded from, so saves go to its folder and changes can be diffed."""
    m = re.match(r".*/(\d{8}T\d+Z)\.[a-z.]*yaml$", path or "")
    ss.model_ref = {"model": store.slug(model or ss.onto.get("ontology", {}).get("name") or "model"),
                    "version": m[1] if m else "", "path": path or "", "text": lc.ossie_text(ss.onto)[0]}


def open_report(rep: ossie.Report, model_index: int = 0, path: str | None = None, enrich: bool = False,
                docs=None) -> None:
    ss.onto = load_ossie(rep, model_index, enrich, docs)
    gv.ensure(ss.onto)
    names = rep.model_names() if rep.models else []
    remember_model(path, names[model_index] if path is None and names else None)
    _fresh(f"Opened {rep.file_name}")


def open_text(text: str, file_name: str, path: str | None = None, force: bool = False) -> str | None:
    """Open a stored or pasted YAML: a studio model as is, an Apache Ossie model after validation.
    Returns an error message (and leaves the open model alone) when it cannot be opened."""
    model = path.split("/")[-3] if path and "/versions/" in path else (path.split("/")[-2] if path else None)
    if store.kind_of_text(text) == store.ONTOLOGY:
        new = yaml.safe_load(text)
        new.setdefault("layout", {})
        ss.onto = refresh_index(new)
        ss.pop("ossie_loaded", None)
    else:
        rep = ossie.validate_text(text, file_name)
        ss.ossie_report = rep
        if not rep.convertible:
            return f"{file_name} cannot be read as an Apache Ossie model: {rep.headline()}"
        if rep.errors and not force:
            return (f"{file_name} has {len(rep.errors)} validation error(s). Tick **Open even if it has "
                    "validation errors** to open it anyway, or read the report below.")
        ss.onto = load_ossie(rep, 0)
    gv.ensure(ss.onto)
    remember_model(path, model)
    _fresh(f"Opened {file_name}")
    return None


def submit_form_model(doc: dict, file_name: str) -> None:
    """The form editor's Submit: its (validated) Ossie model becomes the studio model."""
    base_iri, prefix = iri_options()
    new = ossie.to_ontology(doc, file_name, base_iri=base_iri, prefix=prefix)
    old = ss.get("onto") or {}
    for k in ("layout", "examples", "test_runs", "changes"):            # studio state the form doesn't edit
        if old.get(k) and not new.get(k):
            new[k] = old[k]
    _runner_for(new)
    ss.ossie_loaded = {"file": file_name, "notes": ["Loaded from the form editor."]}
    ss.onto = refresh_index(new)
    gv.ensure(ss.onto)
    if not ss.get("model_ref"):
        remember_model(None, doc.get("name"))
    _fresh(f"Loaded {file_name} from the form editor")
