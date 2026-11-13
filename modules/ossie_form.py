"""
Guided form editor for Apache Ossie semantic models (the "YAML form editor" tab).

Business users edit an Ossie YAML one step at a time instead of in raw text:

    1 Model info -> 2 Datasets -> 3 Fields -> 4 Relationships -> 5 Metrics -> 6 Review & submit

Each step is a Streamlit form: nothing changes until the user presses its save button, and the
review step validates the whole document with the same checks as the Apache Ossie tab. Keys the
form does not show (dialect lists, nonportable_extensions, labels, ...) are carried through
untouched, so editing a studio export and loading it back loses nothing.

The pure helpers (ctx_*, expr_*, apply_*) hold the editing rules and are unit-tested;
editor_tab() is the Streamlit view.
"""
from __future__ import annotations

import copy
import difflib
import json
import math
from typing import Callable

import pandas as pd
import streamlit as st

from . import ossie, ossie_ui

DATATYPES = ["", "String", "Integer", "Decimal", "Float", "Boolean", "Date", "Time", "DateTime",
             "DateTimeTz", "Opaque"]
TIME_TYPES = {"Date", "Time", "DateTime", "DateTimeTz"}
MODEL_SCOPE = "🌐 Whole model (can combine datasets)"

STEPS = [
    ("Model info", "📘", "Name the model and say what it is for."),
    ("Datasets", "🗂️", "The business entities (tables or views) the model is built on."),
    ("Fields", "🔤", "The attributes of each dataset: what they are called and where the data comes from."),
    ("Relationships", "🔗", "How datasets connect, e.g. each order belongs to one customer."),
    ("Metrics", "📊", "The numbers the business reports on, e.g. total revenue."),
    ("Review & submit", "✅", "Check the whole model, see what changed, then download or load it."),
]


# =========================================================================== #
# Pure helpers (no Streamlit)
# =========================================================================== #
def blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def text(v) -> str:
    return "" if blank(v) else str(v).strip()


def split_list(v, sep: str = ",") -> list[str]:
    return [p.strip() for p in text(v).split(sep) if p.strip()]


def join_list(items) -> str:
    return ", ".join(str(i) for i in items or [])


def ctx_get(obj: dict) -> tuple[str, list[str], list[str]]:
    """ai_context (string or object) -> (instructions, synonyms, examples)."""
    c = obj.get("ai_context")
    if isinstance(c, str):
        return c, [], []
    if isinstance(c, dict):
        return str(c.get("instructions") or ""), list(c.get("synonyms") or []), list(c.get("examples") or [])
    return "", [], []


def ctx_set(obj: dict, instructions: str = "", synonyms=None, examples=None) -> None:
    """Write ai_context back in the smallest form; extra keys of an object context are kept."""
    instructions, synonyms, examples = text(instructions), list(synonyms or []), list(examples or [])
    old = obj.get("ai_context")
    if isinstance(old, dict):
        new = {k: v for k, v in old.items() if k not in ("instructions", "synonyms", "examples")}
    else:
        new = {}
    if not (synonyms or examples or new):
        if instructions:
            obj["ai_context"] = instructions
        else:
            obj.pop("ai_context", None)
        return
    if instructions:
        new["instructions"] = instructions
    if synonyms:
        new["synonyms"] = synonyms
    if examples:
        new["examples"] = examples
    obj["ai_context"] = new


def expr_get(obj: dict) -> tuple[str, str]:
    """expression -> (text shown in the form, where it lives: 'sql' or the dialect name)."""
    e = obj.get("expression")
    if isinstance(e, dict):
        if e.get("sql"):
            return str(e["sql"]), "sql"
        d = (e.get("dialects") or [{}])[0]
        if isinstance(d, dict) and d.get("expression"):
            return str(d["expression"]), str(d.get("dialect") or "")
    elif isinstance(e, str):
        return e, "sql"
    return "", "sql"


def expr_set(obj: dict, value: str) -> None:
    """Change the expression in place: the portable sql if present, else the first dialect entry."""
    value = text(value)
    e = obj.get("expression")
    if isinstance(e, dict) and not e.get("sql") and e.get("dialects") and isinstance(e["dialects"][0], dict):
        e["dialects"][0]["expression"] = value
    elif isinstance(e, dict):
        e["sql"] = value
    else:
        obj["expression"] = {"sql": value}


