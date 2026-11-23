"""
Genie space from the semantic model: the Databricks-native place where business users ask questions.

`space_definition()` is pure (no I/O). It turns the model into the `serialized_space` JSON (version 2) that
POST /api/2.0/genie/spaces takes:

  data_sources.tables        every class with a catalog.schema.table source, its description, and one
                             column_config per column that has a description or synonyms
  data_sources.metric_views  the metric views the same deployment creates
  instructions               one text instruction (model description, rules, glossary, code values,
                             certified pages), join_specs from the modeled many-to-one joins,
                             example_question_sqls from the verified examples
  config.sample_questions    up to 5 verified example questions
  benchmarks.questions       every verified example, so Genie's own benchmarks re-check the model

The API rejects unsorted collections, so tables sort by identifier, column configs by column_name and
everything with an id by id. Ids are 32 lowercase hex characters, derived from the content so the same
model gives the same ids (a re-deploy updates the space instead of growing it).

`publish()` creates the space, or updates it when the last deployment recorded its id. It runs with the
viewer's token (the app needs the `genie` user authorization scope).
"""
from __future__ import annotations

import hashlib
import json
import re

RT = {"many-to-one": "MANY_TO_ONE", "one-to-many": "ONE_TO_MANY", "one-to-one": "ONE_TO_ONE",
      "many-to-many": "MANY_TO_MANY"}
MAX_STR = 25_000
MAX_SAMPLE_QUESTIONS = 5


def _id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _fqn(c: dict) -> str | None:
    fq = str(c.get("source_fqn") or "").replace("`", "")
    return fq if fq.count(".") == 2 and all(fq.split(".")) else None


def _aliases(classes: dict) -> dict:
    """One alias per class: the table name, widened to schema_table (then catalog_schema_table) on a clash."""
    fqs = {cid: _fqn(c) for cid, c in classes.items() if _fqn(c)}
    out: dict = {}
    for depth in (1, 2, 3):
        cand = {cid: re.sub(r"\W+", "_", "_".join(fq.split(".")[-depth:])) for cid, fq in fqs.items()
                if cid not in out}
        taken = list(out.values()) + list(cand.values())
        for cid, a in cand.items():
            if taken.count(a) == 1 or depth == 3:
                out[cid] = a
    return out


def _lines(sql: str) -> list[str]:
    rows = str(sql or "").strip().splitlines()
    return [r + "\n" for r in rows[:-1]] + rows[-1:] if rows else []


def _cut(text: str) -> str:
    return text if len(text) <= MAX_STR else text[:MAX_STR - 1] + "…"


def instructions_text(onto: dict) -> str:
    """One plain-text instruction for Genie: what the model knows that the tables don't say."""
    o = onto.get("ontology") or {}
    out = []
    if o.get("description"):
        out.append(str(o["description"]).strip())
    rules = [r for r in onto.get("rules") or [] if r.get("status") != "deprecated"]
    if rules:
        out.append("Business rules:")
        out += [f"- {r.get('name')}: {r.get('description') or ''}"
                + (f" (SQL: {r['sql_hint']})" if r.get("sql_hint") else "") for r in rules]
    gl = onto.get("glossary") or []
    if gl:
        out.append("Glossary:")
        out += [f"- {g.get('name')} means {g.get('definition')}" for g in gl if g.get("name")]
    vms = [v for v in onto.get("value_meanings") or [] if v.get("values")]
    if vms:
        out.append("Code values:")
        for v in vms:
            c = (onto.get("classes") or {}).get(v.get("class"), {})
            col = v.get("column") or v.get("property")
            out.append(f"- {c.get('source_table') or v.get('class')}.{col}: "
                       + ", ".join(f"{k} = {d}" for k, d in v["values"].items() if d))
    metrics = [m for m in onto.get("metrics") or [] if m.get("status") != "deprecated"]
    if metrics:
        out.append("Metrics (prefer certified ones):")
        out += [f"- {m['name']} = {m.get('sql')}" + (" [certified]" if m.get("status") == "certified" else "")
                + (f": {m['description']}" if m.get("description") else "") for m in metrics]
    pages = [p for p in onto.get("pages") or [] if p.get("status") == "certified"]
    for p in pages:
        out.append(f"{p.get('title')}:\n{str(p.get('body') or '').strip()}")
    return _cut("\n".join(x for x in out if x).strip())


