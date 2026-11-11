"""
Apache Ossie semantic-model validator.

Checks a YAML or JSON document against the Apache Ossie core specification version it
declares. The checks follow the reference validator in apache/ossie
(validation/validate.py) and add a few modelling lints:

  Parse       YAML/JSON syntax, duplicate keys, one document per file, a mapping at the root
  Version     the declared version picks the schema: 0.3.0.dev0 (current), or the legacy
              0.2.0.dev0 / 0.1.1 profiles (with an upgrade to 0.3.0.dev0)
  Schema      JSON Schema (Draft 2020-12) bundled from apache/ossie: required properties,
              types, DataType enum, expression shape, extension shape, no unknown keys
  Extensions  extension data is JSON (no dates, NaN or non-string keys); YAML scalars
              that silently changed type; vendor-name typos
  Unique      dataset, field, metric and relationship names; fields and dataset-scoped
              metrics share one namespace
  Reference   relationships name declared datasets; keys and join columns name declared
              fields; to_columns covers a key; metric expressions use dataset.field
  Arity       from_columns and to_columns have the same length
  Scope       dataset-scoped metrics use their own fields unqualified, or source.column
  SQL         every SQL-family expression parses as one expression (sqlglot)
  Drift       a metric's dialect variants agree (aggregates, columns, constants, filters)
  Purity      metrics avoid NOW(), RANDOM() and pg_read_file
  Lint        aggregates in fields, metrics without an aggregate, duplicate dialect entries
              and relationships, case-only name clashes, fieldless or isolated datasets,
              ambiguous synonyms

As in the reference validator, the semantic checks run only once the document passes the
schema; findings are errors (not compliant), warnings (compliant but suspicious) or info.
"""
from __future__ import annotations

import copy
import difflib
import itertools
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import yaml
from yaml.constructor import ConstructorError

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match
except ImportError:  # pragma: no cover - listed in requirements.txt
    Draft202012Validator = None

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError, TokenError
    SQLGLOT = True
except ImportError:  # pragma: no cover - listed in requirements.txt
    SQLGLOT = False

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")
CURRENT_VERSION = "0.3.0.dev0"
PORTABLE_DIALECT = "OSSIE_SQL_2026"
STUDIO_VENDOR = "ONTOLOGY_STUDIO"


@dataclass(frozen=True)
class Profile:
    version: str
    schema_file: str
    title: str
    source: str
    ext_key: str            # name of the extension arrays
    ext_data: str           # "object" (0.3.0.dev0) or "string" (JSON text in older versions)
    wrapped: bool = False   # models live in a semantic_model array (0.1.1)
    current: bool = False
    note: str = ""


PROFILES = {
    "0.3.0.dev0": Profile(
        "0.3.0.dev0", "ossie-schema-0.3.0.dev0.json",
        "Apache Ossie 0.3.0.dev0 (current development version)",
        "apache/ossie core-spec/ossie-schema.json @ e6ad94e (2026-11-11)",
        "nonportable_extensions", "object", current=True),
    "0.2.0.dev0": Profile(
        "0.2.0.dev0", "ossie-schema-0.2.0.dev0.json",
        "Apache Ossie 0.2.0.dev0 (former development label)",
        "apache/ossie core-spec/ossie-schema.json @ 21e849d (2026-11-03)",
        "custom_extensions", "string",
        note="0.2.0.dev0 was renamed to 0.3.0.dev0 on 2026-11-09 (apache/ossie#691) and there will "
             "be no 0.2.0 release, so the current schema rejects this version."),
    "0.1.1": Profile(
        "0.1.1", "osi-schema-0.1.1.json",
        "OSI 0.1.1 (first release, 2025-12-11)",
        "apache/ossie core-spec/osi-schema.json @ tag osi-0.1.1-rc1",
        "custom_extensions", "string", wrapped=True,
        note="0.1.1 keeps models in a semantic_model array; since apache/ossie#383 each document "
             "holds one model at the root."),
}
SUPPORTED_VERSIONS = tuple(PROFILES)

# Ossie dialects sqlglot can parse (None = sqlglot's default, ANSI-like dialect) and the
# known non-SQL dialects whose expressions are carried but not parsed (extensions/dialects.md).
SQLGLOT_DIALECTS = {
    "ANSI_SQL": None, PORTABLE_DIALECT: None, "SNOWFLAKE": "snowflake", "DATABRICKS": "databricks",
    "BIGQUERY": "bigquery", "TRINO": "trino", "CLICKHOUSE": "clickhouse", "POSTGRES": "postgres",
    "REDSHIFT": "redshift",
}
NON_SQL_DIALECTS = {"MDX", "TABLEAU", "MAQL", "SIGMA", "THOUGHTSPOT", "DAX", "HOLISTICS_AQL",
                    "SPARQL", "PYICEBERG"}
KNOWN_DIALECTS = (*SQLGLOT_DIALECTS, *sorted(NON_SQL_DIALECTS))
KNOWN_VENDORS = ("SNOWFLAKE", "SALESFORCE", "DBT", "DATABRICKS", "GOODDATA", "SEMANTIDO", "HONEYDEW",
                 "WISDOM", "POWER_BI", "SIGMA", "THOUGHTSPOT", "HEX", "ATACCAMA", "OMNI", "PRECISELY",
                 STUDIO_VENDOR)
DATA_TYPES = ("String", "Integer", "Decimal", "Float", "Boolean", "Date", "Time", "DateTime",
              "DateTimeTz", "Opaque")
TEMPORAL_TYPES = {"Date", "Time", "DateTime", "DateTimeTz"}
SOURCE_QUALIFIER = "source"

CHECKS = ("Parse", "Version", "Schema", "Extensions", "Unique", "Reference", "Arity", "Scope", "SQL",
          "Drift", "Purity", "Lint")
CHECK_TITLES = {
    "Parse": "YAML / JSON parses as one document",
    "Version": "Declared spec version",
    "Schema": "JSON Schema (structure, types, enums)",
    "Extensions": "Extension data is valid JSON",
    "Unique": "Unique names",
    "Reference": "References resolve (datasets, fields, keys)",
    "Arity": "Relationship column counts match",
    "Scope": "Dataset-scoped metric scoping",
    "SQL": "SQL expressions parse",
    "Drift": "Dialect variants agree",
    "Purity": "Metrics are reproducible reads",
    "Lint": "Modelling and AI-readiness lints",
}
SEMANTIC_CHECKS = ("Unique", "Reference", "Arity", "Scope", "SQL", "Drift", "Purity", "Lint")


# --------------------------------------------------------------------------- #
# Findings and report
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    severity: str            # error | warning | info
    check: str               # one of CHECKS
    message: str
    path: str = ""           # e.g. datasets[0].fields[2].expression
    hint: str = ""
    line: int | None = None  # 1-based line in the uploaded file, when known

    def as_dict(self) -> dict:
        return {"severity": self.severity, "check": self.check, "path": self.path,
                "line": self.line, "message": self.message, "hint": self.hint}


