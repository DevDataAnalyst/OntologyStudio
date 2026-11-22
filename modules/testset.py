"""
Test set: the model's golden questions, run on demand and before each deploy.

The golden questions are the verified examples (onto["examples"]: a question plus the SQL a person
confirmed), so there is one list to keep, used by Ask, the Ossie export and the Genie space alike.

A run asks each question again through Ask (from the ontology, and optionally schema-only), runs the
generated SQL and the expected SQL on the data, and compares the results. The last run is saved with the
model (onto["test_runs"], newest first, at most 10) so Publish can show whether the open model still passes.

Pure functions; the caller passes `answer_fn(question) -> {"sql", "status", "error", "result"}` and
`run(sql) -> DataFrame`, so this is unit tested without a model endpoint or warehouse.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone

import pandas as pd

from .ask import is_read_only

MAX_RUNS = 10


def questions(onto: dict) -> list[dict]:
    return [e for e in onto.get("examples") or [] if e.get("question") and e.get("sql")]


def _norm(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 4)
    try:
        f = float(str(v))
        return round(f, 4)
    except ValueError:
        return str(v).strip()


def results_match(got: pd.DataFrame | None, expected: pd.DataFrame | None) -> tuple[bool, str]:
    """Same rows and values, ignoring column names, column order and row order (numbers to 4 decimals)."""
    if got is None or expected is None:
        return False, "no result to compare"
    if len(got) != len(expected):
        return False, f"{len(got)} row(s), expected {len(expected)}"
    if got.shape[1] != expected.shape[1]:
        return False, f"{got.shape[1]} column(s), expected {expected.shape[1]}"

    def cols(df):
        return [[_norm(v) for v in df.iloc[:, i]] for i in range(df.shape[1])]

    def rows(cs, order):
        return sorted(zip(*(cs[i] for i in order)), key=repr)
    g, e = cols(got), cols(expected)
    want = rows(e, range(len(e)))
    # Ignore column order with ONE column mapping for the whole result, so values stay with their column.
    # Only columns holding the same values (as a multiset) can map to each other, which keeps the search small.
    sig = [sorted(c, key=repr) for c in g]
    options = [[j for j in range(len(g)) if sig[j] == sorted(c, key=repr)] for c in e]
    if any(not o for o in options):
        return False, "different values"
    for order in itertools.islice(itertools.product(*options), 5000):
        if len(set(order)) == len(order) and rows(g, order) == want:
            return True, "same result"
    return False, "different values"


def _sql_ok(sql: str) -> bool:
    return is_read_only(str(sql or "").strip().rstrip(";"))


VOLATILE = ("test_runs", "changes", "examples")


def fingerprint(onto: dict) -> dict:
    """What a run tested: the model, as its Apache Ossie export without layout or history (so a saved and
    reopened model keeps the same fingerprint), and the question / SQL pairs."""
    from . import ossie

    def h(x):
        return hashlib.sha1(json.dumps(x, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    doc, _ = ossie.from_ontology({k: v for k, v in onto.items() if k not in VOLATILE},
                                 include_studio=True, include_layout=False)
    return {"model": h(doc),
            "questions": h(sorted((e["question"], " ".join(e["sql"].split())) for e in questions(onto)))}


def run_set(onto: dict, answer_fn, run=None, baseline_fn=None, by: str = "", progress=None) -> dict:
    """Ask every golden question again. Returns the run (also stored at the front of onto['test_runs'])."""
    qs = questions(onto)
    items = []
    for i, e in enumerate(qs):
        if progress:
            progress(i / max(len(qs), 1), e["question"][:80])
        item = {"question": e["question"], "expected_sql": e["sql"]}
        expected = None
        if run is not None and not _sql_ok(e["sql"]):
            item["expected_error"] = "not a single read-only query (SELECT or WITH), so it was not run"
        elif run is not None:
            try:
                expected = run(e["sql"])
            except Exception as ex:  # noqa: BLE001
                item["expected_error"] = str(ex).splitlines()[0][:300]
        for mode, fn in (("ontology", answer_fn), ("baseline", baseline_fn)):
            if fn is None:
                continue
            try:
                a = fn(e["question"]) or {}
            except Exception as ex:  # noqa: BLE001
                a = {"status": "error", "error": str(ex)}
            res = {"sql": a.get("sql"), "status": a.get("status")}
            if a.get("status") != "ok" or not a.get("sql"):
                res["passed"], res["why"] = False, a.get("error") or a.get("status") or "no SQL"
            elif run is None:
                same = " ".join(a["sql"].split()).rstrip(";").lower() == " ".join(e["sql"].split()).rstrip(";").lower()
                res["passed"], res["why"] = same, ("same SQL" if same else "SQL differs (no live data to compare)")
            elif "expected_error" in item:
                res["passed"], res["why"] = False, "the expected SQL failed: " + item["expected_error"]
            else:
                got = a.get("result")
                if got is None and not _sql_ok(a["sql"]):
                    res["passed"], res["why"] = False, "the generated SQL is not a single read-only query"
                    item[mode] = res
                    continue
                if got is None:
                    try:
                        got = run(a["sql"])
                    except Exception as ex:  # noqa: BLE001
                        res["passed"], res["why"] = False, "the generated SQL failed: " + str(ex).splitlines()[0][:300]
                        item[mode] = res
                        continue
                res["passed"], res["why"] = results_match(got, expected)
            item[mode] = res
        items.append(item)
    if progress:
        progress(1.0, "done")
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": by or None,
           "total": len(items), "passed": sum(1 for x in items if (x.get("ontology") or {}).get("passed")),
           "baseline_passed": (sum(1 for x in items if (x.get("baseline") or {}).get("passed"))
                               if baseline_fn is not None else None),
           "fingerprint": fingerprint(onto), "items": items}
    runs = onto.setdefault("test_runs", [])
    runs.insert(0, out)
    del runs[MAX_RUNS:]
    return out


def last_run(onto: dict) -> dict | None:
    runs = onto.get("test_runs") or []
    return runs[0] if runs else None


def status(onto: dict) -> dict:
    """{"total", "passed", "stale", "verdict"} for the Publish step and the sidebar."""
    qs, r = questions(onto), last_run(onto)
    if not qs:
        return {"total": 0, "passed": 0, "stale": False, "verdict": "no test questions yet"}
    if not r:
        return {"total": len(qs), "passed": 0, "stale": True, "verdict": f"{len(qs)} question(s), not run yet"}
    fp, now = r.get("fingerprint"), fingerprint(onto)
    if fp:
        why = [w for w, k in (("questions", "questions"), ("model", "model")) if fp.get(k) != now[k]]
    else:                                                        # runs saved before fingerprints existed
        why = ["questions"] if {x["question"] for x in r["items"]} != {e["question"] for e in qs} else []
    stale = bool(why)
    return {"total": r["total"], "passed": r["passed"], "stale": stale,
            "verdict": f"{r['passed']} of {r['total']} pass"
                       + (f" ({' and '.join(why)} changed since, run again)" if stale else "")}


def regressions(onto: dict) -> list[str]:
    """Questions that passed in the previous run and fail in the last one."""
    runs = onto.get("test_runs") or []
    if len(runs) < 2:
        return []
    prev = {x["question"]: (x.get("ontology") or {}).get("passed") for x in runs[1]["items"]}
    return [x["question"] for x in runs[0]["items"]
            if prev.get(x["question"]) and not (x.get("ontology") or {}).get("passed")]
