"""
Review queue: knowledge the studio proposes an admin should promote, certify or explain.

Signals (each item says which one fired and why):
  popular-snippet   heavily used learned snippets that are not modeled yet
                    -> promote to a metric / rule / relationship, or draft a page
  conflict          one business name with several definitions (e.g. two "revenue" formulas)
                    -> write a page that settles it, certify the right one
  uncertified       metrics / rules / pages that Ask keeps using but nobody certified
  negative-feedback 👎 / failed Ask requests clustered by domain and asset (knowledge hotspots)
  knowledge-gap     questions whose words match nothing in the ontology
  unassigned        classes without a domain (they never show up in a domain-scoped search)
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import governance as gv
from . import ontology_edit as ed

STOP = set("a an and are as at by for from how in is it last me month of on or per show the this to "
           "top was were what which who with year years by vs versus than all each total number count "
           "list give many much my our".split())


def _norm_name(n: str | None) -> str:
    n = re.sub(r"[^a-z0-9]+", "_", str(n or "").lower()).strip("_")
    for pre in ("total_", "sum_", "net_", "gross_"):    # same concept; avg_ / count_ are different measures
        if n.startswith(pre):
            n = n[len(pre):]
    return n


def _modeled(onto: dict, s: dict) -> str | None:
    """Is the snippet already part of the modeled ontology? Returns where, else None."""
    if s["kind"] == "join":
        a, b = [x.strip() for x in s["text"].split("=")]
        pair = {a.split(".", 1)[1].lower() if "." in a else a.lower(),
                b.split(".", 1)[1].lower() if "." in b else b.lower()}
        ca, cb = a.split(".")[0], b.split(".")[0]
        for pid, p in onto.get("object_properties", {}).items():
            j = p.get("join") or {}
            if {p.get("domain"), p.get("range")} == {ca, cb}:
                cols = {x.rpartition(".")[2].lower() for x in j.get("from", []) + j.get("to", [])}
                if pair <= cols:
                    return f"relationship {pid}"
    if s["kind"] == "expression":
        norm = re.sub(r"\s+", "", (s.get("bare") or s["text"]).lower())
        for m in onto.get("metrics", []):
            if re.sub(r"\s+", "", str(m.get("sql", "")).lower()) == norm:
                return f"metric {m['name']}"
    if s["kind"] == "filter":
        norm = re.sub(r"\s+", "", (s.get("bare") or s["text"]).lower())
        for r in onto.get("rules", []):
            if norm and norm in re.sub(r"\s+", "", str(r.get("sql_hint", "")).lower()):
                return f"rule {r['name']}"
    return None


def build_queue(onto: dict, traces: list[dict] | None = None, min_authority: float = 0.5) -> list[dict]:
    traces = traces or []
    items: list[dict] = []
    dismissed = set(onto.get("review_dismissed") or [])

    # usage of assets by Ask
    used = Counter()
    for t in traces:
        a = t.get("assets") or {}
        for k, kind in (("metrics", "metric"), ("rules", "rule"), ("pages", "page")):
            for name in a.get(k) or []:
                used[(kind, name)] += 1

    # 1. popular learned snippets not yet modeled
    for s in onto.get("snippets", []):
        if s.get("status") in ("promoted", "dismissed") or s.get("authority", 0) < min_authority:
            continue
        where = _modeled(onto, s)
        if where:
            continue
        action = {"join": "relationship", "filter": "rule", "expression": "metric"}[s["kind"]]
        if s["kind"] == "expression" and len(s.get("classes") or []) != 1:
            action = "page"
        users = len(s.get("users") or {})
        items.append({
            "id": f"snip:{s['id']}", "type": "popular-snippet", "priority": round(s["authority"], 2),
            "title": f"Promote {s['kind']} " + (f"'{s['name']}'" if s.get("name") else s["text"][:60]),
            "why": f"Used {s['uses']}× by {users} people (authority {s['authority']:.2f})"
                   + (f"; comment: “{s['comments'][0]}”" if s.get("comments") else "")
                   + " — not in the modeled ontology yet.",
            "action": action, "snippet": s["id"], "domains": s.get("domains") or []})

    # 2. conflicts: one name, several definitions
    defs: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    def_domains: dict[str, list[str]] = defaultdict(list)
    for m in onto.get("metrics", []):
        if m.get("status") != "deprecated":
            defs[_norm_name(m["name"])].append((f"metric {m['name']}", str(m.get("sql")), 1.0))
            def_domains[_norm_name(m["name"])] += m.get("domains") or []
    for s in onto.get("snippets", []):
        if s["kind"] == "expression" and s.get("name") and s.get("status") not in ("dismissed", "promoted"):
            defs[_norm_name(s["name"])].append((f"snippet {s['name']}", s.get("bare") or s["text"],
                                                s.get("authority", 0)))
            def_domains[_norm_name(s["name"])] += s.get("domains") or []
    for name, ds in defs.items():
        formulas = {re.sub(r"[\s`\"]+", "", f.lower()) for _, f, _ in ds}
        if name and len(formulas) > 1:
            ds = sorted(ds, key=lambda x: -x[2])
            items.append({
                "id": f"conflict:{name}", "type": "conflict", "priority": 0.9,
                "title": f"Settle the definition of “{name.replace('_', ' ')}”",
                "why": f"{len(formulas)} different formulas: " + "; ".join(f"{w} = {f}" for w, f, _ in ds[:4]),
                "action": "page", "name": name, "definitions": [{"where": w, "sql": f, "authority": a}
                                                                for w, f, a in ds[:6]],
                "domains": list(dict.fromkeys(def_domains[name]))})

    # 3. uncertified definitions that Ask relies on (or that came from an LLM / heuristic)
    for kind, coll, key in (("metric", "metrics", "name"), ("rule", "rules", "name"), ("page", "pages", "id")):
        for a in onto.get(coll, []):
            if a.get("status") in ("certified", "deprecated"):
                continue
            label = a.get("title") if kind == "page" else a[key]
            n = used[(kind, label)]
            prov = str(a.get("provenance") or "")
            if n == 0 and not any(x in prov for x in ("llm", "learned", "review")):
                continue
            items.append({
                "id": f"cert:{kind}:{a[key]}", "type": "uncertified", "priority": round(min(0.85, 0.4 + 0.1 * n), 2),
                "title": f"Certify {kind} '{label}'",
                "why": (f"Used in {n} Ask request(s); " if n else "") + f"provenance: {prov or 'unknown'}, "
                       "not reviewed by a domain expert yet.",
                "action": "certify", "kind": kind, "key": a[key], "domains": a.get("domains") or []})

    # 4. negative feedback / failures clustered by domain + class
    bad = [t for t in traces if t.get("mode", "ontology") == "ontology"
           and (t.get("feedback") == "down" or t.get("status") == "error")]
    clusters: dict[tuple, list] = defaultdict(list)
    for t in bad:
        a = t.get("assets") or {}
        cls = tuple(sorted(a.get("sql_classes") or [])[:3])      # classes the generated SQL touched
        clusters[(t.get("domain") or "All", cls)].append(t)
    for (dom, cls), ts in clusters.items():
        ts = list({t["question"].strip().lower(): t for t in ts}.values())
        items.append({
            "id": f"neg:{dom}:{'|'.join(cls)}", "type": "negative-feedback",
            "priority": round(min(0.95, 0.5 + 0.15 * len(ts)), 2),
            "title": f"{len(ts)} unhelpful answer(s) in {dom}" + (f" around {', '.join(cls)}" if cls else ""),
            "why": "Questions: " + " | ".join(f"“{t['question']}”" for t in ts[:4])
                   + ". Write a page or fix the definitions they rely on.",
            "action": "page", "questions": [t["question"] for t in ts[:10]], "classes": list(cls),
            "domains": [] if dom == "All" else [dom]})

    # 5. knowledge gaps: question words that match nothing
    vocab = set()
    for term in onto.get("synonyms_index", {}):
        vocab.update(term.split())
    for cid in onto.get("classes", {}):
        vocab.update(w.lower() for w in re.findall(r"[A-Za-z][a-z]+", cid))
    for p in onto.get("pages", []):
        vocab.update(w.lower() for w in re.findall(r"[a-z]{3,}", p.get("title", "").lower()))
        vocab.update(p.get("synonyms") or [])
    gaps = Counter()
    asked = defaultdict(list)
    for t in traces:
        for w in set(re.findall(r"[a-z]{4,}", str(t.get("question", "")).lower())):
            w1 = w[:-1] if w.endswith("s") else w
            if w not in STOP and w not in vocab and w1 not in vocab:
                gaps[w] += 1
                asked[w].append(t["question"])
    for w, n in gaps.most_common(8):
        if n >= 2:
            items.append({
                "id": f"gap:{w}", "type": "knowledge-gap", "priority": round(min(0.8, 0.3 + 0.1 * n), 2),
                "title": f"Nothing in the ontology explains “{w}”",
                "why": f"Asked {n}×: " + " | ".join(f"“{q}”" for q in asked[w][:3])
                       + ". Add a synonym, or a page that defines it.",
                "action": "page", "term": w, "questions": asked[w][:10], "domains": []})

    # 6. classes without a domain
    un = gv.unassigned_classes(onto)
    if un and onto.get("domains"):
        items.append({
            "id": "unassigned", "type": "unassigned", "priority": 0.35,
            "title": f"{len(un)} class(es) have no domain",
            "why": ", ".join(un[:12]) + " — they are skipped by domain-scoped questions.",
            "action": "domains", "classes": un, "domains": []})

    items = [i for i in items if i["id"] not in dismissed]
    items.sort(key=lambda i: -i["priority"])
    return items


def dismiss(onto: dict, item_id: str) -> None:
    onto.setdefault("review_dismissed", [])
    if item_id not in onto["review_dismissed"]:
        onto["review_dismissed"].append(item_id)
    if item_id.startswith("snip:"):
        s = gv.find_asset(onto, "snippet", item_id[5:])
        if s:
            s["status"] = "dismissed"


# --------------------------------------------------------------------------- #
# Promotion of learned snippets into modeled assets
# --------------------------------------------------------------------------- #
def promote(onto: dict, snippet_id: str, as_kind: str, name: str | None = None,
            description: str = "", by: str | None = None) -> str:
    """Turn a learned snippet into a modeled metric / rule / relationship. Returns a message."""
    s = gv.find_asset(onto, "snippet", snippet_id)
    if s is None:
        raise KeyError(snippet_id)
    note = description or (s.get("comments") or [""])[0]
    if not s.get("domains"):
        s["domains"] = list(dict.fromkeys(d for c in s.get("classes") or []
                                          for d in onto["classes"].get(c, {}).get("domains") or []))
    if as_kind == "metric":
        if len(s.get("classes") or []) != 1:
            raise ValueError("Only an expression over one class can become a metric; draft a page instead.")
        mname = name or s.get("name") or "learned_metric"
        ed.upsert_metric(onto, mname, s["classes"][0], s.get("bare") or s["text"], note,
                         synonyms=[k.replace("_", " ") for k in (s.get("names") or {})])
        m = next(x for x in onto["metrics"] if x["name"] == re.sub(r"\W+", "_", mname.strip()).strip("_").lower())
        m.update({"provenance": "learned", "domains": s.get("domains") or [], "status": "draft",
                  "learned_from": snippet_id})
        target = f"metric:{m['name']}"
    elif as_kind == "rule":
        rname = re.sub(r"\W+", "_", (name or s["text"][:40]).lower()).strip("_")
        onto.setdefault("rules", []).append({
            "name": rname, "description": note or f"Filter people apply: {s['text']}",
            "applies_to": s.get("classes") or [], "sql_hint": s["text"], "synonyms": [],
            "provenance": "learned", "domains": s.get("domains") or [], "status": "draft",
            "learned_from": snippet_id})
        target = f"rule:{rname}"
    elif as_kind == "relationship":
        a, b = [x.strip() for x in s["text"].split("=")]
        (ca, cola), (cb, colb) = a.split(".", 1), b.split(".", 1)
        # the side whose column is its class key is the referenced ("to") side
        key_a = cola.lower() in [k.lower() for k in ed.class_key(onto, ca)]
        key_b = colb.lower() in [k.lower() for k in ed.class_key(onto, cb)]
        if key_a and not key_b:
            (ca, cola), (cb, colb) = (cb, colb), (ca, cola)
        pid = ed.add_relationship(onto, ca, cb, [cola], [colb], provenance="learned",
                                  confidence=round(s.get("authority", 0), 2))
        target = f"relationship:{pid}"
    else:
        raise ValueError(as_kind)
    s["status"], s["promoted_to"] = "promoted", target
    if by:
        s["promoted_by"] = by
    return f"Promoted to {target}"


# --------------------------------------------------------------------------- #
# Page drafts
# --------------------------------------------------------------------------- #
DRAFT_PROMPT = """You write a short governed wiki page (Markdown) for a company's data ontology.
It must help both people and a Text-to-SQL agent use the data correctly. Sections:
## What it means, ## How to calculate it (SQL over the given classes/columns only),
## Watch out for, ## Related. Be concrete and brief (under 250 words). No preamble."""