@dataclass
class Report:
    findings: list = field(default_factory=list)
    file_name: str = ""
    version: object = None                # what the document declares
    profile: Profile | None = None        # the schema it was checked against
    document: object = None               # parsed document (None when it does not parse)
    models: list = field(default_factory=list)   # [(path prefix, model dict)]
    checks: dict = field(default_factory=dict)   # check -> passed | warnings | failed | skipped
    stats: dict = field(default_factory=dict)
    strict: bool = False

    @property
    def errors(self) -> list:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def infos(self) -> list:
        return [f for f in self.findings if f.severity == "info"]

    @property
    def ok(self) -> bool:
        return self.document is not None and not self.errors

    @property
    def legacy(self) -> bool:
        return self.profile is not None and not self.profile.current

    @property
    def convertible(self) -> bool:
        """At least one model with a list of named datasets: enough to draw a network."""
        return any(isinstance(m.get("datasets"), list) and any(
            isinstance(d, dict) and isinstance(d.get("name"), str) for d in m["datasets"])
            for _, m in self.models)

    def model_names(self) -> list[str]:
        return [str(m.get("name") or f"model {i + 1}") for i, (_, m) in enumerate(self.models)]

    def headline(self) -> str:
        ver = self.profile.version if self.profile else "?"
        if self.document is None:
            return "Not an Ossie document: it does not parse"
        if self.errors:
            return (f"Not compliant: {_plural(len(self.errors), 'error')}, "
                    f"{_plural(len(self.warnings), 'warning')}")
        return f"Valid Apache Ossie {ver} document" + (
            f" with {_plural(len(self.warnings), 'warning')}" if self.warnings else "")

    def to_markdown(self) -> str:
        lines = [f"# Apache Ossie validation report: {self.file_name or 'document'}", "",
                 f"**Result:** {self.headline()}", ""]
        if self.profile:
            lines += [f"- Checked against: {self.profile.title}", f"- Schema source: {self.profile.source}"]
        lines += [f"- Declared version: {self.version!r}", f"- Strict (current version required): {self.strict}",
                  ""]
        if self.stats:
            lines += ["## Model", ""] + [f"- {k.replace('_', ' ')}: {v}" for k, v in self.stats.items()] + [""]
        lines += ["## Checks", "", "| Check | Status |", "|---|---|"]
        for c in CHECKS:
            if c in self.checks:
                lines.append(f"| {CHECK_TITLES[c]} | {self.checks[c]} |")
        lines += ["", "## Findings", ""]
        if not self.findings:
            lines.append("None.")
        for f in self.findings:
            loc = f.path or "(root)"
            if f.line:
                loc += f" (line {f.line})"
            lines.append(f"- **{f.severity.upper()}** [{f.check}] {loc}: {f.message}"
                         + (f" _Fix:_ {f.hint}" if f.hint else ""))
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
class _Loader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys (merge keys excepted) and reports scalars it
    cannot build (2024-02-30, !!int abc) as YAML errors at their position."""

    def construct_document(self, node):
        self._check_keys(node, set())
        return super().construct_document(node)

    def _check_keys(self, node, visited: set) -> None:
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    self._check_keys(value_node, visited)
                    continue
                if not isinstance(key_node, yaml.ScalarNode):
                    raise ConstructorError("while constructing a mapping", node.start_mark,
                                           "found an unhashable key", key_node.start_mark)
                key = (self.construct_scalar(key_node) if key_node.tag == "tag:yaml.org,2002:value"
                       else self.construct_object(key_node, deep=True))
                try:
                    hash(key)
                except TypeError:
                    raise ConstructorError("while constructing a mapping", node.start_mark,
                                           "found an unhashable key", key_node.start_mark) from None
                if key in seen:
                    raise ConstructorError("while constructing a mapping", node.start_mark,
                                           f"found duplicate key {key!r}", key_node.start_mark)
                seen.add(key)
                self._check_keys(value_node, visited)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                self._check_keys(child, visited)

    def construct_object(self, node, deep=False):
        try:
            return super().construct_object(node, deep)
        except (ValueError, AttributeError, LookupError) as e:
            tag = node.tag.rsplit(":", 1)[-1]
            raise ConstructorError(None, None, f"cannot build {node.value!r} as {tag}",
                                   node.start_mark) from e


class _DuplicateKey(ValueError):
    pass


def _unique_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise _DuplicateKey(k)
        out[k] = v
    return out


def _reject_constant(name):
    raise ValueError(f"{name} is not a JSON value")


def load_text(text: str, file_name: str = ""):
    """Parse YAML or JSON. Returns (data, problems) where problems are Parse findings."""
    if text is None or not str(text).strip():
        return None, [Finding("error", "Parse", "The file is empty.")]
    text = str(text).lstrip("﻿")
    looks_json = file_name.lower().endswith(".json") or text.lstrip().startswith("{")
    if looks_json:
        try:
            return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant), []
        except _DuplicateKey as e:
            return None, [Finding("error", "Parse", f"Duplicate key {e.args[0]!r} in a JSON object.",
                                  hint="Each property may appear once per object.")]
        except json.JSONDecodeError as e:
            if file_name.lower().endswith(".json"):
                return None, [Finding("error", "Parse", f"Invalid JSON: {e.msg}.", line=e.lineno)]
            # "{...}" flow-style YAML is not JSON; fall through to the YAML parser
        except ValueError as e:
            return None, [Finding("error", "Parse", f"Invalid JSON: {e}.")]
    loader = None
    try:
        loader = _Loader(text)
        return loader.get_single_data(), []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        hint = ""
        if "expected a single document" in str(e):
            problem = "the file holds more than one YAML document"
            hint = "An Ossie document holds exactly one semantic model; put each model in its own file."
        elif "duplicate key" in str(problem):
            hint = "Each key may appear once per mapping (the second value would silently win)."
        elif "found character '\\t'" in str(e):
            hint = "YAML does not allow tabs for indentation; use spaces."
        return None, [Finding("error", "Parse", f"Invalid YAML: {problem}.",
                              line=mark.line + 1 if mark else None, hint=hint)]
    except RecursionError:
        return None, [Finding("error", "Parse", "The document is too deeply nested to parse.")]
    finally:
        if loader is not None:
            loader.dispose()


def _scan_nodes(text: str):
    """Line numbers by path, and plain YAML scalars under extension data whose type may
    differ between YAML 1.1 / 1.2 / JSON readers (the spec's quoting advice)."""
    lines: dict[tuple, int] = {}
    retyped: list[tuple[tuple, str, str]] = []
    try:
        root = yaml.compose(str(text).lstrip("﻿"), Loader=yaml.SafeLoader)
    except Exception:  # noqa: BLE001 - the parse error is reported elsewhere
        return lines, retyped
    if root is None:
        return lines, retyped
    visited = set()

    def in_ext_data(path):
        return any(path[i] in ("nonportable_extensions", "custom_extensions") and len(path) > i + 2
                   and path[i + 2] == "data" for i in range(len(path)))

    def walk(node, path):
        lines.setdefault(path, node.start_mark.line + 1)
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                key = k.value if isinstance(k, yaml.ScalarNode) else str(k)
                lines.setdefault(path + (key,), k.start_mark.line + 1)
                walk(v, path + (key,))
        elif isinstance(node, yaml.SequenceNode):
            for i, c in enumerate(node.value):
                walk(c, path + (i,))
        elif isinstance(node, yaml.ScalarNode) and node.style is None and in_ext_data(path):
            raw, tag = node.value, node.tag.rsplit(":", 1)[-1]
            if tag == "bool" and raw.lower() not in ("true", "false"):
                retyped.append((path, raw, f"YAML 1.1 reads it as the boolean {raw.lower() in ('yes', 'y', 'on')}"
                                           " but YAML 1.2 and JSON readers keep the string"))
            elif tag == "int" and (re.match(r"^[-+]?0[0-9_]", raw) or "_" in raw or ":" in raw
                                   or raw.lower().startswith(("0x", "0o", "0b"))):
                retyped.append((path, raw, "readers disagree on this number (octal, hex, base-60 or "
                                           "underscore forms)"))
            elif tag == "float" and ("_" in raw or ":" in raw):
                retyped.append((path, raw, "readers disagree on this number form"))
            elif tag == "str" and re.match(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)[eE][-+]?[0-9]+$", raw):
                retyped.append((path, raw, "this loader keeps it as a string but YAML 1.2 and JSON "
                                           "readers produce a number"))

    try:
        walk(root, ())
    except RecursionError:
        pass
    return lines, retyped


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fmt(path) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + str(p))
    return out


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _dicts(v) -> list:
    return [x for x in _as_list(v) if isinstance(x, dict)]


def _dups(items) -> list:
    return [x for x, n in Counter(items).items() if n > 1]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@lru_cache(maxsize=None)