def set_or_pop(obj: dict, key: str, value) -> None:
    if blank(value) or value == []:
        obj.pop(key, None)
    else:
        obj[key] = value


def _rows(df: pd.DataFrame) -> list[dict]:
    return [] if df is None else df.to_dict("records")


def _row_id(r: dict):
    v = r.get("_row")
    return None if blank(v) else int(v)


def rename_field(doc: dict, ds_name: str, old: str, new: str) -> None:
    """Follow a field rename into keys and relationship columns of the same dataset."""
    for ds in doc.get("datasets") or []:
        if ds.get("name") != ds_name:
            continue
        if ds.get("primary_key"):
            ds["primary_key"] = [new if c == old else c for c in ds["primary_key"]]
        if ds.get("unique_keys"):
            ds["unique_keys"] = [[new if c == old else c for c in k] for k in ds["unique_keys"]]
    for r in doc.get("relationships") or []:
        if r.get("from") == ds_name:
            r["from_columns"] = [new if c == old else c for c in r.get("from_columns") or []]
        if r.get("to") == ds_name:
            r["to_columns"] = [new if c == old else c for c in r.get("to_columns") or []]


def apply_model_info(doc: dict, name: str, description: str, instructions: str, synonyms: str,
                     examples: str) -> None:
    doc["name"] = text(name)
    set_or_pop(doc, "description", text(description))
    ctx_set(doc, instructions, split_list(synonyms), [x.strip() for x in text(examples).splitlines() if x.strip()])


def dataset_table(doc: dict) -> pd.DataFrame:
    rows = []
    for i, ds in enumerate(doc.get("datasets") or []):
        instr, syn, _ = ctx_get(ds)
        rows.append({"_row": i, "name": ds.get("name", ""), "source": ds.get("source", ""),
                     "description": ds.get("description", ""), "primary_key": join_list(ds.get("primary_key")),
                     "unique_keys": "; ".join(join_list(k) for k in ds.get("unique_keys") or []),
                     "synonyms": join_list(syn), "ai_notes": instr,
                     "fields": len(ds.get("fields") or [])})
    cols = ["_row", "name", "source", "description", "primary_key", "unique_keys", "synonyms", "ai_notes", "fields"]
    return pd.DataFrame(rows, columns=cols)


def apply_datasets(doc: dict, df: pd.DataFrame) -> list[str]:
    """Rows of the dataset table -> doc['datasets']. Renames follow into relationships."""
    notes, old = [], doc.get("datasets") or []
    out = []
    for r in _rows(df):
        name = text(r.get("name"))
        if not name:
            continue
        i = _row_id(r)
        ds = copy.deepcopy(old[i]) if i is not None and i < len(old) else {"name": name, "fields": []}
        prev = ds.get("name")
        if prev and prev != name:
            for rel in doc.get("relationships") or []:
                rel["from"] = name if rel.get("from") == prev else rel.get("from")
                rel["to"] = name if rel.get("to") == prev else rel.get("to")
            notes.append(f"Renamed dataset {prev} → {name} (relationships updated; check metric expressions "
                         f"that mention {prev}).")
        ds["name"] = name
        set_or_pop(ds, "source", text(r.get("source")))
        set_or_pop(ds, "description", text(r.get("description")))
        set_or_pop(ds, "primary_key", split_list(r.get("primary_key")))
        set_or_pop(ds, "unique_keys", [split_list(k) for k in text(r.get("unique_keys")).split(";")
                                       if split_list(k)])
        _, _, ex = ctx_get(ds)
        ctx_set(ds, r.get("ai_notes"), split_list(r.get("synonyms")), ex)
        ds.setdefault("fields", [])
        out.append(_order(ds, ["name", "source", "primary_key", "unique_keys", "description", "ai_context",
                               "fields", "metrics"]))
    removed = {d.get("name") for d in old} - {d["name"] for d in out}
    if removed:
        notes.append("Removed dataset(s): " + ", ".join(sorted(map(str, removed)))
                     + ". Relationships that use them will show up in the review.")
    doc["datasets"] = out
    return notes


