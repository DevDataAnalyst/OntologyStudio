"""Governance metadata coverage for modeled tables and metrics."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from . import theme

SIGNALS = (
    ("Business descriptions", "description"),
    ("Owners", "owner"),
    ("Business domains", "domain"),
    ("Review status", "status"),
)


def _present(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def governance_coverage(onto: dict) -> dict:
    """Measure which basic governance details are recorded for tables and metrics."""
    classes = onto.get("classes") or {}
    assets = []
    if isinstance(classes, dict):
        for item in classes.values():
            certification = item.get("certification") or {}
            assets.append({
                "description": item.get("description"),
                "owner": item.get("owner") or certification.get("owner"),
                "domain": item.get("domains"),
                "status": item.get("status") or certification.get("status"),
            })
    for item in onto.get("metrics") or []:
        assets.append({
            "description": item.get("description"),
            "owner": item.get("owner"),
            "domain": item.get("domains"),
            "status": item.get("status"),
        })

    dimension_counts = [
        {
            "label": label,
            "documented": sum(_present(asset[key]) for asset in assets),
            "total": len(assets),
        }
        for label, key in SIGNALS
    ]
    total = len(assets) * len(SIGNALS)
    documented = sum(item["documented"] for item in dimension_counts)
    return {
        "assets": len(assets),
        "documented": documented,
        "total": total,
        "percent": round(documented * 100 / total) if total else 0,
        "dimensions": dimension_counts,
    }


def render(onto: dict) -> None:
    st.subheader("Governance clarity")
    st.caption("How much governance information is recorded for the tables and metrics in this semantic model.")
    coverage = governance_coverage(onto)
    if not coverage["assets"]:
        st.info("Add modeled tables or metrics to see governance coverage.")
        return

    documented = coverage["documented"]
    missing = coverage["total"] - documented
    colors = theme.colors()
    chart_data = pd.DataFrame([
        {"state": "Recorded", "value": documented},
        {"state": "Missing", "value": missing},
    ])
    chart = {
        "layer": [
            {
                "mark": {"type": "arc", "innerRadius": 78, "outerRadius": 112, "cornerRadius": 3},
                "encoding": {
                    "theta": {"field": "value", "type": "quantitative", "stack": True},
                    "color": {
                        "field": "state",
                        "type": "nominal",
                        "scale": {
                            "domain": ["Recorded", "Missing"],
                            "range": ["#2e8b57", colors["borderColor"]],
                        },
                        "legend": None,
                    },
                    "tooltip": [
                        {"field": "state", "type": "nominal", "title": "Governance detail"},
                        {"field": "value", "type": "quantitative", "title": "Fields"},
                    ],
                },
            },
            {
                "mark": {"type": "text", "fontSize": 27, "fontWeight": "bold", "dy": -6},
                "encoding": {
                    "text": {"value": f'{coverage["percent"]}%'},
                    "color": {"value": colors["textColor"]},
                },
            },
            {
                "mark": {"type": "text", "fontSize": 12, "dy": 18},
                "encoding": {
                    "text": {"value": "recorded"},
                    "color": {"value": colors["textColor"]},
                },
            },
        ],
        "view": {"stroke": None},
    }

    chart_col, detail_col = st.columns([1, 1.4], vertical_alignment="center")
    with chart_col:
        st.vega_lite_chart(chart_data, chart, width="stretch", height=270)
    with detail_col:
        st.markdown(f"**{documented} of {coverage['total']} governance details recorded** "
                    f"across {coverage['assets']} modeled assets.")
        for item in coverage["dimensions"]:
            ratio = item["documented"] / item["total"] if item["total"] else 0
            st.progress(ratio, text=f'{item["label"]}: {item["documented"]}/{item["total"]}')
    st.caption("Coverage checks for a description, owner, business domain, and review status on each table "
               "and metric. It shows what is documented—not governance quality or certification approval.")