def load_schema(file: str) -> dict:
    with open(os.path.join(SCHEMA_DIR, file), encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def _validator(file: str):
    return Draft202012Validator(load_schema(file))


def profile_for(version) -> Profile:
    return PROFILES.get(version) if isinstance(version, str) and version in PROFILES \
        else PROFILES[CURRENT_VERSION]


# --------------------------------------------------------------------------- #
# Schema findings (friendlier wording + migration hints)
# --------------------------------------------------------------------------- #
_REQUIRED_HINTS = {
    "version": f"Add `version: {CURRENT_VERSION}` at the top of the document.",
    "name": "Give the model (or element) a unique, non-empty name.",
    "datasets": "A semantic model needs at least one dataset (name + source).",
    "source": "Point the dataset at a table or view (catalog.schema.table) or a query.",
    "expression": "Add `expression: {sql: <column or SQL expression>}`.",
    "from_columns": "List the foreign-key fields of the `from` dataset.",
    "to_columns": "List the key fields of the `to` dataset, in the same order.",
    "vendor_name": "Name the vendor that owns this extension, e.g. DBT or DATABRICKS.",
    "data": "Extensions need a data payload (an object in 0.3.0.dev0).",
    "dialect": "Name the expression language, e.g. ANSI_SQL, DATABRICKS or OSSIE_SQL_2026.",
    "semantic_model": "OSI 0.1.1 documents keep their models in a semantic_model list; for the "
                      f"flat shape declare version {CURRENT_VERSION}.",
}


def _extra_property_hint(prop: str, path: tuple, allowed: list, profile: Profile) -> str:
    at_root = len(path) == 0
    if prop == "custom_extensions" and profile.ext_key == "nonportable_extensions":
        return ("custom_extensions was renamed to nonportable_extensions in 0.3.0.dev0 "
                "(apache/ossie#522) and its data must now be an object. Use Upgrade to migrate.")
    if prop == "nonportable_extensions" and profile.ext_key == "custom_extensions":
        return (f"nonportable_extensions is 0.3.0.dev0 syntax but the document declares "
                f"{profile.version}: set `version: {CURRENT_VERSION}` (data must then be an object).")
    if prop == "semantic_model" and not profile.wrapped:
        return ("The semantic_model wrapper was removed in 0.3.0.dev0 (apache/ossie#383): move the "
                "model's properties to the document root. Use Upgrade to migrate.")
    if prop in ("dialects", "vendors") and at_root and not profile.wrapped:
        return ("Root-level dialects/vendors declarations were removed (apache/ossie#397); dialects are "
                "named on each expression.")
    if profile.wrapped and at_root and prop in ("name", "datasets", "relationships", "metrics",
                                                "description", "ai_context"):
        return (f"OSI 0.1.1 keeps the model inside a semantic_model list. For a flat document declare "
                f"`version: {CURRENT_VERSION}`.")
    if profile.version == "0.1.1" and prop == "sql":
        return "expression.sql arrived after 0.1.1; in 0.1.1 use dialects: [{dialect: ANSI_SQL, expression: …}]."
    if profile.version == "0.1.1" and prop in ("metrics", "datatype", "primary_key") and not at_root:
        return f"`{prop}` here is not part of OSI 0.1.1; declare version {CURRENT_VERSION} to use it."
    close = difflib.get_close_matches(prop, allowed, n=1, cutoff=0.75)
    if close:
        return f"Did you mean `{close[0]}`?"
    return f"Allowed here: {', '.join(allowed)}." if allowed else ""


def _schema_findings(err, profile: Profile) -> list[Finding]:
    out = _schema_findings_raw(err, profile)
    for f in out:                      # problems with `version` belong to the Version check
        if f.path == "version" or (f.path == "" and "`version`" in f.message):
            f.check = "Version"
    return out


def _schema_findings_raw(err, profile: Profile) -> list[Finding]:
    path = tuple(err.absolute_path)
    where = _fmt(path) or "(root)"
    v, inst = err.validator, err.instance
    if v in ("anyOf", "oneOf") and err.context:
        sub = best_match(err.context)
        if path and path[-1] == "expression" and isinstance(inst, dict) \
                and not {"sql", "dialects"} & set(inst):
            return [Finding("error", "Schema", "The expression has neither `sql` nor `dialects`.",
                            _fmt(path), "Add `sql: <portable SQL>` and/or a non-empty `dialects` list.")]
        if path and path[-1] == "ai_context" and not isinstance(inst, (str, dict)):
            return [Finding("error", "Schema", "ai_context must be a string or an object.",
                            _fmt(path), "Use a string, or an object with instructions, synonyms and examples.")]
        if sub is not None and sub.validator not in ("anyOf", "oneOf"):
            return _schema_findings_raw(sub, profile)
    if v == "required":
        m = re.search(r"'(.+?)' is a required property", err.message)
        name = m.group(1) if m else "?"
        return [Finding("error", "Schema", f"Missing required property `{name}`.", _fmt(path),
                        _REQUIRED_HINTS.get(name, ""))]
    if v == "additionalProperties" and isinstance(inst, dict):
        allowed = list((err.schema.get("properties") or {}).keys())
        extras = [k for k in inst if k not in allowed]
        return [Finding("error", "Schema", f"Unknown property `{k}`.", _fmt(path + (k,)),
                        _extra_property_hint(str(k), path, allowed, profile)) for k in extras] or \
            [Finding("error", "Schema", err.message, _fmt(path))]
    if v == "const" and path == ("version",):
        hint = (f"Supported versions: {', '.join(SUPPORTED_VERSIONS)} (current: {CURRENT_VERSION}). "
                "Quote the value if YAML read it as a number.")
        return [Finding("error", "Version", f"version {inst!r} is not a version of the specification "
                                            f"(the {CURRENT_VERSION} schema requires {err.validator_value!r}).",
                        "version", hint)]
    if v == "enum":
        options = list(err.validator_value)
        hint = ""
        if isinstance(inst, str):
            ci = [o for o in options if o.lower() == inst.lower()]
            close = ci or difflib.get_close_matches(inst, options, n=1, cutoff=0.6)
            if close:
                hint = f"Did you mean `{close[0]}`? Values are case-sensitive."
        if path and path[-1] == "datatype":
            return [Finding("error", "Schema", f"{inst!r} is not an Ossie DataType.", _fmt(path),
                            hint or f"Use one of: {', '.join(options)}.")]
        if path and path[-1] == "dialect":
            return [Finding("error", "Schema", f"Dialect {inst!r} is not allowed by the {profile.version} "
                                               f"schema.", _fmt(path),
                            hint or f"{profile.version} accepts: {', '.join(options)}. Since 0.2.0.dev0 any "
                                    f"dialect name is accepted.")]
        if path and path[-1] == "vendor_name":
            return [Finding("error", "Schema", f"Vendor {inst!r} is not allowed by the {profile.version} "
                                               f"schema.", _fmt(path),
                            hint or f"{profile.version} accepts: {', '.join(options)}.")]
        return [Finding("error", "Schema", f"{inst!r} is not one of {options}.", _fmt(path), hint)]
    if v == "type":
        want = err.validator_value
        hint = ""
        if want == "string" and isinstance(inst, (int, float, bool, date)) and not isinstance(inst, str):
            hint = "YAML read this as a non-string value: put it in quotes."
        if want == "object" and path and path[-1] == "data" and isinstance(inst, str):
            hint = ("In 0.3.0.dev0 extension data is an object, not a JSON string (apache/ossie#522). "
                    "Use Upgrade to convert it.")
        if want == "string" and path and path[-1] == "data" and isinstance(inst, dict):
            hint = (f"Object-valued data is 0.3.0.dev0 syntax; this document declares {profile.version}. "
                    f"Set `version: {CURRENT_VERSION}` and rename custom_extensions to nonportable_extensions.")
        if want == "array" and isinstance(inst, (str, dict)):
            hint = "Use a YAML list (one `- item` per line or `[a, b]`)."
        return [Finding("error", "Schema", f"Expected {want} but found {type(inst).__name__} "
                                           f"{_short(inst)}.", _fmt(path), hint)]
    if v == "minItems":
        if path and path[-1] == "datasets":
            return [Finding("error", "Schema", "datasets is empty: a model needs at least one dataset.",
                            _fmt(path), _REQUIRED_HINTS["datasets"])]
        return [Finding("error", "Schema", f"{where} must not be an empty list.", _fmt(path))]
    if v == "minLength":
        return [Finding("error", "Schema", f"{where} must not be an empty string.", _fmt(path))]
    return [Finding("error", "Schema", err.message, _fmt(path))]


def _short(v, n: int = 60) -> str:
    s = repr(v)
    return s if len(s) <= n else s[:n - 1] + "…"


# --------------------------------------------------------------------------- #
# Extension data
# --------------------------------------------------------------------------- #
_EXT_CONTAINERS = ("datasets", "fields", "metrics", "relationships")


def _iter_ext(node, path, key):
    """Yield (path, extension dict) for every extension entry in a model."""
    if not isinstance(node, dict):
        return
    for i, e in enumerate(_as_list(node.get(key))):
        if isinstance(e, dict):
            yield path + (key, i), e
    for c in _EXT_CONTAINERS:
        for i, child in enumerate(_as_list(node.get(c))):
            yield from _iter_ext(child, path + (c, i), key)


def _non_json(value, path, active, walked, out):
    if isinstance(value, (bool, str)) or value is None or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            out.append((path, "a non-finite number (NaN or infinity) is not a JSON value"))
        return
    if isinstance(value, datetime):
        out.append((path, "a timestamp is not a JSON value"))
        return
    if isinstance(value, date):
        out.append((path, "a date is not a JSON value"))
        return
    if isinstance(value, time):
        out.append((path, "a time is not a JSON value"))
        return
    if isinstance(value, (list, dict)):
        if id(value) in active:
            out.append((path, "a cyclic structure (YAML anchor) is not a JSON value"))
            return
        if id(value) in walked:
            return
        walked.add(id(value))
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for k, item in items:
            if isinstance(value, dict) and not isinstance(k, str):
                out.append((path + (str(k),), f"key {k!r} is not a string"))
            _non_json(item, path + (k,), active | {id(value)}, walked, out)
        return
    out.append((path, f"a {type(value).__name__} is not a JSON value"))


def _check_extensions(rep: Report, add, retyped) -> None:
    prof = rep.profile
    for pre, model in rep.models:
        for path, ext in _iter_ext(model, pre, prof.ext_key):
            vendor = ext.get("vendor_name")
            if isinstance(vendor, str) and vendor and vendor not in KNOWN_VENDORS:
                ci = [v for v in KNOWN_VENDORS if v.lower() == vendor.lower().replace(" ", "_")]
                close = ci or difflib.get_close_matches(vendor, KNOWN_VENDORS, n=1, cutoff=0.8)
                if close:
                    add("info", "Extensions", f"Vendor name {vendor!r} is not in the community registry.",
                        path + ("vendor_name",), f"Did you mean `{close[0]}`? (vendor_name is free-form)")
            data = ext.get("data")
            if prof.ext_data == "object":
                if isinstance(data, dict):
                    bad = []
                    _non_json(data, path + ("data",), frozenset(), set(), bad)
                    for p, why in bad:
                        add("error", "Extensions", f"Extension data must be JSON: {why}.", p,
                            "Quote the value so YAML keeps it as a string.")
            elif isinstance(data, str):
                try:
                    parsed = json.loads(data)
                except ValueError:
                    add("warning", "Extensions", "Extension data is not a valid JSON string.",
                        path + ("data",), f"{prof.version} describes data as JSON text; the "
                                          f"{CURRENT_VERSION} upgrade will keep it as {{value: <text>}}.")
                    continue
                if not isinstance(parsed, dict):
                    add("info", "Extensions", "Extension data is JSON but not an object.",
                        path + ("data",), f"{CURRENT_VERSION} needs an object; the upgrade wraps it as "
                                          "{value: …}.")
    for path, raw, why in retyped:
        add("warning", "Extensions", f"Unquoted value {raw!r} under extension data: {why}.", path,
            f"Write it as '{raw}' (quoted) if you mean the text, or as an explicit number/boolean.")


# --------------------------------------------------------------------------- #
# SQL helpers (sqlglot)
# --------------------------------------------------------------------------- #
DATE_PART_FUNCTIONS = {"DATEADD", "DATE_PART", "DATEPART", "PGDATE_PART", "TIMESTAMPADD"}
_FALLBACK_DATE_PARTS = {"YEAR", "YEARS", "YY", "YYYY", "QUARTER", "QUARTERS", "QQ", "Q", "MONTH",
                        "MONTHS", "MM", "MON", "WEEK", "WEEKS", "WK", "WW", "W", "DAY", "DAYS", "DD", "D",
                        "DAYOFWEEK", "DOW", "DAYOFYEAR", "DOY", "HOUR", "HOURS", "HH", "MINUTE", "MINUTES",
                        "MI", "SECOND", "SECONDS", "SS", "S", "MILLISECOND", "MS", "MICROSECOND", "US",
                        "NANOSECOND", "NS", "EPOCH"}


@lru_cache(maxsize=1)
def _date_parts() -> frozenset:
    try:
        from sqlglot.dialects.dialect import Dialect
        m = Dialect.DATE_PART_MAPPING
        return frozenset(set(m) | set(m.values()) | _FALLBACK_DATE_PARTS)
    except Exception:  # noqa: BLE001
        return frozenset(_FALLBACK_DATE_PARTS)


def _is_date_part_name(name: str) -> bool:
    return name.upper() in _date_parts()


def _unit_slot_types():
    return tuple(t for t in (getattr(exp, "DateAdd", None), getattr(exp, "DateDiff", None),
                             getattr(exp, "TimestampDiff", None)) if t is not None) if SQLGLOT else ()


def _uses_default_parser(dialect: str) -> bool:
    return SQLGLOT_DIALECTS.get(dialect) is None


def _is_date_part(column, dialect: str) -> bool:
    parent = column.parent
    if column.table or not _is_date_part_name(column.name):
        return False
    slots = _unit_slot_types()
    if slots and isinstance(parent, slots) and _uses_default_parser(dialect):
        unit = parent.args.get("unit")
        consumed = isinstance(unit, exp.Var) and _is_date_part_name(unit.name)
        return column.arg_key == "this" and unit is not None and not consumed
    if isinstance(parent, exp.Anonymous) and parent.name.upper() in DATE_PART_FUNCTIONS:
        return bool(parent.expressions) and parent.expressions[0] is column
    return False


def _iter_columns(tree, dialect: str):
    """Column references of a parsed expression: no `*`, no date-function units; the default
    parser's DATEDIFF(day, a, b) quirk reads b back as a column."""
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or _is_date_part(column, dialect):
            continue
        yield column
    if not _uses_default_parser(dialect):
        return
    slots = _unit_slot_types()
    if not slots:
        return
    for node in tree.find_all(*slots):
        first, unit = node.this, node.args.get("unit")
        if (isinstance(first, exp.Column) and not first.table and _is_date_part_name(first.name)
                and isinstance(unit, exp.Var) and not _is_date_part_name(unit.name)
                and unit.name.isidentifier()):
            yield exp.column(unit.name)


def _col_parts(column) -> list[str]:
    try:
        return [p.name for p in column.parts]
    except Exception:  # noqa: BLE001
        return [x for x in (column.catalog, column.db, column.table, column.name) if x]


@lru_cache(maxsize=4096)
def parse_sql(text: str, dialect: str):
    """(tree, error) for one expression; (None, None) when sqlglot cannot read the dialect."""
    if not SQLGLOT or dialect not in SQLGLOT_DIALECTS:
        return None, None
    read = SQLGLOT_DIALECTS[dialect]

    def count_statements(sql):
        try:
            return len([s for s in sqlglot.parse(sql, read=read) if s is not None])
        except (ParseError, TokenError, RecursionError):
            return None
        except Exception:  # noqa: BLE001
            return None

    try:
        tree = sqlglot.parse_one(text, read=read)
    except (ParseError, TokenError, RecursionError):
        n = count_statements(text)
        if n == 0:
            return None, "the expression is empty"
    except Exception as e:  # noqa: BLE001
        return None, f"cannot parse ({type(e).__name__}: {str(e)[:120]})"
    else:
        n = count_statements(text)
        if n and n > 1:
            return None, f"expected a single expression but found {n} statements"
        return tree, None
    try:
        tree = sqlglot.parse_one(f"SELECT {text}", read=read)
    except (ParseError, TokenError) as e:
        return None, str(e).split("\n")[0]
    except RecursionError:
        return None, "the expression is too deeply nested to parse"
    except Exception as e:  # noqa: BLE001
        return None, f"cannot parse ({type(e).__name__})"
    n = count_statements(f"SELECT {text}")
    if n and n > 1:
        return None, f"expected a single expression but found {n} statements"
    return tree, None


@dataclass(frozen=True)
class Expr:
    kind: str            # field | dataset_metric | model_metric
    dataset: str
    name: str
    dialect: str
    text: str
    fields: frozenset    # case-folded declared field names of the enclosing dataset
    path: tuple
    superseded_by: str | None = None

    def label(self, model_name: str) -> str:
        who = {"field": "Field", "dataset_metric": "Metric", "model_metric": "Metric"}[self.kind]
        name = f"{self.dataset}.{self.name}" if self.dataset else self.name
        return f"{who} '{name}' ({self.dialect})"


def _field_names(ds: dict) -> frozenset:
    return frozenset(f["name"].casefold() for f in _dicts(ds.get("fields"))
                     if isinstance(f.get("name"), str) and f["name"])


def _exprs_of(member: dict, kind: str, dataset: str, fields: frozenset, path: tuple):
    e = member.get("expression")
    if not isinstance(e, dict):
        return
    name = member.get("name", "<unnamed>")
    sql = e.get("sql")
    if isinstance(sql, str):
        yield Expr(kind, dataset, name, PORTABLE_DIALECT, sql, fields, path + ("expression", "sql"))
    for i, d in enumerate(_as_list(e.get("dialects"))):
        if not isinstance(d, dict) or not isinstance(d.get("dialect"), str) \
                or not isinstance(d.get("expression"), str):
            continue
        yield Expr(kind, dataset, name, d["dialect"], d["expression"], fields,
                   path + ("expression", "dialects", i),
                   superseded_by=sql if d["dialect"] == PORTABLE_DIALECT and isinstance(sql, str) else None)


def iter_expressions(model: dict, pre: tuple = ()):
    for i, ds in enumerate(_dicts(model.get("datasets"))):
        dname = ds.get("name", "<unnamed>")
        names = _field_names(ds)
        for j, f in enumerate(_as_list(ds.get("fields"))):
            if isinstance(f, dict):
                yield from _exprs_of(f, "field", dname, names, pre + ("datasets", i, "fields", j))
        for j, m in enumerate(_as_list(ds.get("metrics"))):
            if isinstance(m, dict):
                yield from _exprs_of(m, "dataset_metric", dname, names, pre + ("datasets", i, "metrics", j))
    for i, m in enumerate(_as_list(model.get("metrics"))):
        if isinstance(m, dict):
            yield from _exprs_of(m, "model_metric", "", frozenset(), pre + ("metrics", i))


def _readable(e: Expr) -> bool:
    return bool(e.text) and e.superseded_by is None


# --------------------------------------------------------------------------- #
# Semantic checks (one model)
# --------------------------------------------------------------------------- #
def _check_unique(model, pre, add):
    mname = model.get("name", "<unnamed>")

    def report(items, what, base_path, scope):
        names = [(i, x.get("name")) for i, x in enumerate(items) if isinstance(x.get("name"), str) and x.get("name")]
        for dup in _dups([n for _, n in names]):
            idx = [i for i, n in names if n == dup]
            add("error", "Unique", f"{what} name '{dup}' is used {len(idx)} times in {scope}.",
                base_path + (idx[1], "name"), f"Rename one of them; {what.lower()} names must be unique.")
        folded = {}
        for i, n in names:
            folded.setdefault(n.casefold(), []).append((i, n))
        for group in folded.values():
            spellings = sorted({n for _, n in group})
            if len(spellings) > 1:
                add("warning", "Lint", f"{what} names {', '.join(repr(s) for s in spellings)} differ only by "
                                       f"case in {scope}.", base_path + (group[1][0], "name"),
                    "References are matched case-insensitively, so these collide. Rename one.")

    datasets = _dicts(model.get("datasets"))
    report(datasets, "Dataset", pre + ("datasets",), f"model '{mname}'")
    for i, ds in enumerate(_as_list(model.get("datasets"))):
        if not isinstance(ds, dict):
            continue
        dname = ds.get("name", "<unnamed>")
        fields = _dicts(ds.get("fields"))
        metrics = _dicts(ds.get("metrics"))
        report(fields, "Field", pre + ("datasets", i, "fields"), f"dataset '{dname}'")
        report(metrics, "Metric", pre + ("datasets", i, "metrics"), f"dataset '{dname}'")
        fnames = {f.get("name") for f in fields if f.get("name")}
        for j, m in enumerate(metrics):
            if m.get("name") in fnames:
                add("error", "Unique", f"'{m['name']}' in dataset '{dname}' is both a field and a "
                                       "dataset-scoped metric.", pre + ("datasets", i, "metrics", j, "name"),
                    "A dataset's fields and metrics share one namespace: rename the metric or the field.")
    report(_dicts(model.get("metrics")), "Metric", pre + ("metrics",), f"model '{mname}'")
    report(_dicts(model.get("relationships")), "Relationship", pre + ("relationships",), f"model '{mname}'")


def _keys_of(ds: dict) -> list[set]:
    declared = _field_names(ds)
    keys = []
    for key in [ds.get("primary_key")] + _as_list(ds.get("unique_keys")):
        if not isinstance(key, list) or not key:
            continue
        folded = [n.casefold() for n in key if isinstance(n, str) and n]
        if len(folded) == len(key) and all(n in declared for n in folded):
            keys.append(set(folded))
    return keys


def _check_references(model, pre, add, legacy: bool):
    mname = model.get("name", "<unnamed>")
    datasets = {d.get("name"): d for d in _dicts(model.get("datasets")) if d.get("name")}
    sev = "warning" if legacy else "error"
    legacy_hint = (" Earlier versions did not say whether these names are fields or source columns; "
                   f"{CURRENT_VERSION} requires declared fields.") if legacy else ""

    def undeclared(names, declared):
        out, seen = [], set()
        for n in _as_list(names):
            if isinstance(n, str) and n.casefold() not in declared and n.casefold() not in seen:
                seen.add(n.casefold())
                out.append(n)
        return out

    for i, ds in enumerate(_as_list(model.get("datasets"))):
        if not isinstance(ds, dict):
            continue
        dname = ds.get("name", "<unnamed>")
        declared = _field_names(ds)
        miss = undeclared(ds.get("primary_key"), declared)
        if miss:
            add(sev, "Reference", f"primary_key of dataset '{dname}' names undeclared field(s) "
                                  f"{', '.join(repr(m) for m in miss)}.", pre + ("datasets", i, "primary_key"),
                "Declare them under fields (a key names fields, not source columns)." + legacy_hint)
        used = [n for k in _as_list(ds.get("unique_keys")) if isinstance(k, list) for n in k]
        miss = undeclared(used, declared)
        if miss:
            add(sev, "Reference", f"unique_keys of dataset '{dname}' name undeclared field(s) "
                                  f"{', '.join(repr(m) for m in miss)}.", pre + ("datasets", i, "unique_keys"),
                "Declare them under fields." + legacy_hint)
        if not _dicts(ds.get("fields")):
            add("info", "Lint", f"Dataset '{dname}' declares no fields, so keys, relationships and "
                                "model-scoped metrics cannot use it.", pre + ("datasets", i),
                "Declare the columns people group, filter or join by as fields.")

    for i, r in enumerate(_as_list(model.get("relationships"))):
        if not isinstance(r, dict):
            continue
        rname = r.get("name", "<unnamed>")
        p = pre + ("relationships", i)
        for side in ("from", "to"):
            target = r.get(side)
            if isinstance(target, str) and target and target not in datasets:
                ci = [d for d in datasets if isinstance(d, str) and d.casefold() == target.casefold()]
                close = ci or difflib.get_close_matches(target, [d for d in datasets if isinstance(d, str)], n=1)
                add("error", "Reference", f"Relationship '{rname}' references unknown dataset '{target}' "
                                          f"in `{side}`.", p + (side,),
                    f"Did you mean '{close[0]}'?" if close else "Name a dataset declared in this model.")
        for side, cols_key in (("from", "from_columns"), ("to", "to_columns")):
            ds = datasets.get(r.get(side))
            if ds is None:
                continue
            miss = undeclared(r.get(cols_key), _field_names(ds))
            if miss:
                add(sev, "Reference", f"Relationship '{rname}': {cols_key} names undeclared field(s) "
                                      f"{', '.join(repr(m) for m in miss)} of dataset '{r.get(side)}'.",
                    p + (cols_key,), "Declare them as fields of that dataset." + legacy_hint)
        to_ds, to_cols = datasets.get(r.get("to")), r.get("to_columns")
        if to_ds is not None and isinstance(to_cols, list) and to_cols \
                and not undeclared(to_cols, _field_names(to_ds)):
            keys = _keys_of(to_ds)
            cols = {c.casefold() for c in to_cols if isinstance(c, str)}
            if keys and not any(k <= cols for k in keys):
                add("warning", "Reference", f"Relationship '{rname}': to_columns {to_cols} do not cover the "
                                            f"primary key or a unique key of '{r.get('to')}', so the join may "
                                            "fan out.", p + ("to_columns",),
                    "Point to_columns at the key of the one-side dataset.")
            elif not keys:
                add("info", "Lint", f"Relationship '{rname}': dataset '{r.get('to')}' declares no primary or "
                                    "unique key, so nothing confirms the to side is unique.",
                    p + ("to_columns",), f"Add primary_key to '{r.get('to')}'.")


def _check_arity(model, pre, add):
    for i, r in enumerate(_as_list(model.get("relationships"))):
        if not isinstance(r, dict):
            continue
        fc, tc = r.get("from_columns"), r.get("to_columns")
        if isinstance(fc, list) and isinstance(tc, list) and len(fc) != len(tc):
            add("error", "Arity", f"Relationship '{r.get('name', '<unnamed>')}' pairs {len(fc)} from_columns "
                                  f"with {len(tc)} to_columns.", pre + ("relationships", i),
                "The two lists pair up by position, so they must be the same length.")


def _check_metric_references(model, pre, add):
    if not SQLGLOT:
        return
    fields: dict[str, set] = {}
    for d in _dicts(model.get("datasets")):
        if isinstance(d.get("name"), str):
            fields.setdefault(d["name"].casefold(), set()).update(_field_names(d))
    all_fields = set().union(*fields.values()) if fields else set()
    metrics = _dicts(model.get("metrics"))
    declared = {m["name"].casefold(): m["name"] for m in metrics if isinstance(m.get("name"), str)}
    for e in iter_expressions(model, pre):
        if e.kind != "model_metric" or not _readable(e):
            continue
        tree, _ = parse_sql(e.text, e.dialect)
        if tree is None:
            continue
        own = e.name.casefold() if isinstance(e.name, str) else None
        others = {k: v for k, v in declared.items() if k not in all_fields and k != own}
        seen = set()
        for col in _iter_columns(tree, e.dialect):
            parts = _col_parts(col)
            key = tuple(x.casefold() for x in parts)
            if key in seen:
                continue
            seen.add(key)
            name, table = col.name.casefold(), (col.table or "").casefold()
            if col.db:
                msg = f"'{'.'.join(parts)}' has more than two parts; refer to fields as dataset.field"
            elif not col.table and name in others:
                msg = f"'{col.name}' names the metric '{others[name]}'; a metric cannot refer to another metric"
            elif not col.table:
                msg = f"'{col.name}' is not qualified; model-scoped metrics refer to fields as dataset.field"
            elif table not in fields:
                msg = f"'{col.table}.{col.name}' references unknown dataset '{col.table}'"
            elif name not in fields[table]:
                msg = f"'{col.table}.{col.name}' is not a declared field of dataset '{col.table}'"
            else:
                continue
            add("warning", "Reference", f"{e.label(model.get('name'))}: {msg}.", e.path,
                "Model-scoped metrics reference declared fields as dataset.field (Name Resolution).")


def _check_field_shadowing(model, pre, add):
    if not SQLGLOT:
        return

    def passes_through(tree, name):
        while isinstance(tree, (exp.Paren, exp.Alias)):
            tree = tree.this
        return isinstance(tree, exp.Column) and not tree.table and tree.name.casefold() == name.casefold()

    for i, ds in enumerate(_as_list(model.get("datasets"))):
        if not isinstance(ds, dict):
            continue
        dname = ds.get("name", "<unnamed>")
        declared = [f for f in _dicts(ds.get("fields")) if isinstance(f.get("name"), str)]
        by_name = {}
        for f in declared:
            by_name.setdefault(f["name"].casefold(), f)
        for j, f in enumerate(_as_list(ds.get("fields"))):
            if not isinstance(f, dict) or not isinstance(f.get("name"), str):
                continue
            for e in _exprs_of(f, "field", dname, frozenset(), pre + ("datasets", i, "fields", j)):
                if not _readable(e):
                    continue
                tree, _ = parse_sql(e.text, e.dialect)
                if tree is None:
                    continue
                names = [c.name for c in _iter_columns(tree, e.dialect) if not c.table]
                for name in dict.fromkeys(names):
                    other = by_name.get(name.casefold())
                    if other is None or other["name"].casefold() == f["name"].casefold():
                        continue
                    defs = [x for x in _exprs_of(other, "field", dname, frozenset(), ()) if _readable(x)]
                    same = [x for x in defs if x.dialect == e.dialect] or defs
                    parsed = [(x.text, t) for x in same if (t := parse_sql(x.text, x.dialect)[0]) is not None]
                    if not parsed or any(passes_through(t, name) for _, t in parsed):
                        continue
                    add("warning", "Reference", f"{e.label(model.get('name'))}: '{name}' is the source column, "
                                                f"not the field '{other['name']}' (defined as "
                                                f"'{parsed[0][0]}').", e.path,
                        "Field expressions see source columns only; repeat the other field's expression "
                        "if you meant the field.")


def _check_scope(model, pre, add):
    if not SQLGLOT:
        return
    for e in iter_expressions(model, pre):
        if e.kind != "dataset_metric" or not _readable(e):
            continue
        tree, _ = parse_sql(e.text, e.dialect)
        if tree is None:
            continue
        qualifiers, undeclared, uses_source = set(), set(), False
        stars = [c for c in tree.find_all(exp.Column) if isinstance(c.this, exp.Star) and c.table]
        for col in (*_iter_columns(tree, e.dialect), *stars):
            parts = _col_parts(col)
            if len(parts) == 2 and parts[0].casefold() == SOURCE_QUALIFIER:
                uses_source = True
            elif len(parts) > 1 and parts[0]:
                qualifiers.add(parts[0])
            elif parts and parts[0].casefold() not in e.fields:
                undeclared.add(parts[0])
        label = f"Dataset-scoped metric '{e.dataset}.{e.name}' ({e.dialect})"
        if qualifiers:
            add("error", "Scope", f"{label} uses qualified reference(s) {', '.join(repr(q) for q in sorted(qualifiers))}.",
                e.path, "Use this dataset's fields unqualified and its source columns as source.column; a metric "
                        "that needs another dataset belongs in the model-level metrics.")
        if undeclared:
            add("error", "Scope", f"{label} references undeclared field(s) "
                                  f"{', '.join(repr(n) for n in sorted(undeclared))}.", e.path,
                "Declare them as fields of the dataset, or write source.<column> for a source column.")
        if uses_source and SOURCE_QUALIFIER in e.fields:
            add("error", "Scope", f"{label} uses source.column, but the dataset declares a field named "
                                  "'source', which takes precedence.", e.path,
                "Rename the field 'source' to reach source columns from this dataset's metrics.")


def _check_sql(model, pre, add, stats):
    if not SQLGLOT:
        add("warning", "SQL", "sqlglot is not installed, so SQL expressions were not syntax-checked.", "",
            "pip install sqlglot")
        return
    unsupported = Counter()
    first_path = {}
    parsed = skipped = 0
    for e in iter_expressions(model, pre):
        if e.dialect in NON_SQL_DIALECTS:
            skipped += 1
            continue
        if e.dialect not in SQLGLOT_DIALECTS:
            unsupported[e.dialect] += 1
            first_path.setdefault(e.dialect, e.path)
            continue
        tree, error = parse_sql(e.text, e.dialect) if e.text else (None, "the expression is empty")
        parsed += 1
        if e.superseded_by is not None:
            if error:
                why = "is not valid SQL"
            elif e.text != e.superseded_by:
                why = "differs from the top-level `sql`"
            else:
                continue
            add("warning", "SQL", f"{e.label(model.get('name'))}: this OSSIE_SQL_2026 dialect entry {why} and is "
                                  "ignored, because the top-level `sql` is authoritative.", e.path,
                "Remove the duplicate entry or make it match `sql`.")
        elif error:
            add("error", "SQL", f"{e.label(model.get('name'))}: {error.rstrip('. ')}.", e.path,
                "Fix the SQL so it parses as one scalar (field) or aggregate (metric) expression.")
    for d in sorted(unsupported):
        upper = [k for k in KNOWN_DIALECTS if k.lower() == d.lower()]
        close = upper or difflib.get_close_matches(d, KNOWN_DIALECTS, n=1, cutoff=0.8)
        add("warning", "SQL", f"Dialect {d!r} is not one this validator can parse; "
                              f"{_plural(unsupported[d], 'expression')} not syntax-checked.", first_path[d],
            (f"Did you mean '{close[0]}'? Dialect names are case-sensitive." if close else
             "Dialect names are free-form, so this is allowed; consumers that do not know it will ignore it."))
    stats["expressions_parsed"] = stats.get("expressions_parsed", 0) + parsed
    stats["expressions_not_sql"] = stats.get("expressions_not_sql", 0) + skipped + sum(unsupported.values())


# --- metric drift / purity (reference: GitHub issue #279) -------------------
def _number_text(text: str) -> str:
    try:
        value = Decimal(str(text))
    except (InvalidOperation, TypeError, ValueError):
        return str(text)
    if value.is_nan():
        return "nan"
    if value.is_infinite():
        return "-infinity" if value < 0 else "infinity"
    try:
        if value % 1 == 0:
            return str(int(value))
    except (InvalidOperation, OverflowError):
        pass
    return str(value.normalize())


def _arith_constant(node, negative=False):
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Neg):
        return _arith_constant(node.this, not negative)
    if not isinstance(node, exp.Literal) or not node.is_number:
        return None
    text = _number_text(node.name)
    if not negative:
        return text
    return text[1:] if text.startswith("-") else f"-{text}"