def draft_page(onto: dict, item: dict, use_llm: bool = False) -> tuple[str, str, list[str]]:
    """(title, markdown body, links) for a review item that asks for a page."""
    links: list[str] = []
    if item["type"] == "conflict":
        title = f"Definition of {item['name'].replace('_', ' ')}"
        facts = "\n".join(f"- {d['where']} (authority {d['authority']:.2f}): `{d['sql']}`" for d in item["definitions"])
        links = [d["where"].replace(" ", ":", 1) for d in item["definitions"]]
    elif item["type"] == "popular-snippet":
        s = gv.find_asset(onto, "snippet", item["snippet"])
        title = (s.get("name") or s["text"][:50]).replace("_", " ").strip().capitalize()
        facts = f"- Learned {s['kind']}: `{s['text']}`\n- Used {s['uses']}× by {len(s.get('users') or {})} people" + \
            "".join(f"\n- Author note: {c}" for c in s.get("comments") or [])
        links = [f"class:{c}" for c in s.get("classes") or []]
    else:
        title = (item.get("term") or (item.get("classes") or ["Questions"])[0]).replace("_", " ").capitalize()
        facts = "Questions people asked that the ontology answered badly:\n" + \
            "\n".join(f"- {q}" for q in item.get("questions") or [])
        links = [f"class:{c}" for c in item.get("classes") or []]
    if use_llm:
        try:
            from .ask import ontology_context
            from .llm_enrich import chat
            scoped = gv.scope(onto, (item.get("domains") or [None])[0])
            body = chat(DRAFT_PROMPT, f"Page title: {title}\nEvidence:\n{facts}\n\nOntology:\n"
                        + ontology_context(scoped)[:20000], max_tokens=900)
            return title, re.sub(r"^```(?:markdown|md)?|```$", "", body.strip(), flags=re.M).strip(), links
        except Exception:  # noqa: BLE001
            pass
    body = gv.page_template(title, links, onto)
    body = body.replace("_Explain the business concept in plain words._",
                        "_Explain the business concept in plain words._\n\n**Evidence**\n\n" + facts)
    return title, body, links
