"""
Knowledge and interop: business documents as cited evidence in the model, full-text search, typed relations
between pages, an Obsidian vault export, and opening auto-ontology MCP exports.

Adapted from auto-ontology-mcp (github.com/gogoymh/auto-ontology-mcp, Apache-2.0: save_passages, search_text,
get_source, save_concept, export_wiki, export_ontology / import_ontology), which ports NVIDIA Auto Ontology
(Apache-2.0). See NOTICE. Everything is kept in the model, so it is versioned and travels through Ossie.

  split_document / suggest_links / add_source   cut a document into passages linked to classes, metrics, pages
  source_view / delete_source / delete_passage  what a document contributed
  search_text                                    ranked (BM25) search over passages, pages and sources
  RELATIONS / set_page_relations                 typed links between pages and to classes / metrics
  obsidian_vault / vault_zip                     the model as Obsidian notes ([[links]], aliases, index, log)
  is_auto_ontology / from_auto_ontology          an auto-ontology export (format auto-ontology-mcp/v1) as Ossie
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone

import yaml

from .answering import tokens

RELATIONS = ["is_a", "part_of", "has_part", "instance_of", "related_to", "depends_on", "causes", "used_for",
             "contrasts_with", "same_as", "example_of"]
MAX_PASSAGE = 2000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id(prefix: str, taken: set, text: str) -> str:
    base = f"{prefix}-" + (re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "x")
    out, i = base, 2
    while out in taken:
        out, i = f"{base}-{i}", i + 1
    return out


# --------------------------------------------------------------------------- #
# Sources and passages
# --------------------------------------------------------------------------- #
def split_document(name: str, text: str, max_chars: int = 1200) -> list[dict]:
    """Passages of a document: Markdown by heading, CSV by groups of rows, anything else by paragraphs, each
    with its heading or locator in the original."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out = []
    if name.lower().endswith(".csv") and lines and "," in lines[0]:
        head = [h.strip() for h in lines[0].split(",")]
        rows = [ln for ln in lines[1:] if ln.strip()]
        for i in range(0, len(rows), 15):
            body = "\n".join("; ".join(f"{h}: {v.strip()}" for h, v in zip(head, r.split(",")) if v.strip())
                             for r in rows[i:i + 15])
            out.append({"heading": None, "locator": f"rows {i + 1}-{min(i + 15, len(rows))}", "text": body})
        return out
    heading, start, buf = None, 1, []

    def flush(end_line: int):
        body = "\n".join(buf).strip()
        if body:
            for k in range(0, len(body), max_chars):              # long sections in max_chars pieces
                out.append({"heading": heading, "locator": f"line {start}", "text": body[k:k + max_chars]})
        buf.clear()

    md = name.lower().endswith((".md", ".markdown"))
    for n, ln in enumerate(lines, 1):
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", ln) if md else None
        if m:
            flush(n)
            heading, start = m.group(2).strip(), n
        elif not md and not ln.strip() and sum(len(x) for x in buf) >= max_chars // 2:
            flush(n)
            start = n + 1
        else:
            buf.append(ln)
    flush(len(lines))
    return out


def suggest_links(onto: dict, text: str) -> list[str]:
    """class:/metric:/page: links for the model items a passage names (by name, label, table or synonym)."""
    low = f" {re.sub(r'[^a-z0-9]+', ' ', str(text).lower())} "

    def hit(*names) -> bool:
        return any(n and len(n) > 2 and f" {re.sub(r'[^a-z0-9]+', ' ', n.lower()).strip()} " in low for n in names)
    links = [f"class:{cid}" for cid, c in onto.get("classes", {}).items()
             if hit(cid, c.get("label"), c.get("source_table"), *(c.get("synonyms") or []))]
    links += [f"metric:{m['name']}" for m in onto.get("metrics", [])
              if hit(m["name"].replace("_", " "), *(m.get("synonyms") or []))]
    links += [f"page:{p['id']}" for p in onto.get("pages", []) if hit(p.get("title"), *(p.get("synonyms") or []))]
    return links


