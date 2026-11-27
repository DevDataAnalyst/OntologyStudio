"""
LLM-assisted enrichment (WS3). The LLM only fills gaps deterministic steps left open, and only proposes:

  E1  business descriptions for datasets, fields and metrics that have none
  E2  synonyms for metrics and fields (≤ 10, the metric view limit)
  E5  explanations of same-name / different-calculation variants (shown to reviewers, never exported)

Hard rules (enforced here, not in the prompt): expressions, aggregations, filters, joins and cardinalities are
never touched; an EXTRACTED value is never replaced; nothing becomes approved until review() records a human
decision. Every proposal keeps the model, prompt id, confidence and the evidence it was given.

`chat` is injectable (default: the app's Foundation Model endpoint via modules.llm_enrich.chat) so tests and
offline runs can pass a stub.
"""
from __future__ import annotations

import json
import re

from . import provenance as pv

PROMPT_ID = "sapbo_describe@v1"
SYSTEM = """You document a finance semantic model migrated from SAP BusinessObjects for a railway company.
For each item you get its business name, folder, BO SQL, the Databricks SQL it became and the reports that use it.
Write a one-sentence business description (what it measures or describes, in business terms, with the unit or
grain when the SQL shows it) and up to 5 synonyms business users might type. Never invent business rules the SQL
doesn't show; if the meaning is unclear, say so and lower your confidence. Do not restate the SQL.
Answer with JSON only: {"items": [{"id": "...", "description": "...", "synonyms": ["..."], "confidence": 0.0-1.0}]}"""

VARIANT_SYSTEM = """Two or more finance measures share a business name but are calculated differently. Explain in two
sentences what differs and which one a CFO report most likely means by the name. Don't pick silently: say what a
human must confirm. JSON only: {"items": [{"id": "...", "explanation": "...", "recommended": "<metric name or empty>"}]}"""


def targets(c: dict, limit: int | None = None) -> list[dict]:
    """Elements whose description isn't approved (and has no pending proposal)."""
    out = []
    for m in c["metrics"].values():
        if not pv.approved_value(m["description"]) and m["description"].get("state") != pv.LLM_PROPOSED:
            out.append({"id": f"metric:{m['name']}", "name": m["label"], "folder": m.get("folder", ""),
                        "bo_sql": m["expression"].get("original"), "sql": m["expression"]["value"],
                        "reports": m.get("reports", []), "kind": m["kind"]})
    for d in c["datasets"].values():
        if not pv.approved_value(d["description"]) and d["description"].get("state") != pv.LLM_PROPOSED:
            fields = [f["label"] for f in d["fields"].values() if f.get("role") == "dimension"][:12]
            out.append({"id": f"dataset:{d['name']}", "name": d["name"], "folder": "", "bo_sql": d["bo_table"],
                        "sql": d["source"]["value"], "reports": [], "kind": d["role"]["value"],
                        "fields": fields})
        for f in d["fields"].values():
            if f.get("role") != "dimension":
                continue
            if not pv.approved_value(f["description"]) and f["description"].get("state") != pv.LLM_PROPOSED:
                out.append({"id": f"field:{d['name']}.{f['name']}", "name": f["label"], "folder": f.get("folder", ""),
                            "bo_sql": f["expression"].get("original"), "sql": f["expression"]["value"],
                            "reports": [], "kind": "dimension"})
    return out[:limit] if limit else out


def _element(c: dict, ident: str):
    kind, _, name = ident.partition(":")
    if kind == "metric":
        return c["metrics"].get(name)
    if kind == "dataset":
        return c["datasets"].get(name)
    if kind == "field":
        ds, _, f = name.partition(".")
        return (c["datasets"].get(ds) or {}).get("fields", {}).get(f)
    return None


def _parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0)).get("items", [])
    except json.JSONDecodeError:
        return []