def field_table(ds: dict) -> pd.DataFrame:
    rows = []
    for i, f in enumerate(ds.get("fields") or []):
        expr, where = expr_get(f)
        instr, syn, _ = ctx_get(f)
        dt = f.get("datatype") or ""
        is_time = (f.get("dimension") or {}).get("is_time", dt in TIME_TYPES)
        rows.append({"_row": i, "name": f.get("name", ""), "expression": expr,
                     "dialect": "portable SQL" if where == "sql" else where,
                     "datatype": dt if dt in DATATYPES else "", "is_time": bool(is_time),
                     "label": f.get("label", ""), "description": f.get("description", ""),
                     "synonyms": join_list(syn), "ai_notes": instr})
    cols = ["_row", "name", "expression", "dialect", "datatype", "is_time", "label", "description", "synonyms",
            "ai_notes"]
    return pd.DataFrame(rows, columns=cols)


def apply_fields(doc: dict, ds_name: str, df: pd.DataFrame) -> list[str]:
    notes = []
    ds = next(d for d in doc["datasets"] if d.get("name") == ds_name)
    old = ds.get("fields") or []
    out = []
    for r in _rows(df):
        name = text(r.get("name"))
        if not name:
            continue
        i = _row_id(r)
        f = copy.deepcopy(old[i]) if i is not None and i < len(old) else {"name": name}
        prev = f.get("name")
        if prev and prev != name:
            rename_field(doc, ds_name, prev, name)
            notes.append(f"Renamed field {ds_name}.{prev} → {name} (keys and relationships updated).")
        f["name"] = name
        expr_set(f, text(r.get("expression")) or name)
        dt = text(r.get("datatype"))
        set_or_pop(f, "datatype", dt)
        default_time = dt in TIME_TYPES
        want = bool(r.get("is_time")) if not blank(r.get("is_time")) else default_time
        dim = dict(f.get("dimension") or {})
        if "is_time" in dim or want != default_time:
            dim["is_time"] = want
        set_or_pop(f, "dimension", dim or None)
        set_or_pop(f, "label", text(r.get("label")))
        set_or_pop(f, "description", text(r.get("description")))
        _, _, ex = ctx_get(f)
        ctx_set(f, r.get("ai_notes"), split_list(r.get("synonyms")), ex)
        out.append(_order(f, ["name", "expression", "datatype", "dimension", "label", "description", "ai_context"]))
    removed = {f.get("name") for f in old} - {f["name"] for f in out}
    if removed:
        notes.append(f"Removed field(s) from {ds_name}: " + ", ".join(sorted(map(str, removed))) + ".")
    ds["fields"] = out
    return notes


def relationship_table(doc: dict) -> pd.DataFrame:
    rows = []
    for i, r in enumerate(doc.get("relationships") or []):
        instr, _, _ = ctx_get(r)
        rows.append({"_row": i, "name": r.get("name", ""), "from": r.get("from", ""),
                     "from_columns": join_list(r.get("from_columns")), "to": r.get("to", ""),
                     "to_columns": join_list(r.get("to_columns")), "ai_notes": instr})
    return pd.DataFrame(rows, columns=["_row", "name", "from", "from_columns", "to", "to_columns", "ai_notes"])


def apply_relationships(doc: dict, df: pd.DataFrame) -> list[str]:
    old, out, notes = doc.get("relationships") or [], [], []
    for r in _rows(df):
        frm, to = text(r.get("from")), text(r.get("to"))
        if not (frm and to):
            if text(r.get("name")):
                notes.append(f"Skipped relationship {text(r.get('name'))}: pick both datasets.")
            continue
        i = _row_id(r)
        rel = copy.deepcopy(old[i]) if i is not None and i < len(old) else {}
        rel["name"] = text(r.get("name")) or f"{frm}_to_{to}"
        rel["from"], rel["to"] = frm, to
        rel["from_columns"] = split_list(r.get("from_columns"))
        rel["to_columns"] = split_list(r.get("to_columns"))
        _, syn, ex = ctx_get(rel)
        ctx_set(rel, r.get("ai_notes"), syn, ex)
        out.append(_order(rel, ["name", "from", "to", "from_columns", "to_columns", "ai_context"]))
    if out:
        doc["relationships"] = out
    else:
        doc.pop("relationships", None)
    return notes


