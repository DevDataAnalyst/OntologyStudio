"""
Deploy an Apache Ossie semantic model into Databricks, next to the tables it describes.

A deployment is planned first (pure, no I/O: `plan()`), shown to the user, then run
(`execute()`) with the viewer's permissions:

  Files (always)  /Volumes/<catalog>/<schema>/<volume>/<model>/
                     <stamp>.ossie.yaml     the deployed version
                     latest.ossie.yaml      what consumers read
                     deployments.json       log of every deployment (who, when, what ran)
  SQL (optional)  CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.<volume>
                  COMMENT ON TABLE … / ALTER TABLE … ALTER COLUMN … COMMENT   (model descriptions)
                  ALTER TABLE … SET TAGS ('semantic_model' = …, 'semantic_model_version' = …)
                  CREATE OR REPLACE VIEW … WITH METRICS LANGUAGE YAML         (one per fact)
                  ALTER TABLE … ADD CONSTRAINT … PRIMARY KEY / FOREIGN KEY …  (informational)

Each statement runs on its own; a failure is recorded and the rest continue. Statements on
a table that turn out to be views get a fallback (ALTER VIEW / COMMENT ON TABLE).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import metric_views as mv
from . import store
from .ossie.convert import _bare_column, pick_expression

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TAG_MODEL, TAG_VERSION = "semantic_model", "semantic_model_version"


def q(name: str) -> str:
    """Backtick-quote one identifier part."""
    return "`" + str(name).replace("`", "``") + "`"


def qn(fqn: str) -> str:
    return ".".join(q(p.strip("`")) for p in str(fqn).split("."))


def lit(text: str) -> str:
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def three_part(source) -> str | None:
    s = str(source or "").strip()
    if not s or re.search(r"\s", s) or s.lower().startswith("select"):
        return None
    parts = [p.strip("`") for p in s.split(".")]
    return ".".join(parts) if len(parts) == 3 and all(parts) else None


@dataclass
class Step:
    kind: str                       # volume | comment | tag | metric_view | constraint
    sql: str
    target: str = ""
    fallback: str | None = None
    status: str = "planned"         # planned | ok | failed | skipped
    message: str = ""


@dataclass
class Plan:
    model: str
    version: str
    catalog: str
    schema: str
    volume: str
    folder: str
    files: dict = field(default_factory=dict)        # path -> text (deployments.json added at run time)
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.kind] = out.get(s.kind, 0) + 1
        return out

    def script(self) -> str:
        return ";\n\n".join(s.sql for s in self.steps) + (";" if self.steps else "")


def default_target(doc: dict) -> tuple[str, str]:
    """The catalog.schema most of the model's datasets live in."""
    seen: dict[tuple[str, str], int] = {}
    for d in doc.get("datasets") or []:
        fq = three_part(d.get("source"))
        if fq:
            c, s, _ = fq.split(".")
            seen[(c, s)] = seen.get((c, s), 0) + 1
    if not seen:
        return "", ""
    return max(seen.items(), key=lambda kv: kv[1])[0]


def uc_datasets(doc: dict) -> list[dict]:
    return [d for d in doc.get("datasets") or [] if isinstance(d, dict) and three_part(d.get("source"))]


def _column_of(f: dict) -> str | None:
    text, _ = pick_expression(f.get("expression"))
    return _bare_column(text) if text else _bare_column(f.get("name"))


