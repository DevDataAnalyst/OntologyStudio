"""
Streamlit views for Apache Ossie validation reports (used by app.py).
"""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from . import ossie

SEV_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}
STATUS_ICON = {"passed": "✅", "warnings": "⚠️", "notes": "ℹ️", "failed": "❌", "skipped": "⏭️"}


def stem(name: str) -> str:
    base = re.sub(r"\.(ossie\.)?(ya?ml|json)$", "", name or "model", flags=re.I)
    return re.sub(r"[^\w\-.]+", "_", base).strip("_") or "model"


def sidebar_badge(rep: ossie.Report) -> None:
    if rep.document is None:
        st.error("❌ Not a valid YAML/JSON document")
    elif rep.errors:
        st.error(f"❌ {len(rep.errors)} error(s) · {len(rep.warnings)} warning(s)")
    elif rep.warnings:
        st.warning(f"✅ Valid Ossie {rep.profile.version} · {len(rep.warnings)} warning(s)")
    else:
        st.success(f"✅ Valid Apache Ossie {rep.profile.version}")


def headline(rep: ossie.Report) -> None:
    text = rep.headline()
    if rep.document is None or rep.errors:
        st.error(f"**{text}**")
    elif rep.warnings:
        st.warning(f"**{text}**")
    else:
        st.success(f"**{text}**")
    if rep.profile:
        st.caption(f"Checked against {rep.profile.title} · schema: {rep.profile.source}"
                   + (" · strict mode" if rep.strict else ""))


def report_view(rep: ossie.Report, key: str, show_stats: bool = True, upgrade: bool = True) -> None:
    """Full report: headline, model summary, check list, findings table, downloads."""
    headline(rep)
    s = rep.stats or {}
    if show_stats and s:
        c = st.columns(6)
        c[0].metric("Datasets", s.get("datasets", 0))
        c[1].metric("Fields", s.get("fields", 0))
        c[2].metric("Relationships", s.get("relationships", 0))
        c[3].metric("Metrics", s.get("model_metrics", 0) + s.get("dataset_metrics", 0),
                    help=f"{s.get('model_metrics', 0)} model-scoped, {s.get('dataset_metrics', 0)} dataset-scoped")
        c[4].metric("SQL checked", s.get("expressions_parsed", "–"),
                    help="Expressions parsed with sqlglot (non-SQL dialects such as DAX or MDX are carried, not parsed)")
        c[5].metric("Models", s.get("models", 1))
        extra = [f"Dialects: {s.get('dialects')}"] + [s[k] for k in ("described", "with_synonyms") if s.get(k)]
        st.caption(" · ".join(extra))
    left, right = st.columns([1, 2.6], gap="medium")
    with left:
        st.markdown("**Checks**")
        lines = []
        for c in ossie.CHECKS:
            if c in rep.checks:
                status = rep.checks[c]
                n = sum(1 for f in rep.findings if f.check == c)
                lines.append(f"{STATUS_ICON.get(status, '•')} {ossie.CHECK_TITLES[c]}"
                             + (f" ({n})" if n else ""))
        st.markdown("  \n".join(lines))
        if "skipped" in rep.checks.values():
            st.caption("⏭️ Semantic checks run once the schema errors are fixed, as in the reference "
                       "validator.")
    with right:
        st.markdown("**Findings**")
        if not rep.findings:
            st.caption("No findings: every check passed.")
        else:
            present = [s for s in ("error", "warning", "info") if any(f.severity == s for f in rep.findings)]
            labels = {"error": f"❌ Errors ({len(rep.errors)})", "warning": f"⚠️ Warnings ({len(rep.warnings)})",
                      "info": f"ℹ️ Notes ({len(rep.infos)})"}
            show = st.pills("Show", present, default=present, selection_mode="multi",
                            format_func=lambda x: labels[x], key=f"{key}_sev", label_visibility="collapsed") \
                or present
            rows = [{"": SEV_ICON[f.severity], "check": f.check,
                     "where": (f.path or "(document)") + (f" · line {f.line}" if f.line else ""),
                     "problem": f.message, "how to fix": f.hint}
                    for f in rep.findings if f.severity in show]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         height=min(520, 38 + 35 * max(1, len(rows))),
                         column_config={"": st.column_config.TextColumn(width=30),
                                        "check": st.column_config.TextColumn(width=86),
                                        "where": st.column_config.TextColumn(width=180),
                                        "problem": st.column_config.TextColumn(width=380),
                                        "how to fix": st.column_config.TextColumn(width=320)})
    st.download_button("⬇️ Download this report (.md)", rep.to_markdown(),
                       file_name=f"{stem(rep.file_name)}.ossie-report.md", mime="text/markdown", key=f"{key}_dl")
    if upgrade:
        upgrade_view(rep, key)


def needs_upgrade(rep: ossie.Report) -> bool:
    return rep.document is not None and isinstance(rep.document, dict) and (
        rep.legacy or any("Upgrade" in (f.hint or "") for f in rep.findings))


def upgrade_view(rep: ossie.Report, key: str) -> None:
    if not needs_upgrade(rep):
        return
    docs, notes = ossie.upgrade(rep.document)
    with st.expander(f"⬆️ Upgrade to Apache Ossie {ossie.CURRENT_VERSION}", expanded=True):
        st.caption("Rewrites the document in the current shape; nothing is changed in your original file.")
        for n in notes:
            st.write("•", n)
        for i, d in enumerate(docs):
            text = ossie.dump_yaml(d)
            r2 = ossie.validate_text(text, f"{d.get('name', 'model')}.ossie.yaml")
            name = stem(str(d.get("name") or rep.file_name or "model"))
            c1, c2 = st.columns([3, 1.4])
            c1.write(f"**{d.get('name', f'model {i + 1}')}** → {r2.headline()}")
            c2.download_button(f"⬇️ {name}.ossie.yaml", text, file_name=f"{name}.ossie.yaml",
                               mime="application/x-yaml", key=f"{key}_up_{i}", use_container_width=True)
            if r2.errors:
                with st.popover("Why it still fails"):
                    for f in r2.errors[:20]:
                        st.write("•", f.message, f"({f.path})" if f.path else "")