def metric_list(doc: dict, scope: str) -> list:
    if scope == MODEL_SCOPE:
        return doc.get("metrics") or []
    ds = next((d for d in doc.get("datasets") or [] if d.get("name") == scope), {})
    return ds.get("metrics") or []


def metric_table(doc: dict, scope: str) -> pd.DataFrame:
    rows = []
    for i, m in enumerate(metric_list(doc, scope)):
        expr, where = expr_get(m)
        instr, syn, _ = ctx_get(m)
        dt = m.get("datatype") or ""
        rows.append({"_row": i, "name": m.get("name", ""), "expression": expr,
                     "dialect": "portable SQL" if where == "sql" else where,
                     "datatype": dt if dt in DATATYPES else "", "description": m.get("description", ""),
                     "synonyms": join_list(syn), "ai_notes": instr})
    return pd.DataFrame(rows, columns=["_row", "name", "expression", "dialect", "datatype", "description",
                                       "synonyms", "ai_notes"])


def apply_metrics(doc: dict, scope: str, df: pd.DataFrame) -> list[str]:
    old, out, notes = metric_list(doc, scope), [], []
    for r in _rows(df):
        name = text(r.get("name"))
        if not name:
            continue
        if not text(r.get("expression")):
            notes.append(f"Metric {name} has no expression yet; it will be flagged in the review.")
        i = _row_id(r)
        m = copy.deepcopy(old[i]) if i is not None and i < len(old) else {"name": name}
        m["name"] = name
        expr_set(m, text(r.get("expression")))
        set_or_pop(m, "datatype", text(r.get("datatype")))
        set_or_pop(m, "description", text(r.get("description")))
        _, _, ex = ctx_get(m)
        ctx_set(m, r.get("ai_notes"), split_list(r.get("synonyms")), ex)
        out.append(_order(m, ["name", "expression", "datatype", "description", "ai_context"]))
    if scope == MODEL_SCOPE:
        set_or_pop(doc, "metrics", out)
    else:
        ds = next(d for d in doc["datasets"] if d.get("name") == scope)
        set_or_pop(ds, "metrics", out)
    return notes


def _order(d: dict, first: list[str]) -> dict:
    """Stable, readable key order: known keys first, then whatever else the object carried."""
    return {**{k: d[k] for k in first if k in d}, **{k: v for k, v in d.items() if k not in first}}


def tidy(doc: dict) -> dict:
    return _order(doc, ["version", "name", "description", "ai_context", "datasets", "relationships", "metrics"])


def new_model() -> dict:
    return {"version": ossie.CURRENT_VERSION, "name": "my_semantic_model",
            "description": "", "datasets": []}


def relationship_sentence(r: dict) -> str:
    fc, tc = join_list(r.get("from_columns")), join_list(r.get("to_columns"))
    return f"Each **{r.get('from', '?')}** belongs to one **{r.get('to', '?')}** (`{fc}` → `{tc}`)"


# =========================================================================== #
# Streamlit view
# =========================================================================== #
CSS = """
<style>
.fe-card{border:1px solid rgba(128,128,128,.28);border-left:5px solid #1f6feb;border-radius:10px;
  padding:10px 16px;margin:6px 0 12px 0;background:rgba(31,111,235,.05)}
.fe-card b{font-size:1.05rem}
</style>
"""


def _ss():
    return st.session_state


def _bump(msg: str | list | None = None, step: int | None = None) -> None:
    s = _ss()
    s.fe_rev = s.get("fe_rev", 0) + 1
    if step is not None:
        s.fe_step = step
        s.fe_seen = sorted(set(s.get("fe_seen", [])) | {step})
    if msg:
        s.fe_msg = msg if isinstance(msg, list) else [msg]
    st.rerun()