def _is_literal(node) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    return _arith_constant(node) is not None or isinstance(node, (exp.Boolean, exp.Null, exp.Literal))


def _operand(node) -> str:
    if node is None:
        return ""
    while isinstance(node, exp.Paren):
        node = node.this
    c = _arith_constant(node)
    if c is not None:
        return c
    if isinstance(node, exp.Column):
        return node.name.lower()
    if isinstance(node, exp.Boolean):
        return "1" if node.this else "0"
    if isinstance(node, exp.Null):
        return "null"
    if isinstance(node, exp.Literal):
        return f"'{node.name}'"
    return " ".join(node.sql().lower().split())


_FLIP = {"GT": "LT", "LT": "GT", "GTE": "LTE", "LTE": "GTE", "EQ": "EQ", "NEQ": "NEQ"}
_IMPURE = {"now": "NOW", "current_timestamp": "NOW", "random": "RANDOM", "rand": "RANDOM",
           "pg_read_file": "pg_read_file"}


def _predicate(node) -> str:
    kind = type(node).__name__.upper()
    left = _operand(node.this)
    if isinstance(node, exp.In):
        items = sorted(_operand(x) for x in (node.expressions or []))
        token = f"IN({left},[{','.join(items)}])"
    elif isinstance(node, exp.Is):
        token = f"IS({left},{_operand(node.args.get('expression'))})"
    elif isinstance(node, exp.Between):
        token = f"BETWEEN({left},{_operand(node.args.get('low'))},{_operand(node.args.get('high'))})"
    elif isinstance(node, exp.Like):
        token = f"LIKE({left},{_operand(node.args.get('expression'))})"
    else:
        right = _operand(node.args.get("expression"))
        if kind in _FLIP and left and right:
            ll, rl = _is_literal(node.this), _is_literal(node.args.get("expression"))
            if (ll != rl and ll) or (ll == rl and left > right):
                kind, left, right = _FLIP[kind], right, left
        token = f"{kind}({left},{right})"
    neg = bool(node.args.get("negate") or node.args.get("is_not"))
    p = node.parent
    while isinstance(p, (exp.Paren, exp.Not)):
        if isinstance(p, exp.Not):
            neg = not neg
        p = p.parent
    return f"NOT({token})" if neg else token


