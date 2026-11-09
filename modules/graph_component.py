"""
Python side of the editable network diagram (components/ontology_graph).

build_elements() turns the ontology into Cytoscape elements:
  nodes : classes (coloured by role) + external tables seen only in lineage
  edges : rel  — relationships (forward side of each inverse pair)
          sug  — suggested relationships awaiting accept/reject
          lin  — lineage (table feeds table)

handle_event() applies an edit sent back by the diagram to the ontology.
"""
from __future__ import annotations

import os

import streamlit.components.v1 as components

from . import ontology_edit as ed

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "components", "ontology_graph")
_component = components.declare_component("ontology_graph", path=_DIR)

CARD = {"many-to-one": "N:1", "one-to-one": "1:1", "many-to-many": "N:M", "one-to-many": "1:N"}


def build_elements(onto: dict) -> list[dict]:
    els = []
    for cid, c in onto["classes"].items():
        els.append({"data": {"id": cid, "label": cid, "role": c.get("role", "entity"),
                             "rows": c.get("instance_count")}})
    seen = set()
    for pid, p in onto["object_properties"].items():
        if not p.get("join") or pid in seen:
            continue
        if p["cardinality"] == "one-to-many":
            continue
        inv = p.get("inverse")
        seen.update({pid, inv})
        conf = p.get("confidence")
        lab = f"{pid} ({CARD.get(p['cardinality'], '')})" + (f" {conf:.0%}" if conf and conf < 1 else "")
        els.append({"data": {"id": f"rel:{pid}", "source": p["domain"], "target": p["range"],
                             "label": lab, "kind": "rel", "pid": pid},
                    "classes": "rel" + (" m2m" if p["cardinality"] == "many-to-many" else "")})
    for s in onto.get("suggested_relationships", []):
        if s.get("status") != "proposed":
            continue
        els.append({"data": {"id": f"sug:{s['id']}", "source": s["domain"], "target": s["range"],
                             "label": f"{s['from_cols'][0]} → {s['to_cols'][0]} · {s['confidence']:.0%}",
                             "kind": "sug", "sid": s["id"]}, "classes": "sug"})
    ext = set()
    for i, e in enumerate(onto.get("lineage", {}).get("tables", [])):
        for side in ("source", "target"):
            nid = e[side]
            if nid not in onto["classes"] and nid not in ext:
                ext.add(nid)
                els.append({"data": {"id": nid, "label": nid.split(".")[-1], "full": nid,
                                     "role": "external"}, "classes": "external"})
        els.append({"data": {"id": f"lin:{i}", "source": e["source"], "target": e["target"],
                             "label": f"feeds · {e.get('events', 0)} events", "kind": "lin",
                             "idx": i}, "classes": "lin"})
    return els


def network(onto: dict, height: int = 640, dark: bool = False, message: str | None = None,
            key: str = "ontology_graph"):
    return _component(elements=build_elements(onto), positions=onto.get("layout", {}),
                      height=height, dark=dark, message=message, key=key, default=None)


def handle_event(onto: dict, evt: dict) -> tuple[str | None, dict | None]:
    """Apply one diagram event. Returns (message, selection or None)."""
    t = evt.get("type")
    if t == "positions":
        onto["layout"] = {k: v for k, v in (evt.get("positions") or {}).items()}
        return None, None
    if t == "select":
        return None, {"kind": evt.get("kind"), "id": evt.get("id"), "edge_kind": evt.get("edge_kind"),
                      "pid": evt.get("pid"), "sid": evt.get("sid")}
    if t == "connect":
        src, tgt = evt["source"], evt["target"]
        if src not in onto["classes"] or tgt not in onto["classes"]:
            return "Relationships can only connect classes (not external lineage tables).", None
        pid = ed.add_relationship(onto, src, tgt)
        p = onto["object_properties"][pid]
        return (f"Added {pid}: {', '.join(p['join']['from'])} → {', '.join(p['join']['to'])}. "
                "Check the join columns in the inspector."), {"kind": "edge", "id": f"rel:{pid}",
                                                              "edge_kind": "rel", "pid": pid}
    if t == "repoint":
        if evt["node"] not in onto["classes"]:
            return "Pick a class, not an external table.", None
        pid = ed.repoint(onto, evt["edge"], evt["end"], evt["node"])
        return f"Repointed → {pid}. Check the join columns.", {"kind": "edge", "id": f"rel:{pid}",
                                                               "edge_kind": "rel", "pid": pid}
    if t == "delete":
        ed.delete_relationship(onto, evt["edge"])
        return f"Deleted {evt['edge']}", {"kind": None}
    if t == "accept":
        pid = ed.accept_suggestion(onto, evt["sid"])
        return (f"Accepted → {pid}" if pid else "Suggestion not found"), \
            ({"kind": "edge", "id": f"rel:{pid}", "edge_kind": "rel", "pid": pid} if pid else None)
    if t == "reject":
        ed.reject_suggestion(onto, evt["sid"])
        return "Suggestion rejected", {"kind": None}
    return None, None