def _stepper(cur: int, seen: list[int]) -> None:
    """Clickable step bar: ✓ for saved steps, the current one highlighted."""
    st.progress((cur + 1) / len(STEPS))
    cols = st.columns(len(STEPS), gap="small")
    for i, (label, icon, _) in enumerate(STEPS):
        mark = "✓" if i in seen and i != cur else str(i + 1)
        short = label.split(" & ")[0]
        if cols[i].button(f"{mark} · {icon} {short}", key=f"fe_go_{i}", use_container_width=True,
                          type="primary" if i == cur else "secondary",
                          help=f"Open step {i + 1}. Save the current step first: unsaved edits are dropped."):
            if i != cur:
                _bump(step=i)


def _card(i: int) -> None:
    label, icon, why = STEPS[i]
    st.markdown(f'<div class="fe-card"><b>Step {i + 1} of {len(STEPS)} · {icon} {label}</b><br>{why}</div>',
                unsafe_allow_html=True)


def _nav(i: int) -> tuple[bool, bool, bool]:
    """Bottom of a step form. Every button saves; they differ in where you land. -> (back, saved, next)"""
    c1, _, c3, c4 = st.columns([1.3, 1.9, 1.1, 1.6])
    back = c1.form_submit_button("◀ Save & back", disabled=i == 0, use_container_width=True)
    save = c3.form_submit_button("💾 Save", use_container_width=True)
    nxt = c4.form_submit_button("Save & continue ▶", type="primary", use_container_width=True)
    return back, (back or save or nxt), nxt


def _go_after(i: int, back: bool, nxt: bool, notes: list[str], saved: bool) -> None:
    msg = notes + (["Saved."] if saved else [])
    _bump(msg, step=i - 1 if back else (i + 1 if nxt else i))


def _start_panel(onto: dict | None) -> None:
    s = _ss()
    st.markdown("Pick the model to edit. Nothing in the studio changes until you **submit** on the last step.")
    opts = (["Current studio model"] if onto else []) + ["Upload an Ossie YAML / JSON file", "Start a new model"]
    choice = st.radio("Start from", opts, horizontal=True, key=f"fe_src_{bool(onto)}", label_visibility="collapsed")
    doc, name, notes = None, "", []
    if choice == "Current studio model":
        st.caption(f"Converts **{(onto.get('ontology') or {}).get('name', 'the ontology')}** to Apache Ossie "
                   f"{ossie.CURRENT_VERSION}; studio-only details ride along as extensions and come back on submit.")
        if st.button("Open in the form editor", type="primary", key="fe_open_cur"):
            doc, notes = ossie.from_ontology(onto, include_studio=True, include_layout=False)
            name = ossie_ui.stem(str(doc.get("name") or "model")) + ".ossie.yaml"
    elif choice.startswith("Upload"):
        f = st.file_uploader("Apache Ossie semantic model", type=["yaml", "yml", "json"], key="fe_upload")
        if f is not None and st.button("Open in the form editor", type="primary", key="fe_open_up"):
            raw = f.getvalue().decode("utf-8-sig", errors="replace")
            data, problems = ossie.load_text(raw, f.name)
            if data is None or not isinstance(data, dict):
                st.error("This file is not a readable YAML/JSON mapping: "
                         + "; ".join(p.message for p in problems[:3]))
                return
            if data.get("version") != ossie.CURRENT_VERSION or isinstance(data.get("semantic_model"), list):
                docs, up_notes = ossie.upgrade(data)
                if not docs:
                    st.error("Could not read a semantic model from this file.")
                    return
                data = docs[0]
                notes = [f"Upgraded to {ossie.CURRENT_VERSION} for editing."] + up_notes[:4]
            doc, name = data, f.name
    else:
        if st.button("Create an empty model", type="primary", key="fe_open_new"):
            doc, name = new_model(), "new_model.ossie.yaml"
    if doc is not None:
        s.fe_doc = copy.deepcopy(doc)
        s.fe_orig = ossie.dump_yaml(tidy(copy.deepcopy(doc)))
        s.fe_name = name
        s.fe_seen = []
        _bump(notes or None, step=0)


