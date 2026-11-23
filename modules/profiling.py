"""
What profiling may keep and what it can infer, before any model or LLM sees the data. Rules ported from
NVIDIA Auto Ontology (Apache-2.0, see NOTICE):

  pii_category       column names that hold personal data, judged with their table (pii_detection/rules.py)
  protect            tags a model's personal-data properties `pii: <category>` and drops every value stored for
                     them: samples, allowed values, value meanings, min/max (profiling never samples them)
  infer_date_format  the notation a text column stores its dates in, e.g. MM/DD/YYYY (semantic/date_format.py)
  keep_samples       the most common values worth keeping: numbers, and text of 30 characters or less
                     (semantic/visit_enter.py)
  transient          an error that says the warehouse stopped answering, not that the SQL was wrong
                     (connectors/db_errors.py); after MAX_TRANSIENT of them in a row profiling stops
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from functools import lru_cache

# --------------------------------------------------------------------------- #
# Personal data (metadata-only rules)
# --------------------------------------------------------------------------- #
# Role or channel words that may precede a personal field (customer_email); longer ones first.
_FIELD_PREFIXES = ("preferred|personal|primary|secondary|alternate|business|billing|shipping|mailing|customer|"
                   "employee|contact|patient|member|person|client|user|household|home|work|postal")
# Person roles that may precede an identifier (customer_id); home/work/billing left out so home_id is no key.
_ID_PREFIXES = "employee|customer|contact|patient|member|person|client|user|account"


def _rule(words: str, category: str, compact: str, prefixes: str | None = _FIELD_PREFIXES):
    """A rule that must describe the whole column name, as words (customer email) or run together
    (CustomerEmail), so derived flags such as no_email or email_hash do not match."""
    if prefixes:
        return (re.compile(rf"^(?:(?:{prefixes}) )*(?:{words})$"), re.compile(rf"^(?:{prefixes})*(?:{compact})$"),
                category)
    return re.compile(rf"^(?:{words})$"), re.compile(rf"^(?:{compact})$"), category


DIRECT = (                              # personal wherever they appear
    _rule(r"social security(?: number)?", "government_id", r"ssn|socialsecurity(?:number)?"),
    _rule(r"passport(?: number)?|driver(?:s)? license", "government_id", r"passport(?:number)?|drivers?license"),
    _rule(r"taxpayer id|tax id|national id", "government_id", r"taxpayerid|taxid|nationalid"),
    _rule(r"e mail|email address", "email_address", r"emailaddress|email"),
    _rule(r"phone number|mobile phone|mobile number|cell phone|fax number", "phone_number",
          r"phonenumber|faxnumber|cellphone|mobilephone|telephone|mobile|phone|fax|cell"),
    _rule(r"date of birth|birth date|birthdate|dob", "date_of_birth", r"dateofbirth|birthdate|dob"),
    _rule(r"credit card|debit card|card number", "payment_card", r"creditcard|debitcard|cardnumber"),
    _rule(r"bank account(?: number)?|iban|swift code", "banking", r"bankaccount(?:number)?|iban|swiftcode"),
    _rule(r"passcode|pin code", "credential", r"password|passcode|pincode"),
    _rule(r"fingerprint|biometric|retina scan|face print", "biometric",
          r"fingerprint|biometric|retinascan|faceprint"),
    _rule(r"ip address|mac address", "online_identifier", r"ipaddress|macaddress"),
)
CONDITIONAL = (                         # personal only in a table of people
    _rule(r"first name|last name|full name|middle name|maiden name|preferred name", "person_name",
          r"firstname|lastname|fullname|middlename|maidenname|preferredname"),
    _rule(r"name", "person_name", r"name", prefixes=None),
    _rule(r"address|street|postal code|zip code", "postal_address", r"address(?:line\d*)?|street|postalcode|zipcode"),
    _rule(r"age|gender|sex|marital status", "personal_attribute", r"age|gender|sex|maritalstatus", prefixes=None),
    _rule(r"salary|income|credit score", "financial", r"salary|income|creditscore", prefixes=None),
    _rule(r"id|account number", "identifier", r"id|accountnumber", prefixes=_ID_PREFIXES),
)
PERSON_TABLE = re.compile(r"^(?:persons?|people|customers?|clients?|contacts?|users?|employees?|patients?|"
                          r"applicants?|members?|students?|advisors?|representatives?|households?|account holders?)$")
NOT_PII = re.compile(r"^(?:product name|account type|account status|employee count|bank name|firm id|"
                     r"buying group name|city name|customer category name|last password change|created at|updated at)$")
NOT_PII_COMPACT = re.compile(r"^(?:productname|accounttype|accountstatus|employeecount|bankname|firmid|"
                             r"buyinggroupname|cityname|customercategoryname|lastpasswordchange|createdat|updatedat)$")
# Not in Auto Ontology: warehouse layer and modeling affixes, so dim_customer and silver_customers are people too.
_TABLE_AFFIX = re.compile(r"^(?:dim|fact|fct|tbl|stg|vw|lkp|ref|raw|src|bronze|silver|gold|d|f|t|v)_|"
                          r"_(?:dim|fact|fct|tbl|lkp|lookup)$", re.I)


def _norm(value) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


@lru_cache(maxsize=65536)
def pii_category(table: str | None, column: str | None) -> str | None:
    """The kind of personal data a column holds (email_address, person_name ...), judged from its name and its
    table's name (catalog.schema.table is fine); None when the name does not mark it as personal."""
    col = _norm(column)
    packed = col.replace(" ", "")
    if not col or NOT_PII.fullmatch(col) or NOT_PII_COMPACT.fullmatch(packed):
        return None
    for words, run_together, category in DIRECT:
        if words.fullmatch(col) or run_together.fullmatch(packed):
            return category
    if PERSON_TABLE.fullmatch(_norm(_TABLE_AFFIX.sub("", str(table or "").rsplit(".", 1)[-1]))):
        for words, run_together, category in CONDITIONAL:
            if words.fullmatch(col) or run_together.fullmatch(packed):
                return category
    return None