def space_definition(onto: dict, metric_views: list[str] | None = None) -> dict:
    """serialized_space (as a dict) for the model. `metric_views`: catalog.schema.view names to include."""
    classes = onto.get("classes") or {}
    tables = []
    for cid, c in classes.items():
        fq = _fqn(c)
        if not fq:
            continue
        t: dict = {"identifier": fq}
        if c.get("description"):
            t["description"] = [_cut(str(c["description"]))]
        cfgs = {}
        for pid, p in (c.get("data_properties") or {}).items():
            col = str(p.get("source_column") or pid)
            cc: dict = {}
            if p.get("description"):
                cc["description"] = [_cut(str(p["description"]))]
            syn = [s for s in p.get("synonyms") or [] if s]
            if syn:
                cc["synonyms"] = syn[:20]
            if cc and col not in cfgs:
                cfgs[col] = {"column_name": col, **cc}
        if cfgs:
            t["column_configs"] = [cfgs[k] for k in sorted(cfgs)]
        tables.append(t)
    seen = set()
    tables = [t for t in sorted(tables, key=lambda t: t["identifier"])
              if not (t["identifier"] in seen or seen.add(t["identifier"]))]
    data_sources: dict = {"tables": tables}
    mvs = sorted(dict.fromkeys(m.replace("`", "") for m in metric_views or [] if m.count(".") == 2))
    if mvs:
        data_sources["metric_views"] = [{"identifier": m} for m in mvs]

    alias = _aliases(classes)
    joins = []
    for pid, p in (onto.get("object_properties") or {}).items():
        j = p.get("join") or {}
        if p.get("cardinality") not in ("many-to-one", "one-to-one") or not j.get("from") or not j.get("to"):
            continue
        a, b = p.get("domain"), p.get("range")
        if a not in alias or b not in alias or a == b:
            continue
        cond = " AND ".join(f"`{alias[a]}`.`{f.rpartition('.')[2]}` = `{alias[b]}`.`{t.rpartition('.')[2]}`"
                            for f, t in zip(j["from"], j["to"]))
        spec = {"id": _id("join", pid, cond),
                "left": {"identifier": _fqn(classes[a]), "alias": alias[a]},
                "right": {"identifier": _fqn(classes[b]), "alias": alias[b]},
                "sql": [cond, f"--rt=FROM_RELATIONSHIP_TYPE_{RT[p['cardinality']]}--"]}
        note = p.get("description") or p.get("role")
        if note:
            spec["comment"] = [str(note)]
        joins.append(spec)

    examples = [e for e in onto.get("examples") or [] if e.get("question") and e.get("sql")]
    instructions: dict = {}
    text = instructions_text(onto)
    if text:
        instructions["text_instructions"] = [{"id": _id("text", text), "content": [text]}]
    if examples:
        instructions["example_question_sqls"] = sorted(
            [{"id": _id("example", e["question"]), "question": [e["question"]], "sql": _lines(e["sql"])}
             for e in examples], key=lambda x: x["id"])
    if joins:
        instructions["join_specs"] = sorted({j["id"]: j for j in joins}.values(), key=lambda x: x["id"])

    out: dict = {"version": 2}
    if examples:
        out["config"] = {"sample_questions": sorted(
            [{"id": _id("sample", e["question"]), "question": [e["question"]]}
             for e in examples[:MAX_SAMPLE_QUESTIONS]], key=lambda x: x["id"])}
    out["data_sources"] = data_sources
    if instructions:
        out["instructions"] = instructions
    if examples:
        out["benchmarks"] = {"questions": sorted(
            [{"id": _id("benchmark", e["question"]), "question": [e["question"]],
              "answer": [{"format": "SQL", "content": _lines(e["sql"])}]} for e in examples],
            key=lambda x: x["id"])}
    return out


def summary(defn: dict) -> dict:
    ds, ins = defn.get("data_sources") or {}, defn.get("instructions") or {}
    return {"tables": len(ds.get("tables") or []), "metric_views": len(ds.get("metric_views") or []),
            "joins": len(ins.get("join_specs") or []), "examples": len(ins.get("example_question_sqls") or []),
            "instructions": len(ins.get("text_instructions") or []),
            "benchmarks": len((defn.get("benchmarks") or {}).get("questions") or [])}


def problems(defn: dict) -> list[str]:
    """What would make the API reject the definition, checked before calling it."""
    out = []
    if not (defn.get("data_sources") or {}).get("tables"):
        out.append("No Unity Catalog tables (catalog.schema.table) in the model, so there is nothing for Genie "
                   "to query.")
    ids = [x["id"] for k in ("text_instructions", "example_question_sqls", "join_specs")
           for x in (defn.get("instructions") or {}).get(k) or []]
    if len(ids) != len(set(ids)):
        out.append("Two instructions have the same id.")
    return out


def curl_command(defn: dict, warehouse_id: str, title: str, description: str = "") -> str:
    body = {"warehouse_id": warehouse_id or "<warehouse-id>", "title": title, "description": description,
            "serialized_space": json.dumps(defn)}
    return ("curl -X POST \"https://$DATABRICKS_HOST/api/2.0/genie/spaces\" \\\n"
            "  -H \"Authorization: Bearer $DATABRICKS_TOKEN\" -H \"Content-Type: application/json\" \\\n"
            f"  -d '{json.dumps(body).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")


def space_url(host: str, space_id: str) -> str:
    host = (host or "").replace("https://", "").rstrip("/")
    return f"https://{host}/genie/rooms/{space_id}" if host and space_id else ""


def publish(defn: dict, warehouse_id: str, title: str, description: str, token: str | None,
            space_id: str | None = None) -> dict:
    """Create the space, or replace the configuration of `space_id`. Returns {space_id, url, action}."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    cfg = Config()
    w = WorkspaceClient(host=cfg.host, token=token, auth_type="pat") if token else WorkspaceClient()
    text = json.dumps(defn)
    if space_id:
        try:
            sp = w.genie.update_space(space_id, serialized_space=text, title=title, description=description or None,
                                      warehouse_id=warehouse_id)
            return {"space_id": sp.space_id or space_id, "url": space_url(cfg.host, sp.space_id or space_id),
                    "action": "updated"}
        except Exception as e:  # noqa: BLE001   the space was deleted or moved: make a new one
            if "not found" not in str(e).lower() and "does not exist" not in str(e).lower():
                raise
    sp = w.genie.create_space(warehouse_id, text, title=title, description=description or None)
    return {"space_id": sp.space_id, "url": space_url(cfg.host, sp.space_id), "action": "created"}