def _signature(tree) -> tuple:
    aggs = []
    for a in tree.find_all(exp.AggFunc):
        name = type(a).__name__.upper()
        if isinstance(a.this, exp.Distinct):
            name += "|DISTINCT"
        aggs.append(name)
    cols = frozenset(c.name.lower() for c in tree.find_all(exp.Column) if c.name)
    consts = set()
    for n in tree.find_all(exp.Mul, exp.Div, exp.Add, exp.Sub):
        for side in (n.this, n.args.get("expression")):
            c = _arith_constant(side)
            if c is not None:
                consts.add(c)
    preds = frozenset(_predicate(n) for n in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT,
                                                             exp.LTE, exp.In, exp.Is, exp.Between, exp.Like))
    return tuple(sorted(aggs)), cols, frozenset(consts), preds


def _impure(tree) -> list[str]:
    found = set()
    for n in tree.find_all(exp.Func):
        name = (n.name or "").lower() if isinstance(n, exp.Anonymous) else n.sql_name().lower()
        if name in _IMPURE:
            found.add(_IMPURE[name])
    return sorted(found)


def _check_drift(model, pre, add):
    if not SQLGLOT:
        return
    mname = model.get("name", "<unnamed>")
    exprs = [e for e in iter_expressions(model, pre) if e.kind in ("dataset_metric", "model_metric")]
    for (kind, dataset, name, mpath), group in itertools.groupby(
            exprs, key=lambda e: (e.kind, e.dataset, e.name, e.path[:e.path.index("expression")])):
        label = f"{dataset}.{name}" if dataset else name
        parsed = []
        for e in group:
            if not _readable(e):
                continue
            tree, err = parse_sql(e.text, e.dialect)
            if tree is None or err:
                continue
            parsed.append((e.dialect, _signature(tree)))
            for call in _impure(tree):
                add("warning", "Purity", f"{e.label(mname)} calls {call}(), which is not a reproducible read.",
                    e.path, "Metrics should give the same answer for the same data; pass dates as filters.")
        if len(parsed) < 2:
            continue
        for idx, axis in ((0, "aggregate functions"), (1, "referenced columns"), (2, "arithmetic constants"),
                          (3, "filter predicates")):
            if len({sig[idx] for _, sig in parsed}) > 1:
                detail = ", ".join(f"{d}={sorted(sig[idx]) if not isinstance(sig[idx], tuple) else list(sig[idx])}"
                                   for d, sig in parsed)
                add("warning", "Drift", f"Metric '{label}': {axis} differ across dialects ({detail}).",
                    mpath + ("expression",), "Make every dialect variant compute the same thing.")


