"""
Edit operations on the ontology dict. Used by the network diagram and the
inspector panel. Every operation keeps forward/inverse object-property pairs
consistent and records provenance "manual".
"""
from __future__ import annotations

import re

from .ontology_builder import camel, label, pascal, plural, refresh_index


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def class_columns(onto: dict, cid: str) -> list[str]:
    c = onto["classes"][cid]
    cols = c.get("source_columns") or [p.get("source_column", pid) for pid, p in c["data_properties"].items()]
    return list(cols)


def class_key(onto: dict, cid: str) -> list[str]:
    c = onto["classes"][cid]
    out = []
    for pid in c.get("identifier", []):
        p = c["data_properties"].get(pid, {})
        out.append(p.get("source_column", pid))
    return out


def guess_join_columns(onto: dict, domain: str, rng: str) -> tuple[list[str], list[str]]:
    """Best guess for domain.fk -> range.pk when the user draws a new edge."""
    to_cols = class_key(onto, rng) or class_columns(onto, rng)[:1]
    dcols = class_columns(onto, domain)
    lower = {c.lower(): c for c in dcols}
    rc = onto["classes"][rng]
    stems = {rng.lower(), rc.get("source_table", "").lower(),
             re.sub(r"s$", "", rc.get("source_table", "").lower())}
    if len(to_cols) == 1:
        pk = to_cols[0].lower()
        if pk in lower and pk not in [k.lower() for k in class_key(onto, domain)]:
            return [lower[pk]], to_cols
        for c in dcols:
            cl = c.lower()
            if any(cl.startswith(s) and re.search(r"(id|key|code|no)$", cl) for s in stems if s):
                return [c], to_cols
    own_key = {k.lower() for k in class_key(onto, domain)}
    keyish = [c for c in dcols if c.lower() not in own_key and re.search(r"(id|key|code|no)$", c.lower())]
    rest = [c for c in dcols if c.lower() not in own_key]
    return (keyish or rest or dcols)[:1], to_cols


def forward_pid(onto: dict, pid: str) -> str:
    """Return the forward (join-owning) side of a relationship pair."""
    p = onto["object_properties"][pid]
    inv = p.get("inverse")
    if p.get("cardinality") == "one-to-many" and inv in onto["object_properties"]:
        return inv
    return pid


def _unique(onto: dict, cand: str, domain: str) -> str:
    ops = onto["object_properties"]
    if cand in ops:
        cand = camel(domain) + cand[:1].upper() + cand[1:]
    base, i = cand, 2
    while cand in ops:
        cand, i = f"{base}{i}", i + 1
    return cand


def _table(onto, cid):
    return onto["classes"][cid].get("source_table", cid)


# --------------------------------------------------------------------------- #
# relationships
# --------------------------------------------------------------------------- #
def add_relationship(onto: dict, domain: str, rng: str, from_cols: list[str] | None = None,
                     to_cols: list[str] | None = None, cardinality: str = "many-to-one",
                     name: str | None = None, provenance: str = "manual", **extra) -> str:
    if domain not in onto["classes"] or rng not in onto["classes"]:
        raise ValueError("Both ends must be classes")
    g_from, g_to = guess_join_columns(onto, domain, rng)
    from_cols, to_cols = from_cols or g_from, to_cols or g_to
    one = cardinality == "one-to-one"
    many = cardinality == "many-to-many"
    fwd = _unique(onto, name or ("has" + (pascal(plural(rng)) if many else rng)), domain)
    inv_name = ("has" + (rng if one else pascal(plural(domain)))) if domain != rng else f"{camel(fwd)}Of"
    if many:
        inv_name = "has" + pascal(plural(domain))
    inv = _unique(onto, inv_name, rng)
    ops = onto["object_properties"]
    ops[fwd] = {"label": label(fwd), "domain": domain, "range": rng, "cardinality": cardinality,
                "functional": not many, "required": False, "inverse": inv,
                "join": {"from": [f"{_table(onto, domain)}.{c}" for c in from_cols],
                         "to": [f"{_table(onto, rng)}.{c}" for c in to_cols]},
                "provenance": provenance, **extra}
    ops[inv] = {"label": label(inv), "domain": rng, "range": domain,
                "cardinality": {"many-to-one": "one-to-many", "one-to-one": "one-to-one",
                                "many-to-many": "many-to-many"}[cardinality],
                "functional": one, "inverse": fwd, "provenance": provenance}
    if many:
        ops[inv]["join"] = {"from": ops[fwd]["join"]["to"], "to": ops[fwd]["join"]["from"]}
    refresh_index(onto)
    return fwd


