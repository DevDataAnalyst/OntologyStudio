"""
Python side of the editable network diagram (components/ontology_graph, drawn with D3.js).

build_graph() turns the ontology into nodes and links for the diagram:
  nodes : classes (coloured by role, with row/column/measure counts and a column preview
          for the tooltip) + external tables seen only in lineage
  links : rel  — relationships (forward side of each inverse pair), with cardinality for
                 the crow's-foot ends
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
CARD_TEXT = {"N:1": "many-to-one", "1:1": "one-to-one", "N:M": "many-to-many"}
_ROLE_ORDER = {"key": 0, "time": 1, "measure": 2, "dimension": 3}
MAX_PREVIEW = 14


def _columns_preview(props: dict) -> list[dict]:
    items = [{"n": pid, "r": p.get("semantic_role") or "dimension"} for pid, p in props.items()]
    items.sort(key=lambda c: _ROLE_ORDER.get(c["r"], 9))
    return items[:MAX_PREVIEW]


def build_graph(onto: dict) -> dict:
    classes = onto.get("classes", {})
    metric_count: dict[str, int] = {}
    for m in onto.get("metrics", []):
        if m.get("base_class"):
            metric_count[m["base_class"]] = metric_count.get(m["base_class"], 0) + 1
    nodes, links = [], []
    for cid, c in classes.items():
        props = c.get("data_properties", {})
        roles = [p.get("semantic_role") for p in props.values()]
        nodes.append({
            "id": cid, "label": cid, "role": c.get("role", "entity"), "rows": c.get("instance_count"),
            "table": c.get("source_fqn") or c.get("source_table") or "",
            "desc": (c.get("description") or "")[:280],
            "cols": len(props), "keys": roles.count("key"), "measures": roles.count("measure"),
            "times": roles.count("time"), "metrics": metric_count.get(cid, 0),
            "columns": _columns_preview(props),
        })
    seen = set()
    ops = onto.get("object_properties", {})
    for pid, p in ops.items():
        if not p.get("join") or pid in seen or p.get("cardinality") == "one-to-many":
            continue
        if p.get("domain") not in classes or p.get("range") not in classes:
            continue
        seen.update({pid, p.get("inverse")})
        card = CARD.get(p.get("cardinality"), "")
        conf = p.get("confidence")
        join = ", ".join(p["join"].get("from", [])) + " → " + ", ".join(p["join"].get("to", []))
        if p["join"].get("via"):
            join += f"  (via {p['join']['via']})"
        links.append({"id": f"rel:{pid}", "source": p["domain"], "target": p["range"], "kind": "rel",
                      "pid": pid, "card": card, "cardText": CARD_TEXT.get(card, p.get("cardinality", "")),
                      "conf": conf, "prov": p.get("provenance"), "join": join,
                      "label": f"{pid} · {card}" + (f" · {conf:.0%}" if conf and conf < 1 else "")})
    for s in onto.get("suggested_relationships", []):
        if s.get("status") != "proposed" or s.get("domain") not in classes or s.get("range") not in classes:
            continue
        fc, tc = s.get("from_cols") or ["?"], s.get("to_cols") or ["?"]
        links.append({"id": f"sug:{s['id']}", "source": s["domain"], "target": s["range"], "kind": "sug",
                      "sid": s["id"], "conf": s.get("confidence"),
                      "join": f"{s['domain']}.{', '.join(fc)} → {s['range']}.{', '.join(tc)}",
                      "evidence": [str(e) for e in (s.get("evidence") or [])][:3],
                      "label": f"{fc[0]} → {tc[0]} · {s.get('confidence', 0):.0%}"})
    ext = set()
    for i, e in enumerate(onto.get("lineage", {}).get("tables", [])):
        for side in ("source", "target"):
            nid = e[side]
            if nid not in classes and nid not in ext:
                ext.add(nid)
                nodes.append({"id": nid, "label": str(nid).split(".")[-1], "full": nid, "role": "external"})
        links.append({"id": f"lin:{i}", "source": e["source"], "target": e["target"], "kind": "lin", "idx": i,
                      "events": e.get("events", 0), "last": str(e.get("last_seen") or "")[:19],
                      "label": f"feeds · {e.get('events', 0)}"})
    return {"nodes": nodes, "links": links}


def network(onto: dict, height: int = 640, dark: bool = False, message: str | None = None,
            selected: dict | None = None, key: str = "ontology_graph"):
    sel = {"kind": (selected or {}).get("kind"), "id": (selected or {}).get("id")}
    return _component(graph=build_graph(onto), positions=onto.get("layout", {}), height=height, dark=dark,
                      message=message, selected=sel, key=key, default=None)


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