# --- studio lints ------------------------------------------------------------
def _check_lints(model, pre, add, stats):
    # expressions: aggregates in fields, metrics without aggregates, duplicate dialect entries
    holders = [(h, "field") for h in _field_holders(model, pre)] + \
              [(h, "metric") for h in _metric_holders(model, pre)]
    for (path, member, dname), kind in holders:
        e = member.get("expression")
        if not isinstance(e, dict):
            continue
        dialects = [d.get("dialect") for d in _dicts(e.get("dialects")) if isinstance(d.get("dialect"), str)]
        for dup in _dups(dialects):
            add("warning", "Lint", f"{kind.title()} '{member.get('name')}' lists dialect {dup!r} more than once.",
                path + ("expression", "dialects"), "Keep one expression per dialect; readers pick one arbitrarily.")
        if not SQLGLOT:
            continue
        exprs = [x for x in _exprs_of(member, "field" if kind == "field" else "model_metric", dname,
                                      frozenset(), path) if _readable(x)]
        trees = [(x, parse_sql(x.text, x.dialect)[0]) for x in exprs]
        trees = [(x, t) for x, t in trees if t is not None]
        if not trees:
            continue
        if kind == "field":
            for x, t in trees:
                aggs = [a for a in t.find_all(exp.AggFunc) if not a.find_ancestor(exp.Window)]
                if aggs:
                    add("warning", "Lint", f"Field '{dname}.{member.get('name')}' uses the aggregate "
                                           f"{type(aggs[0]).__name__.upper()}() ({x.dialect}).", x.path,
                        "Fields are row-level scalar expressions; move aggregations to a metric.")
                    break
        elif all(not any(True for _ in t.find_all(exp.AggFunc)) for _, t in trees):
            x = trees[0][0]
            add("warning", "Lint", f"Metric '{(dname + '.') if dname else ''}{member.get('name')}' has no "
                                   "aggregate function.", x.path,
                "Metrics are aggregations (SUM, COUNT, AVG …); a row-level expression belongs in a field.")
    # duplicate relationships
    seen = {}
    for i, r in enumerate(_as_list(model.get("relationships"))):
        if not isinstance(r, dict):
            continue
        key = (str(r.get("from")).casefold(), str(r.get("to")).casefold(),
               tuple(str(c).casefold() for c in _as_list(r.get("from_columns"))),
               tuple(str(c).casefold() for c in _as_list(r.get("to_columns"))))
        if key in seen:
            add("warning", "Lint", f"Relationship '{r.get('name')}' repeats '{seen[key]}' (same datasets and "
                                   "columns).", pre + ("relationships", i), "Keep one of them.")
        else:
            seen[key] = r.get("name")
    # isolated datasets
    names = [d.get("name") for d in _dicts(model.get("datasets")) if isinstance(d.get("name"), str)]
    linked = {str(r.get(s)) for r in _dicts(model.get("relationships")) for s in ("from", "to")}
    if len(names) > 1:
        for i, d in enumerate(_as_list(model.get("datasets"))):
            if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"] not in linked:
                add("info", "Lint", f"Dataset '{d['name']}' is not in any relationship, so it cannot be joined "
                                    "to the rest of the model.", pre + ("datasets", i),
                    "Add a relationship if queries should combine it with other datasets.")
    # ambiguous synonyms (AI readiness)
    owners: dict[str, dict] = {}

    def syn(ai, owner, path):
        if isinstance(ai, dict):
            for s in _as_list(ai.get("synonyms")):
                if isinstance(s, str) and s.strip():
                    owners.setdefault(s.strip().casefold(), {}).setdefault(owner, path)

    described = total = with_syn = 0
    for i, d in enumerate(_as_list(model.get("datasets"))):
        if not isinstance(d, dict):
            continue
        dn, dp = d.get("name"), pre + ("datasets", i)
        syn(d.get("ai_context"), f"dataset {dn}", dp + ("ai_context", "synonyms"))
        for j, f in enumerate(_as_list(d.get("fields"))):
            if isinstance(f, dict):
                syn(f.get("ai_context"), f"field {dn}.{f.get('name')}", dp + ("fields", j, "ai_context", "synonyms"))
        for j, m in enumerate(_as_list(d.get("metrics"))):
            if isinstance(m, dict):
                syn(m.get("ai_context"), f"metric {dn}.{m.get('name')}", dp + ("metrics", j, "ai_context", "synonyms"))
        for x in [d] + _dicts(d.get("fields")) + _dicts(d.get("metrics")):
            total += 1
            described += bool(x.get("description"))
            with_syn += isinstance(x.get("ai_context"), dict) and bool(x["ai_context"].get("synonyms"))
    for j, m in enumerate(_as_list(model.get("metrics"))):
        if not isinstance(m, dict):
            continue
        syn(m.get("ai_context"), f"metric {m.get('name')}", pre + ("metrics", j, "ai_context", "synonyms"))
        total += 1
        described += bool(m.get("description"))
        with_syn += isinstance(m.get("ai_context"), dict) and bool(m["ai_context"].get("synonyms"))
    for term, who in sorted(owners.items()):
        if len(who) > 1:
            add("info", "Lint", f"Synonym '{term}' points to {len(who)} elements: {', '.join(sorted(who))}.",
                list(who.values())[1], "An AI agent cannot tell which one a user means; keep each synonym on "
                                       "one element (or say which to prefer in ai_context.instructions).")
    if total:
        stats["described"] = f"{described}/{total} datasets, fields and metrics have a description"
        stats["with_synonyms"] = f"{with_syn}/{total} have ai_context synonyms"


