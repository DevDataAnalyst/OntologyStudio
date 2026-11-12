"""
Observability for Ask: one trace per request, user feedback, token cost and per-user budgets.

A trace records what the request did: the question, who asked, the domain scope, which
ontology assets went into the prompt (classes, metrics, rules, pages, snippets), prompt size,
model and SQL timings, rows returned, errors and the user's 👍/👎 feedback.

Traces live in the session and, when TELEMETRY_TABLE (catalog.schema.table) is set, are also
appended to a Delta table with the viewer's permissions, so they survive restarts and can be
queried, filtered and charted in Databricks SQL like any other table.

Warehouse spend comes from system.billing.usage (+ list_prices) for SQL_WAREHOUSE_ID and is split
per user by their share of query time in system.query.history.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone

TELEMETRY_TABLE = os.getenv("TELEMETRY_TABLE", "").strip()
# Pay-per-token list prices are USD per 1M tokens; editable in the Monitor tab.
DEFAULT_PRICE_IN = float(os.getenv("FM_PRICE_PER_M_INPUT", "0.50"))
DEFAULT_PRICE_OUT = float(os.getenv("FM_PRICE_PER_M_OUTPUT", "1.50"))
DEFAULT_DAILY_TOKEN_BUDGET = int(os.getenv("ASK_DAILY_TOKEN_BUDGET", "0") or 0)   # 0 = no cap

COLUMNS = ["id", "ts", "user", "question", "mode", "domain", "status", "error", "sql", "rows",
           "prompt_tokens", "completion_tokens", "llm_ms", "sql_ms", "cost_usd", "feedback",
           "feedback_note", "assets"]


def estimate_tokens(text: str) -> int:
    """~4 characters per token: close enough for budgeting and comparing prompt sizes."""
    return max(1, len(text or "") // 4)


def new_trace(question: str, user: str | None, mode: str, domain: str | None) -> dict:
    return {"id": uuid.uuid4().hex[:12], "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "user": user or "unknown", "question": question, "mode": mode, "domain": domain or "All",
            "status": "running", "error": None, "sql": None, "rows": None, "prompt_tokens": 0,
            "completion_tokens": 0, "llm_ms": None, "sql_ms": None, "cost_usd": 0.0, "feedback": None,
            "feedback_note": None, "assets": {}}


def assets_used(ctx_onto: dict) -> dict:
    """Which ontology assets a (scoped) context carried into the prompt."""
    return {
        "classes": sorted(ctx_onto.get("classes", {})),
        "metrics": [m["name"] for m in ctx_onto.get("metrics", [])],
        "certified_metrics": [m["name"] for m in ctx_onto.get("metrics", []) if m.get("status") == "certified"],
        "rules": [r["name"] for r in ctx_onto.get("rules", [])],
        "pages": [p["title"] for p in ctx_onto.get("pages", []) if p.get("status") != "deprecated"],
        "snippets": len(ctx_onto.get("_snippets_in_context", [])),
    }


def sql_classes(sql: str, onto: dict, dialect: str = "databricks") -> list[str]:
    """Classes whose tables the generated SQL reads (for clustering feedback by asset)."""
    try:
        import sqlglot
        from sqlglot import exp
        look = {}
        for cid, c in onto.get("classes", {}).items():
            for k in (c.get("source_table"), c.get("source_fqn"), cid):
                if k:
                    look[str(k).lower()] = cid
        out = set()
        for tree in sqlglot.parse(sql, read=dialect):
            for t in (tree.find_all(exp.Table) if tree else []):
                full = ".".join(p for p in (t.catalog, t.db, t.name) if p).lower()
                cid = look.get(full) or look.get(t.name.lower())
                if cid:
                    out.add(cid)
        return sorted(out)
    except Exception:  # noqa: BLE001
        return []


def price(trace: dict, price_in: float = DEFAULT_PRICE_IN, price_out: float = DEFAULT_PRICE_OUT) -> float:
    return round(trace.get("prompt_tokens", 0) / 1e6 * price_in
                 + trace.get("completion_tokens", 0) / 1e6 * price_out, 6)


def tokens_today(traces: list[dict], user: str) -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(t.get("prompt_tokens", 0) + t.get("completion_tokens", 0) for t in traces
               if t.get("user") == user and str(t.get("ts", "")).startswith(day))


def over_budget(traces: list[dict], user: str, budget: int) -> bool:
    return bool(budget) and tokens_today(traces, user) >= budget


# --------------------------------------------------------------------------- #
# Delta table persistence (optional)
# --------------------------------------------------------------------------- #
def table_enabled() -> bool:
    return TELEMETRY_TABLE.count(".") == 2 and all(re.match(r"^[A-Za-z0-9_\-]+$", p)
                                                   for p in TELEMETRY_TABLE.split("."))


def _bq(name: str) -> str:
    return ".".join(f"`{p}`" for p in name.split("."))


DDL = ("CREATE TABLE IF NOT EXISTS {t} (`id` STRING, `ts` TIMESTAMP, `user` STRING, `question` STRING, `mode` STRING, "
       "`domain` STRING, `status` STRING, `error` STRING, `sql` STRING, `rows` BIGINT, prompt_tokens BIGINT, "
       "completion_tokens BIGINT, llm_ms BIGINT, sql_ms BIGINT, cost_usd DOUBLE, feedback STRING, "
       "feedback_note STRING, assets STRING) COMMENT 'Ontology Studio Ask traces'")


def save_trace(run, trace: dict) -> str | None:
    """Insert (or, for feedback, update) one trace. Returns an error message or None."""
    if not table_enabled():
        return None
    t = _bq(TELEMETRY_TABLE)
    row = {k: trace.get(k) for k in COLUMNS}
    row["assets"] = json.dumps(trace.get("assets") or {})
    try:
        run(DDL.format(t=t))
        run(f"DELETE FROM {t} WHERE id = :id", {"id": row["id"]})
        cols = ", ".join(f"`{c}`" for c in COLUMNS)
        marks = ", ".join(f"CAST(:{k} AS TIMESTAMP)" if k == "ts" else f":{k}" for k in COLUMNS)
        run(f"INSERT INTO {t} ({cols}) VALUES ({marks})", row)
        return None
    except Exception as e:  # noqa: BLE001
        return f"Trace not saved to {TELEMETRY_TABLE}: {str(e)[:200]}"


def tokens_today_stored(run, user: str) -> int:
    """Tokens the user spent today according to TELEMETRY_TABLE (all sessions)."""
    if not table_enabled():
        return 0
    df = run(f"SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t FROM {_bq(TELEMETRY_TABLE)} "
             "WHERE `user` = :u AND `ts` >= current_date()", {"u": user})
    return int(float(df.iloc[0, 0] or 0)) if len(df) else 0


def load_traces(run, days: int = 30, limit: int = 5000) -> list[dict]:
    if not table_enabled():
        return []
    df = run(f"SELECT {', '.join(f'`{c}`' for c in COLUMNS)}, CAST(`ts` AS STRING) AS ts_s FROM {_bq(TELEMETRY_TABLE)} "
             f"WHERE `ts` >= current_timestamp() - make_dt_interval(:d) ORDER BY `ts` DESC LIMIT {int(limit)}",
             {"d": int(days)})
    out = []
    for r in df.to_dict("records"):
        r["ts"] = str(r.pop("ts_s"))[:19]
        try:
            r["assets"] = json.loads(r.get("assets") or "{}")
        except Exception:  # noqa: BLE001
            r["assets"] = {}
        out.append(r)
    return out


def merge(session: list[dict], stored: list[dict]) -> list[dict]:
    by_id = {t["id"]: t for t in stored}
    by_id.update({t["id"]: t for t in session})
    return sorted(by_id.values(), key=lambda t: str(t.get("ts")), reverse=True)


# --------------------------------------------------------------------------- #
# Warehouse cost from system tables
# --------------------------------------------------------------------------- #
def warehouse_cost(run, warehouse_id: str, days: int = 30):
    """(daily DataFrame, per-user DataFrame). Daily: DBUs and list-price USD for the warehouse.
    Per user: share of query time on that warehouse, applied to the period's cost."""
    daily = run(
        "SELECT u.usage_date, SUM(u.usage_quantity) AS dbus, "
        "SUM(u.usage_quantity * p.pricing.default) AS usd "
        "FROM system.billing.usage u LEFT JOIN system.billing.list_prices p "
        "ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit "
        "AND u.usage_start_time >= p.price_start_time "
        "AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time) "
        "WHERE u.usage_metadata.warehouse_id = :w AND u.usage_date >= date_sub(current_date(), :d) "
        "GROUP BY u.usage_date ORDER BY u.usage_date", {"w": warehouse_id, "d": int(days)})
    users = run(
        "SELECT executed_by AS user, COUNT(*) AS queries, SUM(total_duration_ms) / 1000.0 AS seconds "
        "FROM system.query.history WHERE compute.warehouse_id = :w "
        "AND start_time >= current_timestamp() - make_dt_interval(:d) "
        "GROUP BY executed_by ORDER BY seconds DESC", {"w": warehouse_id, "d": int(days)})
    total = float(daily["usd"].fillna(0).sum()) if len(daily) else 0.0
    secs = float(users["seconds"].fillna(0).sum()) if len(users) else 0.0
    if len(users):
        users["share"] = users["seconds"].fillna(0) / secs if secs else 0.0
        users["usd_estimate"] = (users["share"] * total).round(2)
    return daily, users