def editor_tab(onto: dict | None, on_submit: Callable[[dict, str], None] | None) -> None:
    """The guided editor. on_submit(doc, file_name) loads the finished model into the studio."""
    s = _ss()
    st.markdown(CSS, unsafe_allow_html=True)
    head_l, head_r = st.columns([4, 1.2])
    head_l.markdown("#### ✏️ YAML form editor")
    head_l.caption("Edit an Apache Ossie semantic model step by step, no YAML knowledge needed. "
                   "Each step saves when you press its button; the last step checks everything.")
    doc = s.get("fe_doc")
    if doc is None:
        _start_panel(onto)
        return
    if head_r.button("↺ Start over", use_container_width=True, help="Close this model and pick another"):
        for k in ("fe_doc", "fe_orig", "fe_name", "fe_step", "fe_seen", "fe_msg"):
            s.pop(k, None)
        st.rerun()

    i = s.setdefault("fe_step", 0)
    rev = s.get("fe_rev", 0)
    st.caption(f"Editing **{doc.get('name') or 'unnamed model'}** · {len(doc.get('datasets') or [])} dataset(s) "
               f"· {len(doc.get('relationships') or [])} relationship(s) · "
               f"{len(doc.get('metrics') or []) + sum(len(d.get('metrics') or []) for d in doc.get('datasets') or [])}"
               f" metric(s) · Ossie {doc.get('version', '?')}")
    _stepper(i, s.get("fe_seen", []))
    _card(i)
    for m in s.pop("fe_msg", []) or []:
        (st.success if m == "Saved." else st.info)(m)

    [_step_model, _step_datasets, _step_fields, _step_relationships, _step_metrics, _step_review][i](
        doc, i, rev, on_submit)


# --------------------------------------------------------------------------- #
def _step_model(doc, i, rev, _):
    instr, syn, ex = ctx_get(doc)
    with st.form(f"fe_model_{rev}"):
        c1, c2 = st.columns([2, 1])
        name = c1.text_input("Model name *", doc.get("name", ""),
                             help="A short identifier, e.g. sales_analytics. Letters, digits and _ work best.")
        c2.text_input("Ossie version", str(doc.get("version", "")), disabled=True,
                      help="Set by the file; the form always writes the current Ossie shape.")
        desc = st.text_area("What is this model about?", doc.get("description", ""), height=90,
                            placeholder="e.g. Orders, customers and products of the online shop, for sales reporting.")
        st.markdown("**Help AI assistants understand it** (optional)")
        a1, a2 = st.columns(2)
        syn_t = a1.text_input("Other names people use (comma separated)", join_list(syn),
                              placeholder="sales model, revenue model")
        instr_t = a2.text_input("Instructions for AI", instr, placeholder="Amounts are in CAD unless stated.")
        ex_t = st.text_area("Example questions (one per line)", "\n".join(ex), height=80,
                            placeholder="What was revenue last month?\nWho are our top 10 customers?")
        back, saved, nxt = _nav(i)
    if saved:
        if not text(name):
            st.error("The model needs a name.")
            return
        apply_model_info(doc, name, desc, instr_t, syn_t, ex_t)
        _go_after(i, back, nxt, [], saved)


def _step_datasets(doc, i, rev, _):
    st.markdown("One row per dataset. **Source** is the table or view it reads, e.g. `main.sales.orders`. "
                "**Primary key** is the field (or fields, comma separated) that identifies a row. "
                "Add a row at the bottom; select a row and press Delete to remove it.")
    df = dataset_table(doc)
    with st.form(f"fe_ds_{rev}"):
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"fe_ds_tbl_{rev}",
            column_order=["name", "source", "description", "primary_key", "unique_keys", "synonyms", "ai_notes",
                          "fields"],
            column_config={
                "name": st.column_config.TextColumn("Dataset name *", required=True, width="medium"),
                "source": st.column_config.TextColumn("Source table / view *", width="medium"),
                "description": st.column_config.TextColumn("Description", width="large"),
                "primary_key": st.column_config.TextColumn("Primary key", help="Field name(s), comma separated"),
                "unique_keys": st.column_config.TextColumn(
                    "Other unique keys", help="Comma inside one key, semicolon between keys: a, b; c"),
                "synonyms": st.column_config.TextColumn("Other names", help="Comma separated"),
                "ai_notes": st.column_config.TextColumn("AI instructions"),
                "fields": st.column_config.NumberColumn("Fields", disabled=True, width="small",
                                                        help="Edit fields in step 3"),
            })
        back, saved, nxt = _nav(i)
    if saved:
        notes = apply_datasets(doc, edited)
        _go_after(i, back, nxt, notes, saved)


