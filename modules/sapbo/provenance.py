"""
Property-level provenance (roadmap §6).

Every semantic property the pipeline produces (an expression, a description, a cardinality …) is stored as a
*provenance record*:

    {"value": ..., "state": "EXTRACTED", "by": "extractor", "rule_id": None, "evidence": [...], "history": [...]}

States and the transitions allowed between them:

    EXTRACTED ──► DERIVED ──► LLM_PROPOSED ──► HUMAN_APPROVED / HUMAN_EDITED / HUMAN_REJECTED
        └────────────┴──────► NEEDS_HUMAN, UNSUPPORTED

Only EXTRACTED, DERIVED, HUMAN_APPROVED and HUMAN_EDITED values are *approved* and may be exported as Ossie
definitions. LLM_PROPOSED values travel only as proposals (in the SAP_BO extension), never as the definition.
"""
from __future__ import annotations

import datetime as _dt

EXTRACTED, DERIVED, LLM_PROPOSED = "EXTRACTED", "DERIVED", "LLM_PROPOSED"
HUMAN_APPROVED, HUMAN_EDITED, HUMAN_REJECTED = "HUMAN_APPROVED", "HUMAN_EDITED", "HUMAN_REJECTED"
NEEDS_HUMAN, UNSUPPORTED = "NEEDS_HUMAN", "UNSUPPORTED"
STATES = (EXTRACTED, DERIVED, LLM_PROPOSED, HUMAN_APPROVED, HUMAN_EDITED, HUMAN_REJECTED, NEEDS_HUMAN, UNSUPPORTED)
APPROVED = {EXTRACTED, DERIVED, HUMAN_APPROVED, HUMAN_EDITED}

# the client's five reporting classes
CLASS_OF = {EXTRACTED: "Extracted directly", DERIVED: "Derived deterministically",
            LLM_PROPOSED: "LLM-enriched", HUMAN_APPROVED: "LLM-enriched", HUMAN_EDITED: "LLM-enriched",
            HUMAN_REJECTED: "Requires human validation", NEEDS_HUMAN: "Requires human validation",
            UNSUPPORTED: "Unsupported/unresolved"}
CLASSES = ["Extracted directly", "Derived deterministically", "LLM-enriched", "Requires human validation",
           "Unsupported/unresolved"]

ALLOWED = {
    None: {EXTRACTED, DERIVED, LLM_PROPOSED, NEEDS_HUMAN, UNSUPPORTED},
    EXTRACTED: {DERIVED, NEEDS_HUMAN, UNSUPPORTED, HUMAN_APPROVED, HUMAN_EDITED},
    DERIVED: {LLM_PROPOSED, NEEDS_HUMAN, UNSUPPORTED, HUMAN_APPROVED, HUMAN_EDITED},
    NEEDS_HUMAN: {LLM_PROPOSED, HUMAN_APPROVED, HUMAN_EDITED, UNSUPPORTED},
    LLM_PROPOSED: {HUMAN_APPROVED, HUMAN_EDITED, HUMAN_REJECTED},
    HUMAN_REJECTED: {LLM_PROPOSED, HUMAN_EDITED, NEEDS_HUMAN},
    HUMAN_APPROVED: {HUMAN_EDITED},
    HUMAN_EDITED: {HUMAN_EDITED},
    UNSUPPORTED: {HUMAN_EDITED},
}


class TransitionError(ValueError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(value, state: str, by: str = "pipeline", rule_id: str | None = None, evidence=None, **extra) -> dict:
    if state not in ALLOWED[None]:
        raise TransitionError(f"a property can't start in state {state}")
    r = {"value": value, "state": state, "by": by, "rule_id": rule_id, "evidence": list(evidence or []),
         "history": []}
    r.update(extra)
    return r


def transition(rec: dict, state: str, by: str, value=..., note: str = "", **extra) -> dict:
    """Move a record to a new state (in place) and keep the previous state in its history."""
    if state not in STATES:
        raise TransitionError(f"unknown state {state}")
    if state not in ALLOWED.get(rec.get("state"), set()):
        raise TransitionError(f"{rec.get('state')} → {state} is not allowed")
    if state == HUMAN_EDITED and value is ...:
        raise TransitionError("HUMAN_EDITED needs the edited value")
    snap = {k: rec.get(k) for k in ("value", "state", "by", "rule_id")}
    snap.update({k: rec[k] for k in ("prompt_id", "confidence", "model") if k in rec})
    rec.setdefault("history", []).append(snap | {"at": _now()})
    rec["state"], rec["by"] = state, by
    if value is not ...:
        rec["value"] = value
    if note:
        rec["note"] = note
    rec.update(extra)
    return rec


def propose(rec: dict | None, value, model: str, prompt_id: str, confidence: float | None = None,
            evidence=None) -> dict:
    """An LLM proposal for a property that deterministic steps couldn't fill. The approved value is untouched:
    the proposal is stored beside it (`proposal`), and the state becomes LLM_PROPOSED."""
    if rec is None:
        rec = record(None, NEEDS_HUMAN, by="pipeline")
    if rec["state"] in (EXTRACTED,) and rec.get("value") not in (None, "", []):
        raise TransitionError("an extracted value can't be replaced by an LLM proposal")
    transition(rec, LLM_PROPOSED, by=f"llm:{model}", prompt_id=prompt_id, confidence=confidence, model=model)
    rec["proposal"] = value
    rec["evidence"] = list(dict.fromkeys(list(rec.get("evidence") or []) + list(evidence or [])))
    return rec


def review(rec: dict, decision: str, reviewer: str, edited_value=None, note: str = "") -> dict:
    """Human decision on a proposal: approve (proposal becomes the value), edit, or reject."""
    if decision == "approve":
        return transition(rec, HUMAN_APPROVED, by=f"reviewer:{reviewer}", value=rec.get("proposal"), note=note)
    if decision == "edit":
        return transition(rec, HUMAN_EDITED, by=f"reviewer:{reviewer}", value=edited_value, note=note)
    if decision == "reject":
        return transition(rec, HUMAN_REJECTED, by=f"reviewer:{reviewer}", note=note)
    raise ValueError(decision)


def approved_value(rec: dict | None, default=None):
    """The value to export as a definition, or `default` when the record isn't approved."""
    if not rec or rec.get("state") not in APPROVED:
        return default
    v = rec.get("value")
    return default if v in (None, "", []) else v


def klass(rec: dict | None) -> str:
    return CLASS_OF.get((rec or {}).get("state"), "Requires human validation")