def delete_relationship(onto: dict, pid: str) -> None:
    ops = onto["object_properties"]
    inv = ops.get(pid, {}).get("inverse")
    ops.pop(pid, None)
    if inv:
        ops.pop(inv, None)
    refresh_index(onto)


def update_relationship(onto: dict, pid: str, from_cols: list[str] | None = None,
                        to_cols: list[str] | None = None, cardinality: str | None = None,
                        new_name: str | None = None) -> str:
    pid = forward_pid(onto, pid)
    p = onto["object_properties"][pid]
    from_cols = from_cols or [x.rpartition(".")[2] for x in p["join"]["from"]]
    to_cols = to_cols or [x.rpartition(".")[2] for x in p["join"]["to"]]
    card = cardinality or p["cardinality"]
    domain, rng = p["domain"], p["range"]
    keep = {k: v for k, v in p.items() if k in ("description", "synonyms")}
    delete_relationship(onto, pid)
    return add_relationship(onto, domain, rng, from_cols, to_cols, card,
                            name=new_name or pid, **keep)


def repoint(onto: dict, pid: str, end: str, new_class: str) -> str:
    """Move one end of a relationship to another class. end: 'source' | 'target'."""
    pid = forward_pid(onto, pid)
    p = onto["object_properties"][pid]
    domain, rng = p["domain"], p["range"]
    from_cols = [x.rpartition(".")[2] for x in p["join"]["from"]]
    if end == "target":
        rng, to_cols = new_class, None
    else:
        domain, from_cols, to_cols = new_class, None, [x.rpartition(".")[2] for x in p["join"]["to"]]
    card = p["cardinality"]
    keep = {k: v for k, v in p.items() if k in ("description", "synonyms")}
    delete_relationship(onto, pid)
    if to_cols is None:
        _, to_cols = guess_join_columns(onto, domain, rng)
    if from_cols is None:
        from_cols, _ = guess_join_columns(onto, domain, rng)
    return add_relationship(onto, domain, rng, from_cols, to_cols, card, **keep)


def accept_suggestion(onto: dict, sid: str) -> str | None:
    for s in onto.get("suggested_relationships", []):
        if s["id"] == sid:
            s["status"] = "accepted"
            pid = add_relationship(
                onto, s["domain"], s["range"], s["from_cols"], s["to_cols"], "many-to-one",
                provenance="discovered+manual", confidence=s.get("confidence"))
            onto["suggested_relationships"] = [x for x in onto["suggested_relationships"]
                                               if x["id"] != sid]
            return pid
    return None


def reject_suggestion(onto: dict, sid: str) -> None:
    for s in onto.get("suggested_relationships", []):
        if s["id"] == sid:
            s["status"] = "rejected"