def protect(onto: dict) -> dict:
    """Tag the data properties that hold personal data `pii: <category>` and drop the values stored for them
    (samples, allowed values, value meanings, min/max in the column profile), so they never reach a prompt, an
    export or a saved version. A `pii` a person set on a property, false included, is kept."""
    private = set()
    for cid, c in (onto.get("classes") or {}).items():
        table = c.get("source_fqn") or c.get("source_table") or cid
        props = c.get("data_properties") or {}
        flags = {}
        for pid, p in props.items():
            col = str(p.get("source_column") or pid)
            if "pii" not in p and (cat := pii_category(table, col)):
                p["pii"] = cat
            flags[col.lower()] = p.get("pii")
            if p.get("pii"):
                p.pop("samples", None)
                p.pop("allowed_values", None)
                private.add((cid, pid))
        for col, d in (c.get("column_profile") or {}).items():
            flag = flags[col.lower()] if col.lower() in flags else pii_category(table, col)
            if flag and isinstance(d, dict):
                d.pop("min", None)
                d.pop("max", None)
    if private and onto.get("value_meanings"):
        onto["value_meanings"] = [v for v in onto["value_meanings"]
                                  if (v.get("class"), v.get("property")) not in private]
    return onto


# --------------------------------------------------------------------------- #
# Date formats of text columns
# --------------------------------------------------------------------------- #
_MAX_VALUES, _MAX_VALUE_LEN = 200, 40
# strptime pattern -> the notation the SQL writer is told, most specific first. A pattern counts only when it
# parses every value, so 13/07/2011 rules out month-first on its own and a mixed column yields no claim.
_FORMATS = (
    ("%Y-%m-%d %H:%M:%S.%f", "YYYY-MM-DD HH:MM:SS.f"),
    ("%Y-%m-%dT%H:%M:%S.%f", "YYYY-MM-DDTHH:MM:SS.f"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%Y-%m-%dT%H:%M:%S", "YYYY-MM-DDTHH:MM:SS"),
    ("%Y-%m-%d %H:%M", "YYYY-MM-DD HH:MM"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m-%d-%Y", "MM-DD-YYYY"),
    ("%d.%m.%Y", "DD.MM.YYYY"),
    ("%d/%m/%y", "DD/MM/YY"),
    ("%m/%d/%y", "MM/DD/YY"),
    ("%d-%m-%y", "DD-MM-YY"),
    ("%m-%d-%y", "MM-DD-YY"),
    ("%d.%m.%y", "DD.MM.YY"),
    ("%Y%m%d", "YYYYMMDD"),
    ("%Y%m", "YYYYMM"),
    ("%Y-%m", "YYYY-MM"),               # not in Auto Ontology: monthly periods such as 2024-07
    ("%y%m%d", "YYMMDD"),
    ("%H:%M:%S", "HH:MM:SS"),
    ("%H:%M", "HH:MM"),
    ("%Y", "YYYY"),
)
# Readings that differ only in which part comes first: when both parse every value, say nothing.
_TWINS = tuple(frozenset(p) for p in (
    ("DD/MM/YYYY", "MM/DD/YYYY"), ("DD-MM-YYYY", "MM-DD-YYYY"), ("DD/MM/YY", "MM/DD/YY"), ("DD-MM-YY", "MM-DD-YY"),
    ("DD.MM.YY", "MM.DD.YY"), ("D/M/YY", "M/D/YY"), ("D-M-YY", "M-D-YY"), ("D.M.YY", "M.D.YY"),
    ("D/M/YYYY", "M/D/YYYY"), ("D-M-YYYY", "M-D-YYYY"), ("D.M.YYYY", "M.D.YYYY")))


def _parses_all(pattern: str, values: list[str]) -> bool:
    """Every value reads as `pattern`; with a four-digit year it must fall in 1900-2099 (950324 is not year 9503)."""
    for v in values:
        try:
            d = datetime.strptime(v, pattern)
        except ValueError:
            return False
        if "%Y" in pattern and not 1900 <= d.year <= 2099:
            return False
    return True


def _narrow(label: str, values: list[str]) -> str:
    """A date-only notation at the width stored: 4/4/20 is M/D/YY, which a 04/04/20 literal never matches."""
    if any(ch in label for ch in " T:"):
        return label
    sep = next((s for s in "/-." if s in label), None)
    if sep and any(len(part) == 1 for v in values for part in v.split(sep)):
        return label.replace("MM", "M").replace("DD", "D")
    return label


def infer_date_format(values) -> str | None:
    """The notation every value follows (YYYY-MM-DD, MM/DD/YYYY, YYYYMMDD ...), or None when the values are no
    dates, disagree with each other, or cannot tell day-first from month-first."""
    seen: list[str] = []
    for v in values:
        text = "" if v is None else str(v).strip()
        if text and len(text) <= _MAX_VALUE_LEN and text not in seen:
            seen.append(text)
            if len(seen) >= _MAX_VALUES:
                break
    if not seen:
        return None
    matches = [(pattern, _narrow(lbl, seen)) for pattern, lbl in _FORMATS if _parses_all(pattern, seen)]
    if not matches:
        return None
    pattern, winner = matches[0]
    labels = {lbl for _, lbl in matches}
    if any(winner in twins and len(twins & labels) > 1 for twins in _TWINS):
        return None
    if "%f" in pattern:                 # .f is as wide as the widest fraction stored (19:39:07.0 -> .f)
        winner = winner.replace(".f", "." + "f" * max((len(v.rpartition(".")[2]) for v in seen if "." in v), default=1))
    return winner


# --------------------------------------------------------------------------- #
# Samples and the profiling circuit breaker
# --------------------------------------------------------------------------- #
MAX_SAMPLE_LEN = 30
MAX_TRANSIENT = 3


def keep_samples(values, n: int = 5) -> list:
    """The n most common values (first seen wins a tie), keeping numbers, dates and flags, and text only up to
    MAX_SAMPLE_LEN characters: longer text is prose or an id, noise in a prompt."""
    top = [v for v, _ in Counter(v for v in values if v is not None).most_common(n)]
    return [v for v in top if not isinstance(v, (bytes, bytearray)) and not (isinstance(v, str) and len(v) > MAX_SAMPLE_LEN)]


_TRANSIENT = re.compile(
    r"timed out|timeout exceeded|connection (?:refused|reset|aborted)|broken pipe|could not connect|failed to connect|"
    r"max retries exceeded|error during request to server|temporarily unavailable|service unavailable|bad gateway|"
    r"gateway time-?out|name or service not known|temporary failure in name resolution|remotedisconnected|"
    r"socket is closed|invalid session ?handle|session (?:does not exist|is closed|has been closed|not found)", re.I)


def transient(error: BaseException) -> bool:
    """True for a timeout or a lost connection (the warehouse stopped answering); False for errors another query
    could avoid (permissions, a type the driver cannot read), which must not stop profiling of other tables."""
    return isinstance(error, (TimeoutError, ConnectionError)) or bool(_TRANSIENT.search(str(error)))