def _pick_dataset(doc, key: str, extra: list[str] | None = None) -> str | None:
    names = (extra or []) + [d.get("name") for d in doc.get("datasets") or []]
    if not names:
        st.info("Add a dataset in step 2 first.")
        return None
    return st.selectbox("Dataset", names, key=key)


def _step_fields(doc, i, rev, _):
    ds_name = _pick_dataset(doc, "fe_field_ds")
    if ds_name is None:
        return
    ds = next(d for d in doc["datasets"] if d.get("name") == ds_name)
    st.markdown(f"Fields of **{ds_name}** (source `{ds.get('source', '?')}`). **Column / expression** is the "
                "source column, or a SQL expression such as `first_name || ' ' || last_name`. Tick **Time** for "
                "dates and periods people filter or trend by. Save before switching dataset.")
    df = field_table(ds)
    with st.form(f"fe_fields_{rev}_{ds_name}"):
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"fe_f_tbl_{rev}_{ds_name}",
            column_order=["name", "expression", "datatype", "is_time", "label", "description", "synonyms",
                          "ai_notes", "dialect"],
            column_config={
                "name": st.column_config.TextColumn("Field name *", required=True),
                "expression": st.column_config.TextColumn("Column / expression *", width="medium"),
                "datatype": st.column_config.SelectboxColumn("Data type", options=DATATYPES),
                "is_time": st.column_config.CheckboxColumn("Time", help="Use as a time dimension", width="small"),
                "label": st.column_config.TextColumn("Label / group"),
                "description": st.column_config.TextColumn("Description", width="large"),
                "synonyms": st.column_config.TextColumn("Other names"),
                "ai_notes": st.column_config.TextColumn("AI instructions"),
                "dialect": st.column_config.TextColumn("Written in", disabled=True,
                                                       help="Where the expression is stored; kept on save"),
            })
        back, saved, nxt = _nav(i)
    if saved:
        notes = apply_fields(doc, ds_name, edited)
        _go_after(i, back, nxt, notes, saved)


def _step_relationships(doc, i, rev, _):
    names = [d.get("name") for d in doc.get("datasets") or []]
    st.markdown("Read each row as: *each row of* **From** *belongs to one row of* **To**. "
                "**From columns** are the linking fields on the many side (e.g. `customer_id` on orders); "
                "**To columns** are the key on the one side (e.g. `id` on customers). Several columns: comma separated.")
    with st.expander("Field names per dataset", expanded=False):
        for d in doc.get("datasets") or []:
            st.markdown(f"**{d.get('name')}**: " + (join_list(f.get("name") for f in d.get("fields") or [])
                                                     or "_no fields yet_"))
    df = relationship_table(doc)
    with st.form(f"fe_rel_{rev}"):
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"fe_rel_tbl_{rev}",
            column_order=["from", "from_columns", "to", "to_columns", "name", "ai_notes"],
            column_config={
                "from": st.column_config.SelectboxColumn("From (many side) *", options=names, required=True),
                "from_columns": st.column_config.TextColumn("From columns *"),
                "to": st.column_config.SelectboxColumn("To (one side) *", options=names, required=True),
                "to_columns": st.column_config.TextColumn("To columns *"),
                "name": st.column_config.TextColumn("Relationship name", help="Left empty: from_to_to"),
                "ai_notes": st.column_config.TextColumn("AI instructions", width="medium"),
            })
        back, saved, nxt = _nav(i)
    rels = doc.get("relationships") or []
    if rels:
        st.markdown("**In plain words**")
        st.markdown("\n".join(f"- {relationship_sentence(r)}" for r in rels))
    if saved:
        notes = apply_relationships(doc, edited)
        _go_after(i, back, nxt, notes, saved)