def add_source(onto: dict, title: str, passages: list[dict], kind: str = "document", uri: str | None = None,
               summary: str | None = None, by: str | None = None) -> str:
    """Register a source (or reuse the one with the same title or uri) and add its passages; a passage with the
    same text from the same source is not added twice. Returns the source id."""
    sources, plist = onto.setdefault("sources", []), onto.setdefault("passages", [])
    cur = next((s for s in sources if (uri and s.get("uri") == uri) or s.get("title") == title), None)
    if cur is None:
        cur = {"id": _id("src", {s["id"] for s in sources}, title), "title": title, "kind": kind, "added_at": _now()}
        sources.append(cur)
    cur.update({k: v for k, v in (("uri", uri), ("summary", summary), ("by", by)) if v})
    have = {(p["source"], p["text"]) for p in plist}
    taken = {p["id"] for p in plist}
    for p in passages:
        text = str(p.get("text") or "").strip()[:MAX_PASSAGE]
        if not text or (cur["id"], text) in have:
            continue
        pid = _id("psg", taken, f"{cur['id']} {p.get('heading') or text[:30]}")
        taken.add(pid)
        have.add((cur["id"], text))
        plist.append({"id": pid, "source": cur["id"], "text": text, "heading": p.get("heading"),
                      "locator": p.get("locator"), "kind": p.get("kind") or "quote",
                      "links": list(dict.fromkeys(p.get("links") or suggest_links(onto, text)))})
    return cur["id"]


def delete_source(onto: dict, sid: str) -> None:
    onto["sources"] = [s for s in onto.get("sources") or [] if s["id"] != sid]
    onto["passages"] = [p for p in onto.get("passages") or [] if p["source"] != sid]
    for pg in onto.get("pages", []):
        if sid in (pg.get("sources") or []):
            pg["sources"] = [x for x in pg["sources"] if x != sid]


def delete_passage(onto: dict, pid: str) -> None:
    onto["passages"] = [p for p in onto.get("passages") or [] if p["id"] != pid]


def source_view(onto: dict, sid: str) -> dict:
    """One source with everything taken from it: its passages and the pages that cite it."""
    src = next((s for s in onto.get("sources") or [] if s["id"] == sid), None)
    if src is None:
        raise KeyError(sid)
    return {"source": src, "passages": [p for p in onto.get("passages") or [] if p["source"] == sid],
            "pages": [pg["title"] for pg in onto.get("pages", []) if sid in (pg.get("sources") or [])]}


# --------------------------------------------------------------------------- #
# Full-text search (BM25 over passages, page bodies and sources)
# --------------------------------------------------------------------------- #
def _docs(onto: dict) -> list[dict]:
    titles = {s["id"]: s["title"] for s in onto.get("sources") or []}
    out = [{"kind": "passage", "id": p["id"], "title": titles.get(p["source"], p["source"]) + (
        f" › {p['heading']}" if p.get("heading") else ""), "text": p["text"], "extra": p.get("heading") or "",
        "locator": p.get("locator"), "links": p.get("links") or []} for p in onto.get("passages") or []]
    out += [{"kind": "page", "id": pg["id"], "title": pg["title"], "text": pg.get("body") or "", "extra": pg["title"],
             "links": [], "locator": None} for pg in onto.get("pages", []) if pg.get("status") != "deprecated"]
    out += [{"kind": "source", "id": s["id"], "title": s["title"], "text": s.get("summary") or "", "extra": s["title"],
             "links": [], "locator": s.get("uri")} for s in onto.get("sources") or []]
    return out


