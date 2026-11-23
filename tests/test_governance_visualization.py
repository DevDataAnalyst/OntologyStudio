from modules.governance_visualization import governance_coverage


def test_governance_coverage_counts_documented_table_and_metric_fields():
    coverage = governance_coverage({
        "classes": {
            "Orders": {
                "description": "One row per order",
                "domains": ["Sales"],
                "certification": {"owner": "sales@example.com", "status": "certified"},
            },
        },
        "metrics": [{
            "description": " ",
            "domains": [],
            "owner": "finance@example.com",
            "status": "draft",
        }],
    })

    assert coverage["assets"] == 2
    assert coverage["documented"] == 6
    assert coverage["total"] == 8
    assert coverage["percent"] == 75
    assert [item["documented"] for item in coverage["dimensions"]] == [1, 2, 1, 2]


def test_governance_coverage_handles_an_empty_model():
    coverage = governance_coverage({"classes": {}, "metrics": []})

    assert coverage == {
        "assets": 0,
        "documented": 0,
        "total": 0,
        "percent": 0,
        "dimensions": [
            {"label": "Business descriptions", "documented": 0, "total": 0},
            {"label": "Owners", "documented": 0, "total": 0},
            {"label": "Business domains", "documented": 0, "total": 0},
            {"label": "Review status", "documented": 0, "total": 0},
        ],
    }