def _step_metrics(doc, i, rev, _):
    scope = _pick_dataset(doc, "fe_metric_scope", extra=[MODEL_SCOPE])
    if scope is None:
        return
    if scope == MODEL_SCOPE:
        st.markdown("Model-wide metrics may combine datasets. Refer to fields as `dataset.field`, e.g. "
                    "`SUM(orders.amount) / COUNT(DISTINCT customers.id)`.")
    else:
        st.markdown(f"Metrics that belong to **{scope}** only. Refer to its fields by name, e.g. `SUM(amount)`.")
    df = metric_table(doc, scope)
    with st.form(f"fe_metrics_{rev}_{scope}"):
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"fe_m_tbl_{rev}_{scope}",
            column_order=["name", "expression", "datatype", "description", "synonyms", "ai_notes", "dialect"],
            column_config={
                "name": st.column_config.TextColumn("Metric name *", required=True),
                "expression": st.column_config.TextColumn("Calculation (SQL) *", width="large"),
                "datatype": st.column_config.SelectboxColumn("Data type", options=DATATYPES),
                "description": st.column_config.TextColumn("What it measures", width="medium"),
                "synonyms": st.column_config.TextColumn("Other names"),
                "ai_notes": st.column_config.TextColumn("AI instructions"),
                "dialect": st.column_config.TextColumn("Written in", disabled=True),
            })
        back, saved, nxt = _nav(i)
    if saved:
        notes = apply_metrics(doc, scope, edited)
        _go_after(i, back, nxt, notes, saved)


def _step_review(doc, i, rev, on_submit):
    s = _ss()
    final = tidy(doc)
    yaml_text = ossie.dump_yaml(final)
    name = ossie_ui.stem(str(final.get("name") or "model")) + ".ossie.yaml"
    rep = ossie.validate_text(yaml_text, name)
    ossie_ui.report_view(rep, key=f"fe_rep_{rev}", upgrade=False)

    orig = s.get("fe_orig") or ""
    diff = list(difflib.unified_diff(orig.splitlines(), yaml_text.splitlines(), "before", "after", lineterm="", n=2))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    with st.expander(f"🔍 What changed ({added} line(s) added, {removed} removed)", expanded=False):
        if diff:
            st.code("\n".join(diff), language="diff")
        else:
            st.caption("No changes yet.")
    with st.expander("📄 Preview the YAML"):
        st.code(yaml_text, language="yaml")

    st.markdown("##### Submit")
    with st.form(f"fe_submit_{rev}"):
        c1, c2 = st.columns([1.1, 2])
        load = c1.checkbox("Load into Ontology Studio", value=on_submit is not None and rep.ok,
                           disabled=on_submit is None or not rep.ok,
                           help="Replaces the model in the Network and other tabs with this one")
        c2.caption("Fix the errors above to load it into the studio; you can always download it."
                   if not rep.ok else "Submitting keeps your download below ready either way.")
        b1, _, b3 = st.columns([1, 2, 1.6])
        back = b1.form_submit_button("◀ Back", use_container_width=True)
        submit = b3.form_submit_button("✅ Submit", type="primary", use_container_width=True)
    if back:
        _bump(step=i - 1)
    if submit:
        if load and on_submit is not None and rep.ok:
            on_submit(copy.deepcopy(final), name)
            s.fe_orig = yaml_text
            _bump("Submitted: the model is loaded in Ontology Studio (see the Network tab). "
                  "Download it below to keep the file.", step=i)
        else:
            st.success("Submitted. Download the YAML below.")
    d1, d2, _ = st.columns([1.3, 1.3, 2])
    d1.download_button("⬇️ Download YAML", yaml_text, file_name=name, mime="application/x-yaml", type="primary",
                       use_container_width=True, key=f"fe_dl_y_{rev}")
    d2.download_button("⬇️ Download JSON", json.dumps(final, indent=2, default=str),
                       file_name=name.replace(".yaml", ".json"), mime="application/json",
                       use_container_width=True, key=f"fe_dl_j_{rev}")