def _field_holders(model, pre):
    for i, d in enumerate(_as_list(model.get("datasets"))):
        if isinstance(d, dict):
            for j, f in enumerate(_as_list(d.get("fields"))):
                if isinstance(f, dict):
                    yield pre + ("datasets", i, "fields", j), f, d.get("name", "<unnamed>")


def _metric_holders(model, pre):
    for i, d in enumerate(_as_list(model.get("datasets"))):
        if isinstance(d, dict):
            for j, m in enumerate(_as_list(d.get("metrics"))):
                if isinstance(m, dict):
                    yield pre + ("datasets", i, "metrics", j), m, d.get("name", "<unnamed>")
    for i, m in enumerate(_as_list(model.get("metrics"))):
        if isinstance(m, dict):
            yield pre + ("metrics", i), m, ""


def _model_stats(models) -> dict:
    ds = [d for _, m in models for d in _dicts(m.get("datasets"))]
    dialects = Counter(e.dialect for pre, m in models for e in iter_expressions(m, pre))
    return {
        "models": len(models),
        "datasets": len(ds),
        "fields": sum(len(_dicts(d.get("fields"))) for d in ds),
        "relationships": sum(len(_dicts(m.get("relationships"))) for _, m in models),
        "model_metrics": sum(len(_dicts(m.get("metrics"))) for _, m in models),
        "dataset_metrics": sum(len(_dicts(d.get("metrics"))) for d in ds),
        "dialects": ", ".join(f"{d} ({n})" for d, n in dialects.most_common()) or "none",
    }


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def validate_text(text: str, file_name: str = "", strict: bool = False) -> Report:
    """Validate a YAML/JSON document given as text."""
    data, problems = load_text(text, file_name)
    rep = Report(findings=list(problems), file_name=file_name, strict=strict)
    if data is None:
        rep.checks = {"Parse": "failed"}
        return rep
    lines, retyped = _scan_nodes(text)
    return _validate(data, rep, lines, retyped, strict)


def validate_document(data, file_name: str = "", strict: bool = False) -> Report:
    """Validate an already-parsed document (no line numbers)."""
    rep = Report(file_name=file_name, strict=strict)
    return _validate(data, rep, {}, [], strict)