def propose(c: dict, chat=None, model: str = "", batch: int = 20, limit: int | None = None,
            progress=None) -> dict:
    """Ask the LLM for descriptions/synonyms of every target; store them as LLM_PROPOSED. Returns stats."""
    if chat is None:
        from modules.llm_enrich import FM_ENDPOINT, chat as fm_chat
        chat, model = fm_chat, model or FM_ENDPOINT
    todo = targets(c, limit)
    stats = {"asked": len(todo), "proposed": 0, "skipped": 0}
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        if progress:
            progress(i / max(len(todo), 1), f"LLM proposals {i + 1}–{i + len(chunk)} of {len(todo)}")
        for it in _parse(chat(SYSTEM, json.dumps({"items": chunk}, default=str))):
            el = _element(c, str(it.get("id", "")))
            desc = (it.get("description") or "").strip()
            if el is None or not desc:
                stats["skipped"] += 1
                continue
            ev = [f"sir:{el.get('bo_object') or el.get('trace', {}).get('sir', '')}"]
            pv.propose(el["description"], desc, model or "llm", PROMPT_ID, it.get("confidence"), evidence=ev)
            syn = [s for s in (it.get("synonyms") or []) if isinstance(s, str) and s.strip()][:10]
            if syn and "synonyms" not in el:
                el["synonyms"] = pv.propose(None, syn, model or "llm", PROMPT_ID, it.get("confidence"), evidence=ev)
            stats["proposed"] += 1
    return stats


def explain_variants(c: dict, chat=None) -> list[dict]:
    if not c["variants"]:
        return []
    if chat is None:
        from modules.llm_enrich import chat as fm_chat
        chat = fm_chat
    items = [{"id": str(i), "label": v["label"], "definitions": v["expressions"], "reason": v["reason"]}
             for i, v in enumerate(c["variants"])]
    for it in _parse(chat(VARIANT_SYSTEM, json.dumps({"items": items}))):
        try:
            v = c["variants"][int(it.get("id"))]
        except (ValueError, IndexError, TypeError):
            continue
        v["llm_explanation"] = {"text": it.get("explanation", ""), "recommended": it.get("recommended") or None,
                                "state": pv.LLM_PROPOSED}
    return c["variants"]


def proposals(c: dict) -> list[dict]:
    """Pending proposals, for the review inbox."""
    rows = []

    def add(ident, el, prop):
        rec = el.get(prop)
        if rec and rec.get("state") == pv.LLM_PROPOSED:
            rows.append({"id": ident, "property": prop, "name": el.get("label") or el.get("name"),
                         "proposal": rec.get("proposal"), "confidence": rec.get("confidence"),
                         "by": rec.get("by"), "evidence": ", ".join(rec.get("evidence") or [])})
    for m in c["metrics"].values():
        add(f"metric:{m['name']}", m, "description")
        add(f"metric:{m['name']}", m, "synonyms")
    for d in c["datasets"].values():
        add(f"dataset:{d['name']}", d, "description")
        for f in d["fields"].values():
            add(f"field:{d['name']}.{f['name']}", f, "description")
            add(f"field:{d['name']}.{f['name']}", f, "synonyms")
    return rows


def review(c: dict, ident: str, prop: str, decision: str, reviewer: str, edited=None, note: str = "") -> dict:
    el = _element(c, ident)
    if el is None or prop not in ("description", "synonyms"):
        raise KeyError(f"{ident}.{prop}")
    return pv.review(el[prop], decision, reviewer, edited, note)


def acceptance(c: dict) -> dict:
    """Outcome of LLM proposals so far (answers roadmap Q3 with numbers)."""
    from collections import Counter
    states = Counter()
    for m in c["metrics"].values():
        for p in ("description", "synonyms"):
            rec = m.get(p)
            if rec and (rec.get("history") or rec["state"] == pv.LLM_PROPOSED) and any(
                    h.get("state") == pv.LLM_PROPOSED for h in rec.get("history", [])) | (rec["state"] == pv.LLM_PROPOSED):
                states[rec["state"]] += 1
    for d in c["datasets"].values():
        for el in [d] + list(d["fields"].values()):
            for p in ("description", "synonyms"):
                rec = el.get(p)
                if rec and (rec["state"] == pv.LLM_PROPOSED
                            or any(h.get("state") == pv.LLM_PROPOSED for h in rec.get("history", []))):
                    states[rec["state"]] += 1
    return {"pending": states[pv.LLM_PROPOSED], "approved": states[pv.HUMAN_APPROVED],
            "edited": states[pv.HUMAN_EDITED], "rejected": states[pv.HUMAN_REJECTED]}