# --------------------------------------------------------------------------- #
# classes / properties / metrics
# --------------------------------------------------------------------------- #
def rename_class(onto: dict, old: str, new: str) -> str:
    new = re.sub(r"\W+", "", new.strip()) or old
    if new == old:
        return old
    if new in onto["classes"]:
        raise ValueError(f"Class {new} already exists")
    onto["classes"] = {(new if k == old else k): v for k, v in onto["classes"].items()}
    onto["classes"][new]["label"] = label(new)
    for p in onto["object_properties"].values():
        for side in ("domain", "range"):
            if p.get(side) == old:
                p[side] = new
    for m in onto.get("metrics", []):
        if m.get("base_class") == old:
            m["base_class"] = new
    for r in onto.get("rules", []):
        r["applies_to"] = [new if x == old else x for x in r.get("applies_to", [])]
    for v in onto.get("value_meanings", []):
        if v.get("class") == old:
            v["class"] = new
    for s in onto.get("suggested_relationships", []):
        for side in ("domain", "range"):
            if s.get(side) == old:
                s[side] = new
    for e in onto.get("lineage", {}).get("tables", []):
        for side in ("source", "target"):
            if e.get(side) == old:
                e[side] = new
    for sn in onto.get("snippets", []):                  # learned snippets keep their text, re-point classes
        sn["classes"] = [new if x == old else x for x in sn.get("classes") or []]
    for pg in onto.get("pages", []):
        pg["links"] = [f"class:{new}" if x == f"class:{old}" else x for x in pg.get("links") or []]
    layout = onto.get("layout", {})
    if old in layout:
        layout[new] = layout.pop(old)
    refresh_index(onto)
    return new


def update_class(onto: dict, cid: str, *, role=None, description=None, synonyms=None) -> None:
    c = onto["classes"][cid]
    if role:
        c["role"] = role
    if description is not None:
        c["description"] = description
    if synonyms is not None:
        c["synonyms"] = [s.strip().lower() for s in synonyms if s.strip()]
    refresh_index(onto)


def update_property(onto: dict, cid: str, pid: str, *, semantic_role=None, aggregation=None,
                    description=None, synonyms=None, new_name=None) -> str:
    props = onto["classes"][cid]["data_properties"]
    p = props[pid]
    if semantic_role and semantic_role != "measure" and p.get("semantic_role") == "measure":
        col = p.get("source_column", pid).lower()
        onto["metrics"] = [m for m in onto.get("metrics", []) if not (
            m.get("provenance") == "heuristic" and m.get("base_class") == cid
            and re.fullmatch(rf"\w+\(\s*(distinct\s+)?{re.escape(col)}\s*\)", m.get("sql", "").lower()))]
    if semantic_role:
        p["semantic_role"] = semantic_role
        if semantic_role == "measure":
            p["aggregation"] = aggregation or p.get("aggregation") or "SUM"
        else:
            p.pop("aggregation", None)
    elif aggregation and p.get("semantic_role") == "measure":
        p["aggregation"] = aggregation
    if description is not None:
        p["description"] = description or None
    if synonyms is not None:
        p["synonyms"] = [s.strip().lower() for s in synonyms if s.strip()]
    if new_name and new_name != pid:
        new_name = camel(new_name)
        onto["classes"][cid]["data_properties"] = {(new_name if k == pid else k): v
                                                   for k, v in props.items()}
        ident = onto["classes"][cid].get("identifier", [])
        onto["classes"][cid]["identifier"] = [new_name if x == pid else x for x in ident]
        pid = new_name
    refresh_index(onto)
    return pid


def upsert_metric(onto: dict, name: str, base_class: str, sql: str, description: str = "",
                  synonyms: list[str] | None = None) -> None:
    name = re.sub(r"\W+", "_", name.strip()).strip("_").lower()
    old = next((m for m in onto.setdefault("metrics", []) if m["name"] == name), {})
    metrics = [m for m in onto["metrics"] if m["name"] != name]
    keep = {k: old[k] for k in ("domains", "owner") if old.get(k)}   # governance survives an edit
    metrics.append({"name": name, "description": description, "base_class": base_class,
                    "sql": sql, "synonyms": synonyms or [], "provenance": "manual", **keep,
                    "status": "draft"})
    onto["metrics"] = metrics
    refresh_index(onto)


def delete_metric(onto: dict, name: str) -> None:
    onto["metrics"] = [m for m in onto.get("metrics", []) if m["name"] != name]
    refresh_index(onto)