def _snippet(text: str, words: set, width: int = 220) -> str:
    """The text around the first matching word, matching words in bold."""
    found = [m for m in re.finditer(r"\w+", text) if (tokens(m.group(0)) or [""])[0] in words]
    start = max(0, found[0].start() - width // 3) if found else 0
    piece = text[start:start + width]
    for w in sorted({m.group(0) for m in found}, key=len, reverse=True):
        piece = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", f"**{w}**", piece)
    return ("…" if start else "") + piece.replace("\n", " ") + ("…" if start + width < len(text) else "")


def search_text(onto: dict, query: str, kinds: list[str] | None = None, limit: int = 10) -> list[dict]:
    """Ranked full-text hits (BM25, k1 1.2, b 0.75) with the matching words in bold."""
    q = set(tokens(query))
    docs = [d for d in _docs(onto) if not kinds or d["kind"] in kinds]
    if not q or not docs:
        return []
    toks = [tokens(f"{d['extra']} {d['text']}") for d in docs]
    avg = sum(len(t) for t in toks) / len(toks) or 1
    df = Counter(w for t in toks for w in set(t))
    hits = []
    for d, t in zip(docs, toks):
        tf = Counter(t)
        s = sum(math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5)) * tf[w] * 2.2
                / (tf[w] + 1.2 * (0.25 + 0.75 * len(t) / avg)) for w in q if tf[w])
        if s > 0:
            hits.append({"kind": d["kind"], "id": d["id"], "title": d["title"], "score": round(s, 3),
                         "snippet": _snippet(d["text"] or d["title"], q), "locator": d["locator"], "links": d["links"],
                         "text": d["text"]})
    return sorted(hits, key=lambda h: -h["score"])[:limit]


def set_page_relations(onto: dict, page_id: str, relations: list[dict], sources: list[str]) -> None:
    """Typed links from a page (is_a, part_of, related_to …) to pages, classes or metrics, and its sources."""
    pg = next(p for p in onto.get("pages", []) if p["id"] == page_id)
    rels = [{"relation": r["relation"], "target": r["target"]} for r in relations or []
            if r.get("relation") in RELATIONS and r.get("target")]
    pg["relations"] = list({(r["relation"], r["target"]): r for r in rels}.values())
    pg["sources"] = list(dict.fromkeys(sources or []))
    for k in ("relations", "sources"):
        if not pg[k]:
            pg.pop(k)