def _validate(data, rep: Report, lines: dict, retyped: list, strict: bool) -> Report:
    def add(sev, check, msg, path=(), hint=""):
        path = tuple(path) if not isinstance(path, str) else path
        line = None
        if isinstance(path, tuple):
            for n in range(len(path), -1, -1):
                if path[:n] in lines:
                    line = lines[path[:n]]
                    break
            path = _fmt(path)
        rep.findings.append(Finding(sev, check, msg, path, hint, line))

    if not isinstance(data, dict):
        add("error", "Parse", f"The document root must be a mapping (version, name, datasets …), not a "
                              f"{type(data).__name__}.")
        rep.checks = {"Parse": "failed"}
        return rep
    rep.document = data
    version = data.get("version")
    rep.version = version
    prof = profile_for(version)
    rep.profile = prof
    if isinstance(version, str) and version in PROFILES and not prof.current:
        add("error" if strict else "warning", "Version",
            f"The document declares {version}, not the current {CURRENT_VERSION}. {prof.note}",
            ("version",), f"Use Upgrade to migrate it to {CURRENT_VERSION}."
                          + (" (Strict mode treats older versions as errors.)" if strict else ""))
    # other versions (or a non-string version) are reported by the schema's version check
    # models (the wrapped 0.1.1 shape keeps them in semantic_model)
    if prof.wrapped and isinstance(data.get("semantic_model"), list):
        rep.models = [(("semantic_model", i), m) for i, m in enumerate(data["semantic_model"]) if isinstance(m, dict)]
    elif not prof.wrapped:
        rep.models = [((), data)]
    elif "datasets" in data:          # flat document that declares 0.1.1
        rep.models = [((), data)]

    # 1. JSON Schema
    if Draft202012Validator is None:
        add("warning", "Schema", "jsonschema is not installed, so the schema was not checked.", (),
            "pip install jsonschema")
        schema_ok = True
    else:
        before = len(rep.errors)
        errs = sorted(_validator(prof.schema_file).iter_errors(data),
                      key=lambda e: [str(p) for p in e.absolute_path])
        found = [f for err in errs for f in _schema_findings(err, prof)]
        if any(f.path == "version" and f.message.startswith("Expected string") for f in found):
            # a non-string version also fails the version constant: report it once
            found = [f for f in found if not (f.path == "version" and f.message.startswith("version "))]
            for f in found:
                if f.path == "version":
                    f.hint = (f"Quote it: version: \"{CURRENT_VERSION}\" "
                              f"(supported: {', '.join(SUPPORTED_VERSIONS)}).")
        for f in found:
            add(f.severity, f.check, f.message, _parse_path(f.path), f.hint)
        schema_ok = len(rep.errors) == before
    if prof.wrapped and isinstance(data.get("semantic_model"), list) and not data["semantic_model"]:
        add("error", "Schema", "semantic_model is empty: there is no model to check.", ("semantic_model",))
        schema_ok = False
    # 2. extension payloads
    _check_extensions(rep, add, retyped)
    clean = schema_ok and not any(f.severity == "error" and f.check == "Extensions" for f in rep.findings)

    stats: dict = {}
    if clean:
        for pre, model in rep.models:
            _check_unique(model, pre, add)
            _check_references(model, pre, add, legacy=not prof.current)
            _check_arity(model, pre, add)
            _check_metric_references(model, pre, add)
            _check_field_shadowing(model, pre, add)
            _check_scope(model, pre, add)
            _check_sql(model, pre, add, stats)
            _check_drift(model, pre, add)
            _check_lints(model, pre, add, stats)
    if rep.models:
        rep.stats = {**_model_stats(rep.models), **stats}

    # check statuses
    status = {}
    for c in CHECKS:
        sev = [f.severity for f in rep.findings if f.check == c]
        if c in SEMANTIC_CHECKS and not clean:
            status[c] = "skipped"
        else:
            status[c] = "failed" if "error" in sev else "warnings" if "warning" in sev else \
                "notes" if sev else "passed"
    rep.checks = status
    order = {"error": 0, "warning": 1, "info": 2}
    rep.findings.sort(key=lambda f: (order.get(f.severity, 3), CHECKS.index(f.check)
                                     if f.check in CHECKS else 99))
    return rep


def _parse_path(path: str) -> tuple:
    """Inverse of _fmt for the paths produced by the schema findings."""
    if not path:
        return ()
    out = []
    for name, idx in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
        out.append(int(idx) if idx else name)
    return tuple(out)


# --------------------------------------------------------------------------- #
# Upgrade older documents to the current version
# --------------------------------------------------------------------------- #
def upgrade(data: dict, declare_missing_fields: bool = True) -> tuple[list[dict], list[str]]:
    """Migrate an OSI 0.1.1 or Ossie 0.2.0.dev0 document to 0.3.0.dev0.

    Returns (documents, notes): one document per model (0.1.1 files may hold several).
    """
    notes: list[str] = []
    if not isinstance(data, dict):
        return [], ["Not a mapping: nothing to upgrade."]
    data = copy.deepcopy(data)
    old = data.get("version")
    if isinstance(data.get("semantic_model"), list):
        models = [m for m in data["semantic_model"] if isinstance(m, dict)]
        if not models:
            return [], ["semantic_model is empty: there is no model to migrate."]
        dropped = [k for k in ("dialects", "vendors") if k in data]
        if dropped:
            notes.append(f"Removed the root-level {' and '.join(dropped)} declarations (apache/ossie#397).")
        notes.append("Moved the model to the document root; the semantic_model list is gone (apache/ossie#383).")
        if len(models) > 1:
            notes.append(f"The file held {len(models)} models; each became its own document.")
    else:
        models = [{k: v for k, v in data.items() if k not in ("version", "dialects", "vendors")}]
        if any(k in data for k in ("dialects", "vendors")):
            notes.append("Removed the root-level dialects/vendors declarations (apache/ossie#397).")
    docs, renamed, parsed, wrapped, declared = [], 0, 0, 0, 0
    for m in models:
        doc = {"version": CURRENT_VERSION, **{k: v for k, v in m.items() if k != "version"}}
        for holder in _ext_holders(doc):
            if "custom_extensions" not in holder:
                continue
            new = []
            for e in _as_list(holder.get("custom_extensions")):
                if not isinstance(e, dict):
                    continue
                payload = e.get("data")
                if isinstance(payload, str):
                    try:
                        value = json.loads(payload)
                    except ValueError:
                        value = payload
                    if isinstance(value, dict):
                        payload = value
                        parsed += 1
                    else:
                        payload = {"value": value}
                        wrapped += 1
                elif not isinstance(payload, dict):
                    payload = {"value": payload}
                    wrapped += 1
                new.append({"vendor_name": e.get("vendor_name"), "data": payload})
            items = list(holder.items())
            holder.clear()
            for k, v in items:
                if k == "custom_extensions":
                    holder["nonportable_extensions"] = new + _as_list(dict(items).get("nonportable_extensions"))
                elif k != "nonportable_extensions":
                    holder[k] = v
            renamed += 1
        if declare_missing_fields:
            declared += _declare_missing_fields(doc)
        docs.append(doc)
    if renamed:
        notes.append(f"Renamed {_plural(renamed, 'custom_extensions list')} to nonportable_extensions "
                     f"(apache/ossie#522); parsed {parsed} JSON-string payload(s) into objects"
                     + (f" and wrapped {wrapped} non-object payload(s) as {{value: …}}." if wrapped else "."))
    if declared:
        notes.append(f"Declared {_plural(declared, 'field')} that keys or relationships named as source "
                     f"columns ({CURRENT_VERSION} resolves those names as declared fields).")
    if old != CURRENT_VERSION:
        notes.append(f"Set version to {CURRENT_VERSION} (was {old!r}).")
    return docs, notes


def _ext_holders(model: dict):
    yield model
    for d in _dicts(model.get("datasets")):
        yield d
        yield from _dicts(d.get("fields"))
        yield from _dicts(d.get("metrics"))
    yield from _dicts(model.get("relationships"))
    yield from _dicts(model.get("metrics"))


def _declare_missing_fields(model: dict) -> int:
    datasets = {d.get("name"): d for d in _dicts(model.get("datasets")) if isinstance(d.get("name"), str)}
    added = 0

    def ensure(ds, names):
        nonlocal added
        if not isinstance(ds, dict):
            return
        have = _field_names(ds)
        for n in _as_list(names):
            if isinstance(n, str) and n and n.casefold() not in have:
                ds.setdefault("fields", []).append({"name": n, "expression": {"sql": n}})
                have = have | {n.casefold()}
                added += 1

    for d in datasets.values():
        ensure(d, d.get("primary_key"))
        for k in _as_list(d.get("unique_keys")):
            ensure(d, k)
    for r in _dicts(model.get("relationships")):
        ensure(datasets.get(r.get("from")), r.get("from_columns"))
        ensure(datasets.get(r.get("to")), r.get("to_columns"))
    return added


def dump_yaml(doc: dict) -> str:
    """YAML text for an Ossie document (version first, block style, quoted version)."""
    class _D(yaml.SafeDumper):
        pass

    def _str(dumper, s):
        if "\n" in s:
            return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", s)

    _D.add_representer(str, _str)
    return yaml.dump(doc, Dumper=_D, sort_keys=False, allow_unicode=True, width=110, default_flow_style=False)