def plan(doc: dict, onto: dict | None, catalog: str, schema: str, volume: str = "semantic_models",
         version: str | None = None, *, create_volume: bool = True, comments: bool = True, tags: bool = True,
         metric_views: bool = False, constraints: bool = False, overwrite_comments: bool = True,
         model_name: str | None = None, yaml_text: str | None = None) -> Plan:
    from . import ossie
    model = store.slug(model_name or doc.get("name") or "model")
    version = version or store.stamp()
    folder = f"/Volumes/{catalog}/{schema}/{volume}/{model}"
    p = Plan(model=model, version=version, catalog=catalog, schema=schema, volume=volume, folder=folder)
    text = yaml_text if yaml_text is not None else ossie.dump_yaml(doc)
    p.files[f"{folder}/{version}.ossie.yaml"] = text
    p.files[f"{folder}/latest.ossie.yaml"] = text

    if create_volume:
        p.steps.append(Step("volume", f"CREATE VOLUME IF NOT EXISTS {qn(f'{catalog}.{schema}.{volume}')}\n"
                                      f"COMMENT {lit('Semantic models (Apache Ossie YAML) deployed by Ontology Studio')}",
                            target=f"{catalog}.{schema}.{volume}"))
    ds_uc = uc_datasets(doc)
    skipped = len(doc.get("datasets") or []) - len(ds_uc)
    if skipped and (comments or tags or constraints):
        p.notes.append(f"{skipped} dataset(s) are not Unity Catalog tables (no catalog.schema.table source); "
                       "only the YAML file covers them.")

    for d in ds_uc:
        fq = three_part(d["source"])
        if comments:
            if d.get("description"):
                p.steps.append(Step("comment", f"COMMENT ON TABLE {qn(fq)} IS {lit(d['description'])}", target=fq,
                                    fallback=f"ALTER VIEW {qn(fq)} SET TBLPROPERTIES "
                                             f"('comment' = {lit(d['description'])})"))
            for f in d.get("fields") or []:
                col = _column_of(f) if isinstance(f, dict) else None
                if col and f.get("description"):
                    p.steps.append(Step(
                        "comment", f"ALTER TABLE {qn(fq)} ALTER COLUMN {q(col)} COMMENT {lit(f['description'])}",
                        target=f"{fq}.{col}",
                        fallback=f"COMMENT ON COLUMN {qn(fq)}.{q(col)} IS {lit(f['description'])}"))
        if tags:
            tag_sql = f"SET TAGS ({lit(TAG_MODEL)} = {lit(model)}, {lit(TAG_VERSION)} = {lit(version)})"
            p.steps.append(Step("tag", f"ALTER TABLE {qn(fq)} {tag_sql}", target=fq,
                                fallback=f"ALTER VIEW {qn(fq)} {tag_sql}"))

    if constraints:
        by_name = {d.get("name"): d for d in ds_uc}
        for d in ds_uc:
            pk = d.get("primary_key")
            if pk:
                cols = pk if isinstance(pk, list) else [pk]
                tbl = three_part(d["source"]).split(".")[-1]
                p.steps.append(Step("constraint",
                                    f"ALTER TABLE {qn(three_part(d['source']))} ADD CONSTRAINT {q(f'pk_{tbl}')} "
                                    f"PRIMARY KEY ({', '.join(q(c) for c in cols)}) NOT ENFORCED",
                                    target=three_part(d["source"])))
        for r in doc.get("relationships") or []:
            a, b = by_name.get(r.get("from")), by_name.get(r.get("to"))
            if not (a and b and r.get("from_columns") and r.get("to_columns")):
                continue
            name = re.sub(r"\W+", "_", f"fk_{r.get('name') or r['from'] + '_' + r['to']}")[:120]
            p.steps.append(Step("constraint",
                                f"ALTER TABLE {qn(three_part(a['source']))} ADD CONSTRAINT {q(name)} FOREIGN KEY "
                                f"({', '.join(q(c) for c in r['from_columns'])}) REFERENCES "
                                f"{qn(three_part(b['source']))} ({', '.join(q(c) for c in r['to_columns'])}) "
                                "NOT ENFORCED", target=three_part(a["source"])))
        if any(s.kind == "constraint" for s in p.steps):
            p.notes.append("Constraints are informational (NOT ENFORCED). Primary keys need NOT NULL columns, "
                           "and a statement fails if the constraint already exists; the rest still run.")

    if metric_views and onto is not None:
        facts = mv.fact_classes(onto)
        for f in facts:
            if not three_part(onto["classes"][f].get("source_fqn")):
                continue
            target = f"{catalog}.{schema}.{mv.view_name(onto, f)}"
            try:
                body = mv.to_yaml(mv.build_metric_view(onto, f))
            except Exception as e:  # noqa: BLE001
                p.notes.append(f"Metric view for {f} skipped: {e}")
                continue
            p.steps.append(Step("metric_view", mv.ddl(body, qn(target)), target=target))
        if not facts:
            p.notes.append("No fact datasets found, so there are no metric views to create.")
    return p


def execute(p: Plan, run, token: str | None, author: str = "", progress=None) -> dict:
    """Run the plan: SQL steps first (the volume must exist before files go in), then the files."""
    total = len(p.steps) + len(p.files) + 1
    done = 0

    def tick(msg):
        nonlocal done
        done += 1
        if progress:
            progress(min(done / total, 1.0), msg)

    for s in p.steps:
        try:
            run(s.sql)
            s.status = "ok"
        except Exception as e:  # noqa: BLE001
            if s.fallback:
                try:
                    run(s.fallback)
                    s.status, s.message = "ok", "ran the fallback statement"
                except Exception as e2:  # noqa: BLE001
                    s.status, s.message = "failed", str(e2).splitlines()[0][:300]
            else:
                s.status, s.message = "failed", str(e).splitlines()[0][:300]
        tick(f"{s.kind}: {s.target}")
        if s.kind == "volume" and s.status == "failed":
            p.notes.append("Could not create the volume; the files are written only if it already exists.")

    files_ok, file_errors, who = [], [], ""
    for path, text in p.files.items():
        try:
            who = store.write_text(path, text, token)
            files_ok.append(path)
        except Exception as e:  # noqa: BLE001
            file_errors.append(f"{path}: {str(e)[:300]}")
        tick(f"file: {path.rsplit('/', 1)[-1]}")

    log_path = f"{p.folder}/deployments.json"
    entry = {"version": p.version, "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "deployed_by": author, "model": p.model, "target": f"{p.catalog}.{p.schema}",
             "files": files_ok, "file_errors": file_errors,
             "steps": [{"kind": s.kind, "target": s.target, "status": s.status, "message": s.message}
                       for s in p.steps]}
    if files_ok:
        try:
            prev = store.read_or_none(log_path, token)
            log = json.loads(prev) if prev else []
            if not isinstance(log, list):
                log = []
        except ValueError:
            log = []
        log.append(entry)
        try:
            store.write_text(log_path, json.dumps(log, indent=2), token)
        except Exception as e:  # noqa: BLE001
            file_errors.append(f"{log_path}: {str(e)[:300]}")
    tick("deployment log")
    entry["written_as"] = who
    entry["ok"] = not file_errors and all(s.status == "ok" for s in p.steps)
    return entry


def read_log(catalog: str, schema: str, volume: str, model: str, token: str | None) -> list[dict]:
    text = store.read_or_none(f"/Volumes/{catalog}/{schema}/{volume}/{store.slug(model)}/deployments.json", token)
    try:
        log = json.loads(text) if text else []
    except ValueError:
        return []
    return list(reversed(log)) if isinstance(log, list) else []