# --------------------------------------------------------------------------- #
# Obsidian vault
# --------------------------------------------------------------------------- #
def _fname(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", str(text)).strip()[:80] or "untitled"


def _front(d: dict) -> str:
    d = {k: v for k, v in d.items() if v not in (None, [], "")}
    return "---\n" + yaml.safe_dump(d, sort_keys=False, allow_unicode=True) + "---\n\n" if d else ""


def obsidian_vault(onto: dict) -> dict[str, str]:
    """{path: markdown}: one note per class, metric, page and source, linked with [[wikilinks]], plus index.md and
    log.md. Re-exporting rewrites the notes; edits made in Obsidian are not read back."""
    classes, ops = onto.get("classes", {}), onto.get("object_properties", {})
    pages = {p["id"]: p for p in onto.get("pages", [])}
    sources = {s["id"]: s for s in onto.get("sources") or []}
    names = {f"class:{c}": f"Classes/{_fname(c)}" for c in classes}
    names.update({f"metric:{m['name']}": f"Metrics/{_fname(m['name'])}" for m in onto.get("metrics", [])})
    names.update({f"page:{pid}": f"Pages/{_fname(p['title'])}" for pid, p in pages.items()})
    names.update({f"source:{sid}": f"Sources/{_fname(s['title'])}" for sid, s in sources.items()})

    def link(ref: str) -> str:
        return f"[[{names[ref]}|{names[ref].split('/', 1)[1]}]]" if ref in names else ref
    out = {}
    for cid, c in classes.items():
        props = c.get("data_properties") or {}
        lines = [f"# {cid}", "", c.get("description") or "_No description yet._", "", "## Columns", "",
                 "| column | type | description |", "|---|---|---|"]
        lines += [f"| {p.get('source_column') or pid} | {p.get('sql_type') or p.get('range') or ''} | "
                  f"{(p.get('description') or '').replace('|', '/')} |" for pid, p in props.items()]
        joins = [f"- {pid}: {link('class:' + p['range'])}" + (f" ({p['role']})" if p.get("role") else "")
                 for pid, p in ops.items() if p.get("domain") == cid and p.get("join") and p.get("cardinality") != "one-to-many"]
        joins += [f"- {pid}: {link('class:' + p['domain'])} (from)" for pid, p in ops.items()
                  if p.get("range") == cid and p.get("join") and p.get("cardinality") != "one-to-many"]
        mets = [f"- {link('metric:' + m['name'])}" for m in onto.get("metrics", []) if m.get("base_class") == cid]
        pgs = [f"- {link('page:' + pid)}" for pid, p in pages.items() if f"class:{cid}" in (p.get("links") or [])
               or any(r["target"] == f"class:{cid}" for r in p.get("relations") or [])]
        for title, items in (("Joins", joins), ("Metrics", mets), ("Pages", pgs)):
            if items:
                lines += ["", f"## {title}", "", *items]
        out[names[f"class:{cid}"] + ".md"] = _front({
            "aliases": c.get("synonyms"), "tags": c.get("domains"), "table": c.get("source_fqn") or c.get("source_table"),
            "status": (c.get("certification") or {}).get("status", "draft")}) + "\n".join(lines) + "\n"
    for m in onto.get("metrics", []):
        out[names[f"metric:{m['name']}"] + ".md"] = _front({"aliases": m.get("synonyms"), "tags": m.get("domains"),
                                                            "status": m.get("status", "draft")}) + "\n".join([
            f"# {m['name']}", "", m.get("description") or "", "", "```sql", str(m.get("sql") or ""), "```", "",
            f"On {link('class:' + str(m.get('base_class')))}"]) + "\n"
    for pid, p in pages.items():
        lines = [f"# {p['title']}", "", p.get("body") or "_Empty page._"]
        rels = [f"- {r['relation'].replace('_', ' ')} {link(r['target'])}" for r in p.get("relations") or []]
        rels += [f"- about {link(x)}" for x in p.get("links") or [] if x in names]
        if rels:
            lines += ["", "## Relations", "", *rels]
        if p.get("sources"):
            lines += ["", "## Sources", "", *[f"- {link('source:' + s)}" for s in p["sources"]]]
        out[names[f"page:{pid}"] + ".md"] = _front({"aliases": p.get("synonyms"), "tags": p.get("domains"),
                                                    "status": p.get("status", "draft")}) + "\n".join(lines) + "\n"
    for sid, s in sources.items():
        lines = [f"# {s['title']}", "", s.get("summary") or "", "", "## Passages", ""]
        for p in [x for x in onto.get("passages") or [] if x["source"] == sid]:
            where = " · ".join(x for x in (p.get("heading"), p.get("locator")) if x)
            lines += [f"### {where or p['id']}", "", "> " + p["text"].replace("\n", "\n> "), ""]
            if p.get("links"):
                lines += ["About: " + ", ".join(link(x) for x in p["links"]), ""]
        out[names[f"source:{sid}"] + ".md"] = _front({"kind": s.get("kind"), "uri": s.get("uri"),
                                                      "added": s.get("added_at")}) + "\n".join(lines) + "\n"
    folders: dict[str, list[str]] = {}
    for ref, path in sorted(names.items(), key=lambda x: x[1]):
        folders.setdefault(path.split("/", 1)[0], []).append(f"- {link(ref)}")
    title = onto.get("ontology", {}).get("name") or "Model"
    out["index.md"] = "\n".join([f"# {title}", "", *[x for f, items in folders.items() for x in ("", f"## {f}", "", *items)]]) + "\n"
    out["log.md"] = "\n".join(["# Change log", "", *[f"- {c.get('at')} · {c.get('by') or '?'} · {c.get('what')}"
                                                      for c in reversed(onto.get("changes") or [])]]) + "\n"
    return out


def vault_zip(onto: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in obsidian_vault(onto).items():
            z.writestr(path, text)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# auto-ontology MCP exports (export_ontology, format auto-ontology-mcp/v1) -> Apache Ossie
# --------------------------------------------------------------------------- #
def is_auto_ontology(text_or_doc) -> bool:
    doc = text_or_doc
    if isinstance(doc, str):
        if "auto-ontology-mcp/v1" not in doc[:2000]:
            return False
        try:
            doc = yaml.safe_load(doc)
        except Exception:  # noqa: BLE001
            return False
    return isinstance(doc, dict) and doc.get("format") == "auto-ontology-mcp/v1"


def _ident(text: str, taken: set) -> str:
    base = "".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", str(text)) if w) or "Dataset"
    if base[0].isdigit():
        base = "T" + base
    out, i = base, 2
    while out in taken:
        out, i = f"{base}{i}", i + 1
    taken.add(out)
    return out


def from_auto_ontology(doc: dict, target: str | None = None) -> tuple[dict, list[str]]:
    """An auto-ontology export as an Apache Ossie document. `target` is the Unity Catalog catalog.schema (or just a
    catalog) where the tables live; the export carries no catalog. One dataset per table, named after its Term;
    measures become metrics, filters rules, dimensions computed fields, concepts pages; glossary and example
    queries travel too. Proposed and rejected joins and join/query attributes are skipped (listed in notes)."""
    import sqlglot
    from sqlglot import exp

    from .ossie.validator import CURRENT_VERSION, STUDIO_VENDOR
    notes, target = [], (target or "").strip().strip(".")
    tables = {t["table"]: t for t in doc.get("tables") or [] if t.get("table")}
    terms_by_table: dict[str, list] = {}
    for t in doc.get("terms") or []:
        if t.get("table"):
            terms_by_table.setdefault(t["table"], []).append(t)
            tables.setdefault(t["table"], {"table": t["table"], "primary_key": [], "columns": {}})

    def source(table: str) -> str:
        parts = table.split(".")
        if target.count(".") == 1:
            return f"{target}.{parts[-1]}"
        if target and len(parts) == 2:
            return f"{target}.{table}"
        return table

    taken, ds_of, datasets, studio = set(), {}, [], {"glossary": doc.get("glossary") or [], "rules": [], "pages": []}
    for table, tb in tables.items():
        terms = terms_by_table.get(table) or []
        term = terms[0] if terms else None
        if len(terms) > 1:
            notes.append(f"{table} has {len(terms)} Terms; Ossie keeps one dataset per table, so it is named after "
                         f"{term['name']} and the columns of all of them are kept.")
        name = _ident(term["name"] if term else table.split(".")[-1], taken)
        ds_of[table.lower()] = ds_of[table.split(".")[-1].lower()] = name
        cols = {}
        for t in terms:
            for c in t.get("columns") or []:
                cols.setdefault(c["column"], {"display": c.get("display_name"), "description": c.get("description")})
        for col, desc in (tb.get("columns") or {}).items():
            cols.setdefault(col, {}).setdefault("description", desc)
        for col in tb.get("primary_key") or []:
            cols.setdefault(col, {})
        fields = []
        for col, info in cols.items():
            f = {"name": col, "expression": {"sql": col}}
            if info.get("description"):
                f["description"] = info["description"]
            if info.get("display") and info["display"].lower().replace(" ", "_") != col.lower():
                f["ai_context"] = {"synonyms": [info["display"]]}
            fields.append(f)
        ds = {"name": name, "source": source(table)}
        if tb.get("primary_key"):
            ds["primary_key"] = list(tb["primary_key"])
        if (term or {}).get("description") or tb.get("description"):
            ds["description"] = (term or {}).get("description") or tb.get("description")
        if (term or {}).get("synonyms"):
            ds["ai_context"] = {"synonyms": list(term["synonyms"])}
        ds["fields"] = fields
        if term and term.get("status") in ("certified", "deprecated"):
            ds["nonportable_extensions"] = [{"vendor_name": STUDIO_VENDOR,
                                             "data": {"certification": {"status": term["status"]}}}]
        datasets.append(ds)
    by_name = {d["name"]: d for d in datasets}

    def field(ds_name: str, col: str) -> str:
        fs = by_name[ds_name]["fields"]
        hit = next((f["name"] for f in fs if f["name"].lower() == col.lower()), None)
        if hit is None:                                      # Ossie relationships must name declared fields
            fs.append({"name": col, "expression": {"sql": col}, "description": "Join key"})
            hit = col
        return hit

    relationships, rel_names = [], set()
    skipped = Counter()
    for r in doc.get("relationships") or []:
        if r.get("status") in ("proposed", "rejected"):
            skipped[r["status"]] += 1
            continue
        (ft, _, fc), (tt, _, tc) = str(r.get("source")).rpartition("."), str(r.get("target")).rpartition(".")
        a, b = ds_of.get(ft.lower()) or ds_of.get(ft.split(".")[-1].lower()), \
            ds_of.get(tt.lower()) or ds_of.get(tt.split(".")[-1].lower())
        if not a or not b:
            notes.append(f"Relationship {r.get('source')} → {r.get('target')} skipped: a table is not in the export.")
            continue
        rel = {"name": _ident(f"{a} {r.get('role') or ''} {b}", rel_names), "from": a, "to": b,
               "from_columns": [field(a, fc)], "to_columns": [field(b, tc)]}
        if r.get("description"):
            rel["ai_context"] = {"instructions": r["description"]}
        sx = {k: v for k, v in (("role", r.get("role")), ("default", bool(r.get("is_default")) or None),
                                ("provenance", "auto-ontology")) if v}
        rel["nonportable_extensions"] = [{"vendor_name": STUDIO_VENDOR, "data": sx}]
        relationships.append(rel)
    if skipped:
        notes.append("Skipped " + " and ".join(f"{n} {k}" for k, n in skipped.items()) + " relationship(s): only "
                     "accepted joins are modeled; review the others again here.")

    def placed(sql: str, own: str) -> tuple[str, bool]:
        """MCP fragments qualify columns with the bare table name. Columns of the Term's own table become bare
        (a dataset metric); other tables' qualifiers become dataset names (a model metric)."""
        try:
            tree = sqlglot.parse_one(f"SELECT {sql}")
        except Exception:  # noqa: BLE001
            return sql, True
        mine = {own.lower(), own.split(".")[-1].lower()}
        local = True
        for c in tree.find_all(exp.Column):
            if c.table and c.table.lower() in mine:
                c.set("table", None)
            elif c.table and c.table.lower() in ds_of:
                c.set("table", exp.to_identifier(ds_of[c.table.lower()]))
                local = False
        if not local:                         # a model metric names its own dataset too
            for c in tree.find_all(exp.Column):
                if not c.table:
                    c.set("table", exp.to_identifier(ds_of[own.lower()]))
        return (tree.expressions[0].sql() if len(tree.expressions) == 1 else sql), local

    metrics, metric_names, kinds = [], set(), Counter()
    for table, terms in terms_by_table.items():
        ds_name = ds_of[table.lower()]
        for t in terms:
            for s in t.get("sql_attributes") or []:
                kind, expr = s.get("kind") or "measure", str(s.get("expression") or "")
                key = re.sub(r"[^a-z0-9]+", "_", str(s.get("name")).lower()).strip("_") or "attribute"
                if kind == "measure":
                    name, i = key, 2
                    while name in metric_names:
                        name, i = f"{key}_{i}", i + 1
                    metric_names.add(name)
                    sql, local = placed(expr, table)
                    m = {"name": name, "expression": {"sql": sql}, "nonportable_extensions": [
                        {"vendor_name": STUDIO_VENDOR, "data": {"status": s.get("status") or "draft",
                                                                "provenance": "auto-ontology"}}]}
                    if s.get("description"):
                        m["description"] = s["description"]
                    (by_name[ds_name].setdefault("metrics", []) if local else metrics).append(m)
                elif kind == "filter":
                    studio["rules"].append({"name": key, "description": s.get("description") or s.get("name"),
                                            "applies_to": [ds_name], "sql_hint": expr, "synonyms": [],
                                            "provenance": "auto-ontology", "status": s.get("status") or "draft"})
                elif kind == "dimension":
                    by_name[ds_name]["fields"].append({"name": key, "expression": {"sql": re.sub(
                        r"\b\w+\.(?=\w)", "", expr)}, **({"description": s["description"]} if s.get("description") else {})})
                else:
                    kinds[kind] += 1
    if kinds:
        notes.append("Skipped " + ", ".join(f"{n} {k}" for k, n in kinds.items()) + " SQL attribute(s): Ossie has "
                     "no place for whole queries; keep them as verified examples or pages.")
    rels_by_source: dict[str, list] = {}
    for r in doc.get("term_relations") or []:
        rels_by_source.setdefault(r.get("source"), []).append(r)
    page_ids: dict[str, str] = {}
    for t in doc.get("terms") or []:
        if not t.get("table"):
            pid = re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-") or "page"
            page_ids[t["name"]] = pid
            studio["pages"].append({"id": pid, "title": t["name"], "body": t.get("body") or t.get("description") or "",
                                    "status": t.get("status") or "draft", "synonyms": t.get("synonyms") or [],
                                    "provenance": "auto-ontology", "domains": [], "links": []})
    for pg in studio["pages"]:
        rels = [{"relation": r["relation"], "target": f"page:{page_ids[r['target']]}" if r["target"] in page_ids
                 else f"class:{_ident_lookup(r['target'], terms_by_table, ds_of)}"}
                for r in rels_by_source.get(pg["title"]) or [] if r.get("relation") in RELATIONS]
        rels = [r for r in rels if not r["target"].endswith(":")]
        if rels:
            pg["relations"] = rels
    examples = [{"question": e["question"], "sql": e["sql"], "note": e.get("note")}
                for e in doc.get("example_queries") or [] if e.get("question") and e.get("sql")]
    if examples:
        studio["examples"] = examples
        notes.append(f"{len(examples)} example queries came along; their SQL uses the export's table names, so "
                     "check them in 🧪 Check SQL before relying on them.")
    ossie = {"version": CURRENT_VERSION, "name": str((doc.get("datasource") or {}).get("name") or "auto_ontology"),
             **({"description": doc["datasource"]["description"]} if (doc.get("datasource") or {}).get("description") else {}),
             "datasets": datasets}
    if relationships:
        ossie["relationships"] = relationships
    if metrics:
        ossie["metrics"] = metrics
    ossie["nonportable_extensions"] = [{"vendor_name": STUDIO_VENDOR, "data": {k: v for k, v in studio.items() if v}}]
    return ossie, notes


def _ident_lookup(term_name: str, terms_by_table: dict, ds_of: dict) -> str:
    for table, terms in terms_by_table.items():
        if any(t["name"] == term_name for t in terms):
            return ds_of[table.lower()]
    return ""


def convert_auto_ontology_text(text: str, target: str | None = None) -> tuple[str, list[str]]:
    """auto-ontology export text -> Apache Ossie YAML text (+ notes)."""
    from .ossie import dump_yaml
    doc, notes = from_auto_ontology(yaml.safe_load(text), target)
    return dump_yaml(doc), notes
