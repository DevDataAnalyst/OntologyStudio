"""
Optional LLM pass (Databricks Foundation Model endpoint) that turns the
structural ontology into a business semantic layer, grounded in docs the user
uploads (data dictionary, KPI definitions, code lists, wiki pages...).

Fills — never overwrites anything that came from the schema:
  classes / properties : description, synonyms
  value_meanings       : code -> business meaning   (e.g. line_cd  M -> Mountain)
  rules                : business rules + SQL hint  (e.g. fiscal year starts 1 July)
  metrics              : KPI name + SQL expression  (e.g. AOV = SUM(subtotal)/COUNT(order))
Every item it adds carries `provenance`: "docs/<file>" when the docs support it, else "llm".
"""
from __future__ import annotations

import json
import os
import re

FM_ENDPOINT = os.getenv("FM_ENDPOINT_NAME", "databricks-meta-llama-3-3-70b-instruct")
MAX_DOC_CHARS = 60_000

SYSTEM_PROMPT = """You are a senior data modeller turning a database ontology into a business \
semantic layer for Text-to-SQL. You get (1) a compact ontology of classes, properties and \
relationships mapped to SQL tables/columns and (2) optional company documents.

Rules:
- Use only the class, property and column names you were given. Never invent tables or columns.
- Prefer facts from the documents. Set "provenance" to "docs/<file name>" when a document \
supports an item, otherwise "llm".
- Synonyms are business words people would type in a question (3-8 per item, lower case).
- Rules and metrics need valid SQL fragments using the given source table/column names.
- Only add value meanings for the coded columns listed under "coded_columns".
- Respond with ONLY JSON, no prose and no markdown fences, in exactly this shape:
{"classes": {"<Class>": {"description": "...", "synonyms": ["..."], "provenance": "..."}},
 "data_properties": {"<Class>.<property>": {"description": "...", "synonyms": ["..."], "provenance": "..."}},
 "object_properties": {"<property>": {"description": "..."}},
 "value_meanings": [{"class": "<Class>", "property": "<property>", "values": {"<code>": "<meaning>"}, "provenance": "..."}],
 "rules": [{"name": "snake_case", "description": "...", "applies_to": ["<Class>"], "sql_hint": "...", "synonyms": ["..."], "provenance": "..."}],
 "metrics": [{"name": "snake_case", "description": "...", "base_class": "<Class>", "sql": "...", "synonyms": ["..."], "provenance": "..."}]}"""


# --------------------------------------------------------------------------- #
# Model call
# --------------------------------------------------------------------------- #
def chat(system: str, user: str, max_tokens: int = 4000, endpoint: str | None = None) -> str:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    resp = WorkspaceClient().serving_endpoints.query(
        name=endpoint or FM_ENDPOINT,
        messages=[ChatMessage(role=ChatMessageRole.SYSTEM, content=system),
                  ChatMessage(role=ChatMessageRole.USER, content=user)],
        max_tokens=max_tokens, temperature=0,
    )
    return resp.choices[0].message.content.strip()


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1])


# --------------------------------------------------------------------------- #
# Enrichment
# --------------------------------------------------------------------------- #
def _compact(onto: dict, class_names: list[str]) -> dict:
    classes = {}
    for cid in class_names:
        c = onto["classes"][cid]
        classes[cid] = {
            "table": c.get("source_table"),
            "description": c.get("description"),
            "properties": {
                pid: {k: v for k, v in {
                    "column": p.get("source_column"), "type": p.get("range"),
                    "samples": p.get("samples") or p.get("allowed_values"),
                    "description": p.get("description")}.items() if v}
                for pid, p in c["data_properties"].items()},
        }
    rels = {pid: f'{p["domain"]} -> {p["range"]} ({p["cardinality"]}) join {p.get("join")}'
            for pid, p in onto["object_properties"].items()
            if p["domain"] in class_names and p.get("join")}
    coded = [{"class": vm["class"], "property": vm["property"], "codes": list(vm["values"])}
             for vm in onto.get("value_meanings", []) if vm["class"] in class_names]
    return {"classes": classes, "relationships": rels, "coded_columns": coded}


def _docs_block(docs: dict[str, str]) -> str:
    out, budget = [], MAX_DOC_CHARS
    for name, text in docs.items():
        chunk = text[: max(0, budget)]
        if not chunk:
            break
        out.append(f"### docs/{name}\n{chunk}")
        budget -= len(chunk)
    return "\n\n".join(out)


def enrich(onto: dict, docs: dict[str, str] | None = None, batch_size: int = 25,
           endpoint: str | None = None, progress=None) -> dict:
    """Mutates and returns `onto`. `docs` = {file name: text}."""
    from .ontology_builder import refresh_index

    names = list(onto["classes"])
    docs_text = _docs_block(docs or {})
    batches = [names[i:i + batch_size] for i in range(0, len(names), batch_size)] or [[]]
    for n, batch in enumerate(batches, 1):
        if progress:
            progress(n / len(batches), f"Enriching batch {n}/{len(batches)}")
        user = (f"ONTOLOGY:\n{json.dumps(_compact(onto, batch), default=str)}\n\n"
                f"DOCUMENTS:\n{docs_text or '(none provided)'}")
        result = parse_json(chat(SYSTEM_PROMPT, user, max_tokens=6000, endpoint=endpoint))
        _merge(onto, result)
    return refresh_index(onto)


def _merge(onto: dict, r: dict) -> None:
    classes, ops = onto["classes"], onto["object_properties"]

    def fill(target: dict, src: dict):
        if not src:
            return
        if src.get("description") and not target.get("description"):
            target["description"] = src["description"]
        if src.get("synonyms"):
            target["synonyms"] = sorted({*target.get("synonyms", []),
                                         *[str(x).lower() for x in src["synonyms"]]})
        if src.get("provenance") and "provenance" in target:
            if src["provenance"] not in str(target["provenance"]):
                target["provenance"] = f'{target["provenance"]}+{src["provenance"]}'

    for cid, src in (r.get("classes") or {}).items():
        if cid in classes:
            fill(classes[cid], src)
    for key, src in (r.get("data_properties") or {}).items():
        cid, _, pid = key.partition(".")
        if pid in classes.get(cid, {}).get("data_properties", {}):
            fill(classes[cid]["data_properties"][pid], src)
    for pid, src in (r.get("object_properties") or {}).items():
        if pid in ops and src.get("description") and not ops[pid].get("description"):
            ops[pid]["description"] = src["description"]

    vms = {(v["class"], v["property"]): v for v in onto.get("value_meanings", [])}
    for vm in r.get("value_meanings") or []:
        tgt = vms.get((vm.get("class"), vm.get("property")))
        if tgt:
            for code, meaning in (vm.get("values") or {}).items():
                if str(code) in tgt["values"] and not tgt["values"][str(code)]:
                    tgt["values"][str(code)] = meaning
            if vm.get("provenance"):
                tgt["provenance"] = vm["provenance"]

    for kind, required in (("rules", "sql_hint"), ("metrics", "sql")):
        existing = {x["name"] for x in onto.get(kind, [])}
        for item in r.get(kind) or []:
            if item.get("name") and item.get(required) and item["name"] not in existing:
                refs = item.get("applies_to") or [item.get("base_class")]
                if all(ref in classes for ref in refs if ref):
                    onto.setdefault(kind, []).append(item)
                    existing.add(item["name"])
