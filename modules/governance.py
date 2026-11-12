"""
Curated ("modeled") side of the ontology: domains, certification and pages.

Data kept in the ontology dict (so it is saved with versions and travels through Ossie):

  domains   : [{name, parent, description, owner}]           parent makes it a subdomain
  classes.<C>.domains           : ["Sales", ...]              an asset can live in several domains
  classes.<C>.certification     : {status, owner, by, at}     status draft | certified | deprecated
  metrics[].domains / status / owner / certified_by / certified_at
  rules[].domains / status / owner
  pages     : [{id, title, body, domains, owner, status, links, provenance, updated_at, ...}]

Domains narrow the search space for Ask: a question scoped to one domain only sends that
domain's classes, metrics, rules, pages and snippets to the model.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from datetime import datetime, timezone

STATUSES = ["draft", "certified", "deprecated"]
TAG_DOMAIN = "ontology_domain"
TAG_STATUS = "ontology_certification"
TAG_OWNER = "ontology_owner"

# Words in table / class names that point at a business domain.
DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "Sales": ["order", "sale", "sales", "revenue", "quote", "cart", "transaction", "pos", "booking",
              "opportunity", "deal", "discount", "invoice_line", "line"],
    "Customer": ["customer", "client", "account", "contact", "member", "segment", "demographic",
                 "subscriber", "household", "loyalty"],
    "Product": ["product", "item", "category", "sku", "catalog", "brand", "model", "variant"],
    "Supply chain": ["supplier", "vendor", "inventory", "stock", "warehouse", "purchase", "procurement",
                     "shipment", "shipper", "freight", "logistics", "carrier", "delivery", "receipt"],
    "Finance": ["payment", "ledger", "gl", "budget", "cost", "expense", "invoice", "tax", "currency",
                "fiscal", "billing", "refund", "price", "pricing"],
    "People": ["employee", "staff", "hr", "payroll", "department", "job", "salary", "worker",
               "manager", "headcount"],
    "Marketing": ["campaign", "promotion", "promo", "channel", "click", "lead", "ad", "web",
                  "session", "visit", "impression", "survey"],
    "Geography": ["region", "territory", "country", "city", "address", "location", "geo", "store",
                  "state", "zip"],
    "Calendar": ["date", "calendar", "time", "period", "fiscal_period"],
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure(onto: dict) -> dict:
    """Add the governance keys an older ontology lacks (no-op when present)."""
    onto.setdefault("domains", [])
    onto.setdefault("pages", [])
    onto.setdefault("snippets", [])
    return onto


def _tokens(*names) -> list[str]:
    out = []
    for n in names:
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(n or ""))
        out += [w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if w]
    # singular forms too: orders -> order, categories -> category
    more = []
    for w in out:
        if w.endswith("ies"):
            more.append(w[:-3] + "y")
        elif w.endswith("s") and not w.endswith("ss"):
            more.append(w[:-1])
    return out + more


# --------------------------------------------------------------------------- #
# Domains
# --------------------------------------------------------------------------- #
def domain_names(onto: dict) -> list[str]:
    return [d["name"] for d in onto.get("domains", []) if d.get("name")]


def domain_path(onto: dict, name: str) -> str:
    """'Sales/Online' style path for display."""
    parents = {d["name"]: d.get("parent") for d in onto.get("domains", [])}
    parts, seen = [name], {name}
    while parents.get(parts[0]) and parents[parts[0]] not in seen:
        parts.insert(0, parents[parts[0]])
        seen.add(parts[0])
    return "/".join(parts)


def subdomains(onto: dict, name: str) -> set[str]:
    """The domain and every domain below it."""
    kids: dict[str, list[str]] = {}
    for d in onto.get("domains", []):
        if d.get("parent"):
            kids.setdefault(d["parent"], []).append(d["name"])
    out, stack = set(), [name]
    while stack:
        n = stack.pop()
        if n not in out:
            out.add(n)
            stack += kids.get(n, [])
    return out


def upsert_domain(onto: dict, name: str, parent: str | None = None, description: str = "",
                  owner: str = "") -> None:
    name = name.strip()
    if not name:
        raise ValueError("A domain needs a name.")
    if parent == name:
        raise ValueError("A domain cannot be its own parent.")
    ensure(onto)
    for d in onto["domains"]:
        if d["name"] == name:
            d.update({"parent": parent or None, "description": description or d.get("description"),
                      "owner": owner or d.get("owner")})
            return
    onto["domains"].append({"name": name, "parent": parent or None, "description": description or None,
                            "owner": owner or None})


def delete_domain(onto: dict, name: str) -> None:
    onto["domains"] = [d for d in onto.get("domains", []) if d["name"] != name]
    for d in onto["domains"]:
        if d.get("parent") == name:
            d["parent"] = None
    for a in _all_assets(onto):
        if name in (a.get("domains") or []):
            a["domains"] = [x for x in a["domains"] if x != name]


def rename_domain(onto: dict, old: str, new: str) -> None:
    new = new.strip()
    if not new or new == old:
        return
    if new in domain_names(onto):
        raise ValueError(f"Domain '{new}' already exists.")
    for d in onto.get("domains", []):
        if d["name"] == old:
            d["name"] = new
        if d.get("parent") == old:
            d["parent"] = new
    for a in _all_assets(onto):
        if old in (a.get("domains") or []):
            a["domains"] = [new if x == old else x for x in a["domains"]]


def _all_assets(onto: dict):
    yield from onto.get("classes", {}).values()
    for k in ("metrics", "rules", "pages", "snippets"):
        yield from onto.get(k, [])


def suggest_domains(onto: dict, overwrite: bool = False, max_per_class: int = 2) -> dict[str, list[str]]:
    """Keyword heuristic on class / table names and descriptions, then neighbours: a class
    with no keyword hit takes the domain most of its related classes have. Returns
    {class: [domains]} and writes them (only to classes without domains unless overwrite)."""
    ensure(onto)
    classes = onto.get("classes", {})
    guess: dict[str, list[str]] = {}
    for cid, c in classes.items():
        words = _tokens(cid, c.get("source_table"), c.get("label"))
        desc = _tokens(c.get("description"))
        score = Counter()
        for dom, kws in DOMAIN_KEYWORDS.items():
            for kw in kws:
                if kw in words:
                    score[dom] += 3
                if kw in desc:
                    score[dom] += 1
        if score:
            best = score.most_common(1)[0][1]
            guess[cid] = [d for d, s in score.most_common(max_per_class) if s >= max(2, best * 0.6)]
        else:
            guess[cid] = []
    # facts belong to the domains of what they measure: add the top domain of their dimensions
    nbrs: dict[str, list[str]] = {c: [] for c in classes}
    for p in onto.get("object_properties", {}).values():
        if p.get("domain") in nbrs and p.get("range") in nbrs:
            nbrs[p["domain"]].append(p["range"])
            nbrs[p["range"]].append(p["domain"])
    for cid in classes:
        if not guess[cid]:
            votes = Counter(d for n in nbrs[cid] for d in guess.get(n, []))
            if votes:
                guess[cid] = [votes.most_common(1)[0][0]]
    for cid, doms in guess.items():
        c = classes[cid]
        if doms and (overwrite or not c.get("domains")):
            c["domains"] = doms
            for d in doms:
                if d not in domain_names(onto):
                    upsert_domain(onto, d)
    inherit_metric_domains(onto)
    return guess


def inherit_metric_domains(onto: dict, overwrite: bool = False) -> None:
    """Metrics and rules without domains take the domains of their base class(es). Inherited domains
    (marked `domains_inherited`) follow the class when it moves; explicitly assigned ones are kept."""
    classes = onto.get("classes", {})
    for m in onto.get("metrics", []):
        if overwrite or not m.get("domains") or m.get("domains_inherited"):
            doms = classes.get(m.get("base_class"), {}).get("domains") or []
            if doms or m.get("domains_inherited"):
                m["domains"], m["domains_inherited"] = list(doms), True
    for r in onto.get("rules", []):
        if overwrite or not r.get("domains") or r.get("domains_inherited"):
            doms = []
            for cid in r.get("applies_to") or []:
                doms += [d for d in classes.get(cid, {}).get("domains") or [] if d not in doms]
            if doms or r.get("domains_inherited"):
                r["domains"], r["domains_inherited"] = doms, True


def set_class_domains(onto: dict, cid: str, domains: list[str]) -> None:
    ensure(onto)
    doms = [d.strip() for d in domains if d and d.strip()]
    for d in doms:
        if d not in domain_names(onto):
            upsert_domain(onto, d)
    onto["classes"][cid]["domains"] = list(dict.fromkeys(doms))


def in_domain(asset: dict, domain_set: set[str]) -> bool:
    return bool(set(asset.get("domains") or []) & domain_set)


def assets_in(onto: dict, domain: str, include_sub: bool = True) -> dict[str, list]:
    ds = subdomains(onto, domain) if include_sub else {domain}
    return {
        "classes": [cid for cid, c in onto.get("classes", {}).items() if in_domain(c, ds)],
        "metrics": [m for m in onto.get("metrics", []) if in_domain(m, ds)],
        "rules": [r for r in onto.get("rules", []) if in_domain(r, ds)],
        "pages": [p for p in onto.get("pages", []) if in_domain(p, ds)],
        "snippets": [s for s in onto.get("snippets", []) if in_domain(s, ds)],
    }


def unassigned_classes(onto: dict) -> list[str]:
    return [cid for cid, c in onto.get("classes", {}).items() if not c.get("domains")]


def scope(onto: dict, domain: str | None, hops: int = 1) -> dict:
    """A copy of the ontology narrowed to one domain (plus its subdomains). Classes one join
    away are kept so joins still resolve, but their metrics/pages are not. None = everything."""
    if not domain:
        return onto
    ds = subdomains(onto, domain)
    core = {cid for cid, c in onto["classes"].items() if in_domain(c, ds)}
    keep, frontier = set(core), set(core)
    for _ in range(hops):                       # follow forward joins (key holder -> referenced class)
        nxt = {p["range"] for p in onto.get("object_properties", {}).values()
               if p.get("join") and p.get("cardinality") != "one-to-many"
               and p.get("domain") in frontier and p.get("range") in onto["classes"]}
        frontier = nxt - keep
        keep |= nxt
    out = copy.deepcopy({k: v for k, v in onto.items() if k not in ("classes", "object_properties")})
    out["classes"] = {cid: copy.deepcopy(c) for cid, c in onto["classes"].items() if cid in keep}
    out["object_properties"] = {pid: copy.deepcopy(p) for pid, p in onto.get("object_properties", {}).items()
                                if p.get("domain") in keep and p.get("range") in keep}
    out["metrics"] = [m for m in out.get("metrics", [])
                      if in_domain(m, ds) or (not m.get("domains") and m.get("base_class") in core)]
    out["rules"] = [r for r in out.get("rules", [])
                    if in_domain(r, ds) or (not r.get("domains") and set(r.get("applies_to") or []) & core)]
    out["value_meanings"] = [v for v in out.get("value_meanings", []) if v.get("class") in keep]
    out["pages"] = [p for p in out.get("pages", []) if in_domain(p, ds) or not p.get("domains")]
    out["snippets"] = [s for s in out.get("snippets", [])
                       if in_domain(s, ds) or set(s.get("classes") or []) & core]
    out["scope"] = {"domain": domain, "domains": sorted(ds), "core_classes": sorted(core)}
    return out


# --------------------------------------------------------------------------- #
# Certification (classes, metrics, rules, pages)
# --------------------------------------------------------------------------- #
def status_of(onto: dict, kind: str, key: str) -> str:
    a = find_asset(onto, kind, key)
    if a is None:
        return "draft"
    if kind == "class":
        return (a.get("certification") or {}).get("status", "draft")
    return a.get("status", "draft")


def find_asset(onto: dict, kind: str, key: str) -> dict | None:
    if kind == "class":
        return onto.get("classes", {}).get(key)
    coll = {"metric": "metrics", "rule": "rules", "page": "pages", "snippet": "snippets"}[kind]
    field = "id" if kind in ("page", "snippet") else "name"
    return next((x for x in onto.get(coll, []) if x.get(field) == key), None)


def certify(onto: dict, kind: str, key: str, status: str, owner: str | None = None,
            by: str | None = None, domains: list[str] | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    a = find_asset(onto, kind, key)
    if a is None:
        raise KeyError(f"{kind} '{key}' not found")
    stamp = {"status": status, "owner": owner or None,
             "certified_by": by if status == "certified" else None,
             "certified_at": now() if status == "certified" else None}
    if kind == "class":
        cur = a.get("certification") or {}
        a["certification"] = {**cur, **{k: v for k, v in stamp.items() if v is not None or k == "status"}}
        if status != "certified":
            a["certification"].pop("certified_by", None)
            a["certification"].pop("certified_at", None)
    else:
        for k, v in stamp.items():
            if v is not None or k == "status":
                a[k] = v
            elif k in ("certified_by", "certified_at"):
                a.pop(k, None)
    if domains is not None:
        if kind == "class":
            set_class_domains(onto, key, domains)
        else:
            for d in domains:
                if d not in domain_names(onto):
                    upsert_domain(onto, d)
            a["domains"] = list(dict.fromkeys(domains))
            if kind in ("metric", "rule") and a.get("domains_inherited"):
                base = find_asset(onto, "class", a.get("base_class") or "") or {}
                if kind == "rule" or a["domains"] != (base.get("domains") or []):
                    a.pop("domains_inherited", None)     # explicitly assigned now


def governance_stats(onto: dict) -> dict:
    classes = onto.get("classes", {}).values()
    cert = lambda xs: sum(1 for x in xs if x.get("status") == "certified")  # noqa: E731
    return {
        "domains": len(onto.get("domains", [])),
        "certified_classes": sum(1 for c in classes if (c.get("certification") or {}).get("status") == "certified"),
        "certified_metrics": cert(onto.get("metrics", [])),
        "pages": len(onto.get("pages", [])),
        "certified_pages": cert(onto.get("pages", [])),
        "snippets": len([s for s in onto.get("snippets", []) if s.get("status") != "dismissed"]),
    }


# --------------------------------------------------------------------------- #
# Pages: governed wiki pages (the qualitative counterpart of metric views)
# --------------------------------------------------------------------------- #
def _page_id(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "page"
    pid, i = base, 2
    while pid in taken:
        pid, i = f"{base}-{i}", i + 1
    return pid


def upsert_page(onto: dict, title: str, body: str, domains: list[str] | None = None, owner: str = "",
                status: str = "draft", links: list[str] | None = None, provenance: str = "manual",
                page_id: str | None = None, synonyms: list[str] | None = None, by: str | None = None) -> str:
    ensure(onto)
    if not title.strip():
        raise ValueError("A page needs a title.")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    pages = onto["pages"]
    cur = next((p for p in pages if p["id"] == page_id), None) if page_id else None
    if cur is None:
        page_id = _page_id(title, {p["id"] for p in pages})
        cur = {"id": page_id, "created_at": now()}
        pages.append(cur)
    for d in domains or []:
        if d not in domain_names(onto):
            upsert_domain(onto, d)
    cur.update({"title": title.strip(), "body": body, "domains": list(dict.fromkeys(domains or [])),
                "owner": owner or None, "status": status, "links": list(dict.fromkeys(links or [])),
                "synonyms": [s.strip().lower() for s in synonyms or [] if s.strip()],
                "provenance": cur.get("provenance") if provenance == "manual" and cur.get("provenance") else provenance,
                "updated_at": now()})
    if status == "certified":
        cur["certified_by"], cur["certified_at"] = by, now()
    else:
        cur.pop("certified_by", None)
        cur.pop("certified_at", None)
    return page_id


def delete_page(onto: dict, page_id: str) -> None:
    onto["pages"] = [p for p in onto.get("pages", []) if p["id"] != page_id]


def pages_markdown(onto: dict) -> dict[str, str]:
    """{file name: markdown with front matter} for every page (export)."""
    import yaml
    out = {}
    for p in onto.get("pages", []):
        front = {k: p.get(k) for k in ("title", "domains", "owner", "status", "links", "synonyms",
                                       "provenance", "certified_by", "certified_at", "updated_at") if p.get(k)}
        out[f"{p['id']}.md"] = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + (p.get("body") or "")
    return out


def page_template(title: str, links: list[str], onto: dict) -> str:
    """A starting point for a new page when no LLM draft is available."""
    lines = [f"# {title}", "", "## What it means", "", "_Explain the business concept in plain words._", "",
             "## How to calculate it", ""]
    for m in onto.get("metrics", []):
        if f"metric:{m['name']}" in links:
            lines.append(f"- **{m['name']}** = `{m.get('sql')}` ({m.get('base_class')})")
    lines += ["", "## Watch out for", "", "- _Edge cases, exclusions, common mistakes._", "",
              "## Related", ""]
    lines += [f"- {x}" for x in links]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Unity Catalog tags: write domains / certification back, read them in
# --------------------------------------------------------------------------- #
def _lit(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def uc_tag_statements(onto: dict) -> list[str]:
    """ALTER TABLE … SET TAGS for every class with a 3-part source and domains or a status."""
    out = []
    for cid, c in onto.get("classes", {}).items():
        fq = c.get("source_fqn")
        if not fq or fq.count(".") != 2:
            continue
        tags = {}
        if c.get("domains"):
            tags[TAG_DOMAIN] = ",".join(c["domains"])[:250]
        cert = c.get("certification") or {}
        if cert.get("status"):
            tags[TAG_STATUS] = cert["status"]
        if cert.get("owner"):
            tags[TAG_OWNER] = cert["owner"][:250]
        if tags:
            name = ".".join(f"`{p}`" for p in fq.split("."))
            kind = "VIEW" if c.get("source_kind") == "view" else "TABLE"
            out.append(f"ALTER {kind} {name} SET TAGS ("
                       + ", ".join(f"{_lit(k)} = {_lit(v)}" for k, v in tags.items()) + ")")
    return out


def read_uc_tags(run, full_names: list[str]) -> dict[str, dict]:
    """{fqn: {tag: value}} for the ontology_* tags (and a plain `domain` tag) on the given tables."""
    out: dict[str, dict] = {}
    by_cat: dict[str, list[str]] = {}
    for fq in full_names:
        by_cat.setdefault(fq.split(".")[0], []).append(fq)
    for cat, fqs in by_cat.items():
        if not re.match(r"^[A-Za-z0-9_\-]+$", cat):
            continue
        params = {f"p{i}": fq.lower() for i, fq in enumerate(fqs)}
        marks = ", ".join(f":{k}" for k in params)
        try:
            df = run(f"SELECT catalog_name, schema_name, table_name, tag_name, tag_value "
                     f"FROM `{cat}`.information_schema.table_tags "
                     f"WHERE lower(concat_ws('.', catalog_name, schema_name, table_name)) IN ({marks}) "
                     f"AND tag_name IN ('{TAG_DOMAIN}', '{TAG_STATUS}', '{TAG_OWNER}', 'domain')", params)
        except Exception:  # noqa: BLE001
            continue
        for r in df.itertuples(index=False):
            fq = f"{r.catalog_name}.{r.schema_name}.{r.table_name}".lower()
            out.setdefault(fq, {})[r.tag_name] = r.tag_value
    return out


def apply_uc_tags(onto: dict, tags: dict[str, dict]) -> int:
    """Seed class domains / certification from UC tags. Returns the number of classes updated."""
    n = 0
    for cid, c in onto.get("classes", {}).items():
        t = tags.get(str(c.get("source_fqn") or "").lower())
        if not t:
            continue
        doms = t.get(TAG_DOMAIN) or t.get("domain")
        if doms:
            set_class_domains(onto, cid, [d for d in doms.split(",") if d.strip()])
        if t.get(TAG_STATUS) in STATUSES:
            c["certification"] = {"status": t[TAG_STATUS], "owner": t.get(TAG_OWNER), "source": "unity-catalog"}
        n += 1
    inherit_metric_domains(onto)
    return n
