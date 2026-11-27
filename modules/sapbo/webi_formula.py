"""
Web Intelligence formula language: just enough to classify report variables and translate the safe ones.

    classify(formula, qualification) -> {
        "kind": arithmetic | conditional_dimension | time | presentation | context | unknown,
        "references": [object or variable names],
        "functions": [Webi functions used],
        "translation": {...} | None,     # how WS4/WS5 can represent it
        "status": Direct | Transformed | Partial | Consumer specific | Manual,
        "reason": "...",
    }

Translations:
  arithmetic             -> SQL expression over measures, written with {Name} placeholders; a / b becomes
                            a / NULLIF(b, 0) (Webi shows #DIV/0, SQL would fail or return NULL)
  conditional_dimension  -> CASE WHEN … over dimensions (If/ElseIf/Else with = comparisons)
  time                   -> window spec: RelativeValue(m; (d); -n) = offset, RunningSum(m; (d)) = cumulative
                            reset by d, m + Previous(m) + Previous(m; 2) … = trailing N periods
  presentation           -> Rank, Percentage, formatting: stays in the BI tool
  context                -> In / ForEach / ForAll: needs a human (level-of-detail logic)
"""
from __future__ import annotations

import re

TIME_FUNCS = {"RELATIVEVALUE", "RUNNINGSUM", "PREVIOUS", "TODATE", "RUNNINGAVERAGE", "RUNNINGCOUNT",
              "LASTDAYOFMONTH", "MONTHSBETWEEN", "RELATIVEDATE"}
PRESENTATION_FUNCS = {"RANK", "PERCENTAGE", "FORMATNUMBER", "FORMATDATE", "NAMEOF", "PAGE", "NUMBEROFPAGES",
                      "PERCENTILE"}
CONTEXT_OPS = {"IN", "FOREACH", "FORALL"}
AGG_FUNCS = {"SUM", "MAX", "MIN", "AVERAGE", "COUNT", "MEDIAN"}

_REF = re.compile(r"\[([^\]]+)\]")
_FUNC = re.compile(r"\b([A-Za-z]+)\s*\(")
_WORD = re.compile(r"\b(In|ForEach|ForAll|If|Then|ElseIf|Else|And|Or|Not)\b")


def references(formula: str) -> list[str]:
    return list(dict.fromkeys(_REF.findall(formula or "")))


def functions(formula: str) -> list[str]:
    body = _REF.sub("X", formula or "")
    return list(dict.fromkeys(f.upper() for f in _FUNC.findall(body)))


def _snake(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_").lower()


def _arith_sql(body: str) -> str | None:
    """[A]/[B]*100 -> {A} / NULLIF({B}, 0) * 100, via sqlglot so precedence is right."""
    import sqlglot
    from sqlglot import exp
    refs = references(body)
    tokens = {r: f"__ref{i}__" for i, r in enumerate(refs)}
    text = body
    for r, t in tokens.items():
        text = text.replace(f"[{r}]", t)
    if re.search(r"[^\w\s\.\+\-\*/\(\)]", text):
        return None
    try:
        tree = sqlglot.parse_one(text)
    except Exception:  # noqa: BLE001
        return None

    def guard(node):
        if isinstance(node, exp.Div) and not (isinstance(node.expression, exp.Anonymous)
                                              and node.expression.name.upper() == "NULLIF"):
            return exp.Div(this=node.this, expression=exp.Nullif(this=node.expression, expression=exp.Literal.number(0)))
        return node
    out = tree.transform(guard).sql()
    for r, t in tokens.items():
        out = out.replace(t, "{" + r + "}")
    return out


def _case_sql(body: str) -> str | None:
    """If([D]="a") Then "x" ElseIf([D]="b") Then "y" Else "z"  ->  CASE WHEN {D} = 'a' THEN 'x' ... END"""
    parts = re.split(r"\b(ElseIf|Else|Then)\b", body.strip())
    if not parts or not parts[0].strip().startswith("If"):
        return None
    cond = parts[0].strip()[2:].strip()
    whens, default = [], None
    i, cur = 1, cond
    while i < len(parts):
        kw, val = parts[i], parts[i + 1].strip() if i + 1 < len(parts) else ""
        if kw == "Then":
            whens.append((cur, val))
        elif kw == "ElseIf":
            cur = val
        elif kw == "Else":
            default = val
        i += 2

    def cond_sql(c: str) -> str | None:
        c = c.strip()
        if c.startswith("(") and c.endswith(")"):
            c = c[1:-1]
        m = re.fullmatch(r"\s*\[([^\]]+)\]\s*(=|<>|>=|<=|>|<)\s*(\"[^\"]*\"|-?\d+(?:\.\d+)?)\s*", c)
        if not m:
            return None
        lit = m.group(3).replace('"', "'")
        return "{" + m.group(1) + "} " + m.group(2) + " " + lit

    def val_sql(v: str) -> str:
        return v.replace('"', "'")
    out = []
    for c, v in whens:
        cs = cond_sql(c)
        if cs is None:
            return None
        out.append(f"WHEN {cs} THEN {val_sql(v)}")
    sql = "CASE " + " ".join(out)
    if default is not None:
        sql += f" ELSE {val_sql(default)}"
    return sql + " END"


def _time(body: str) -> dict | None:
    b = body.strip()
    m = re.fullmatch(r"RelativeValue\(\s*\[([^\]]+)\]\s*;\s*\(\s*\[([^\]]+)\]\s*\)\s*;\s*(-?\d+)\s*\)", b, re.I)
    if m:
        return {"window": "offset", "measure": m.group(1), "along": m.group(2), "offset": int(m.group(3))}
    m = re.fullmatch(r"RunningSum\(\s*\[([^\]]+)\]\s*(?:;\s*\(\s*\[([^\]]+)\]\s*\))?\s*\)", b, re.I)
    if m:
        return {"window": "cumulative", "measure": m.group(1), "reset_by": m.group(2)}
    terms = [t.strip() for t in b.split("+")]
    if len(terms) >= 2:
        base = re.fullmatch(r"\[([^\]]+)\]", terms[0])
        if base:
            offsets = [0]
            for t in terms[1:]:
                pm = re.fullmatch(r"Previous\(\s*\[([^\]]+)\]\s*(?:;\s*(\d+))?\s*\)", t, re.I)
                if not pm or pm.group(1) != base.group(1):
                    return None
                offsets.append(int(pm.group(2) or 1))
            if sorted(offsets) == list(range(len(offsets))):
                return {"window": "trailing", "measure": base.group(1), "periods": len(offsets)}
    return None


def classify(formula: str, qualification: str = "Measure") -> dict:
    body = (formula or "").strip()
    if body.startswith("="):
        body = body[1:].strip()
    refs, funcs = references(body), functions(body)
    words = {w.upper() for w in _WORD.findall(_REF.sub("X", body))}
    out = {"kind": "unknown", "references": refs, "functions": funcs, "translation": None,
           "status": "Manual", "reason": ""}
    if words & CONTEXT_OPS:
        out.update(kind="context", status="Manual",
                   reason="Uses a Webi context operator (In/ForEach/ForAll): level-of-detail logic a human must "
                          "re-express (e.g. a grouped subquery or a separate metric view).")
        return out
    if set(funcs) & PRESENTATION_FUNCS:
        out.update(kind="presentation", status="Consumer specific",
                   reason="Ranking/percentage/formatting depends on what the visual shows; keep it in the BI layer.")
        return out
    if set(funcs) & TIME_FUNCS:
        t = _time(body)
        out.update(kind="time", translation=t, status="Transformed" if t else "Manual",
                   reason=("Becomes a metric view window measure." if t else
                           "Time function pattern not recognised; translate by hand."))
        return out
    if "IF" in words or body.upper().startswith("IF"):
        sql = _case_sql(body)
        out.update(kind="conditional_dimension" if qualification != "Measure" else "conditional_measure",
                   translation={"sql": sql} if sql else None, status="Transformed" if sql else "Manual",
                   reason="If/Then/Else becomes CASE WHEN." if sql else "Condition too complex to translate.")
        return out
    if set(funcs) - AGG_FUNCS:
        out.update(reason=f"Unsupported function(s): {', '.join(sorted(set(funcs) - AGG_FUNCS))}.")
        return out
    sql = _arith_sql(body)
    if sql:
        guarded = "NULLIF" in sql.upper()
        out.update(kind="arithmetic", translation={"sql": sql}, status="Transformed" if guarded else "Direct",
                   reason="Arithmetic over measures; becomes a composed measure (MEASURE(a) / MEASURE(b))."
                          + (" Division guarded with NULLIF." if guarded else ""))
    else:
        out["reason"] = "Could not parse the formula."
    return out


def placeholders_to(sql: str, resolve) -> str:
    """Replace {Name} placeholders using resolve(name) -> SQL text."""
    return re.sub(r"\{([^}]+)\}", lambda m: resolve(m.group(1)), sql)


snake = _snake
