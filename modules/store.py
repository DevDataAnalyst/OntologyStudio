"""
Versioned semantic models in a Unity Catalog Volume (ONTOLOGY_STORE in app.yaml).

Layout (one folder per model):

    <ONTOLOGY_STORE>/<model>/versions/<UTC stamp>.ossie.yaml      Apache Ossie model
    <ONTOLOGY_STORE>/<model>/versions/<UTC stamp>.ontology.yaml   studio ontology (Ontology YAML tab)
    <ONTOLOGY_STORE>/<model>/latest.ossie.yaml                    newest Ossie version
    <ONTOLOGY_STORE>/<model>/history.jsonl                        one JSON line per save:
        {"version", "path", "kind", "saved_at", "saved_by", "note", "sha256", "bytes",
         "spec_version", "errors", "warnings", "restored_from"?}

Saves from before this layout (<model>/<stamp>.yaml, studio ontologies) still appear in
the history, marked "legacy".

Writes are tried as the viewer first (their forwarded token, so Volume permissions are
respected); if the token lacks the Files scope, the app's service principal is used. Grant
it READ VOLUME / WRITE VOLUME on the Volume in that case.

For local development, ONTOLOGY_STORE may also be a plain directory (e.g. ./store).
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

STORE = os.getenv("ONTOLOGY_STORE", "").strip().rstrip("/")
OSSIE, ONTOLOGY = "ossie", "ontology"


def root() -> str:
    return STORE


def enabled() -> bool:
    r = root()
    return r.startswith("/Volumes/") or (bool(r) and os.path.isdir(r))


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", str(name or "")).strip("-").lower() or "model"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")             # microseconds: no clashes


def pretty_stamp(s: str) -> str:
    m = re.match(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)", s or "")
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]} UTC" if m else (s or "")


# --------------------------------------------------------------------------- #
# File backends
# --------------------------------------------------------------------------- #
class _Local:
    who = "local folder"

    def read(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def write(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        if not os.path.isdir(path):
            return []
        return [(os.path.join(path, n), os.path.isdir(os.path.join(path, n))) for n in sorted(os.listdir(path))]

    def exists(self, path: str) -> bool:
        return os.path.exists(path)


class _Volume:
    def __init__(self, who: str, client):
        self.who, self.w = who, client

    def read(self, path: str) -> bytes:
        return self.w.files.download(path).contents.read()

    def write(self, path: str, data: bytes) -> None:
        self.w.files.upload(path, io.BytesIO(data), overwrite=True)

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        try:
            return [(d.path.rstrip("/"), bool(d.is_directory)) for d in self.w.files.list_directory_contents(path)]
        except Exception as e:  # noqa: BLE001
            if "NOT_FOUND" in str(e).upper() or "does not exist" in str(e).lower() or "404" in str(e):
                return []
            raise

    def exists(self, path: str) -> bool:
        try:
            self.w.files.get_metadata(path)
            return True
        except Exception:  # noqa: BLE001
            return False


def backends(token: str | None, path: str | None = None) -> list:
    """Clients to try in order, for a path under /Volumes (viewer first) or a local folder."""
    path = path or root()
    if not path.startswith("/Volumes/"):
        return [_Local()]
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    out = []
    if token:
        try:
            out.append(_Volume("you", WorkspaceClient(host=Config().host, token=token, auth_type="pat")))
        except Exception:  # noqa: BLE001
            pass
    try:
        out.append(_Volume("app service principal", WorkspaceClient()))
    except Exception:  # noqa: BLE001
        pass
    return out


def _try(token, fn, what: str, path: str | None = None):
    errors = []
    for b in backends(token, path):
        try:
            return fn(b)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{b.who}: {str(e)[:180]}")
    raise RuntimeError(f"Could not {what} — " + (" | ".join(errors) or "no Databricks client available"))


def write_text(path: str, text: str, token: str | None) -> str:
    """Write one file (any /Volumes path or local path); returns who wrote it."""
    def go(b):
        b.write(path, text.encode("utf-8"))
        return b.who
    return _try(token, go, f"write {path}", path)


def read_text(path: str, token: str | None) -> str:
    return _try(token, lambda b: b.read(path).decode("utf-8"), f"read {path}", path)


def read_or_none(path: str, token: str | None) -> str | None:
    for b in backends(token, path):
        try:
            return b.read(path).decode("utf-8")
        except Exception:  # noqa: BLE001
            continue
    return None


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
@dataclass
class Version:
    model: str
    version: str
    path: str
    kind: str = OSSIE
    saved_at: str = ""
    saved_by: str = ""
    note: str = ""
    sha256: str = ""
    spec_version: str = ""
    errors: int | None = None
    warnings: int | None = None
    legacy: bool = False
    restored_from: str = ""
    written_as: str = ""

    def label(self) -> str:
        bits = [pretty_stamp(self.version), "Ossie" if self.kind == OSSIE else "studio ontology"]
        if self.legacy:
            bits.append("legacy")
        if self.saved_by:
            bits.append(self.saved_by)
        if self.note:
            bits.append(f"“{self.note[:60]}”")
        return " · ".join(bits)

    def as_row(self) -> dict:
        return {"version": pretty_stamp(self.version), "kind": self.kind + (" (legacy)" if self.legacy else ""),
                "saved by": self.saved_by, "note": self.note, "spec": self.spec_version,
                "errors": self.errors, "warnings": self.warnings, "sha256": self.sha256[:12], "path": self.path}


def model_dir(model: str) -> str:
    return f"{root()}/{slug(model)}"


def save(text: str, model: str, token: str | None, kind: str = OSSIE, note: str = "", author: str = "",
         report=None, restored_from: str = "") -> Version:
    """Save a new version, update latest.ossie.yaml (Ossie only) and append to history.jsonl."""
    m = slug(model)
    folder = model_dir(m)
    st_ = stamp()
    path = f"{folder}/versions/{st_}.{kind}.yaml"
    data = text.encode("utf-8")
    v = Version(model=m, version=st_, path=path, kind=kind,
                saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), saved_by=author or "",
                note=note.strip(), sha256=hashlib.sha256(data).hexdigest(), restored_from=restored_from)
    if report is not None:
        v.spec_version = str(report.version or "")
        v.errors, v.warnings = len(report.errors), len(report.warnings)
    entry = {k: val for k, val in v.__dict__.items() if k not in ("legacy", "written_as") and val not in ("", None)}
    entry["bytes"] = len(data)

    def go(b):
        b.write(path, data)
        if kind == OSSIE:
            b.write(f"{folder}/latest.ossie.yaml", data)
        try:
            prev = b.read(f"{folder}/history.jsonl").decode("utf-8")
        except Exception:  # noqa: BLE001
            prev = ""
        if prev and not prev.endswith("\n"):
            prev += "\n"
        b.write(f"{folder}/history.jsonl", (prev + json.dumps(entry) + "\n").encode("utf-8"))
        return b.who
    v.written_as = _try(token, go, "save the version")
    return v


def list_models(token: str | None) -> list[str]:
    def go(b):
        return sorted(os.path.basename(p) for p, is_dir in b.listdir(root()) if is_dir)
    try:
        return _try(token, go, "list models")
    except RuntimeError:
        return []


def history(model: str, token: str | None) -> list[Version]:
    """All versions of a model, newest first (history.jsonl + files it does not list)."""
    folder = model_dir(model)

    def go(b):
        out: dict[str, Version] = {}
        try:
            lines = b.read(f"{folder}/history.jsonl").decode("utf-8").splitlines()
        except Exception:  # noqa: BLE001
            lines = []
        for ln in lines:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            known = {k: d[k] for k in Version.__dataclass_fields__ if k in d}
            known.setdefault("model", slug(model))
            if "path" in known and "version" in known:
                out[known["path"]] = Version(**known)
        for p, is_dir in b.listdir(f"{folder}/versions"):
            mt = re.match(r".*/(\d{8}T\d+Z)\.(ossie|ontology)\.yaml$", p)
            if not is_dir and mt and p not in out:
                out[p] = Version(model=slug(model), version=mt[1], path=p, kind=mt[2])
        for p, is_dir in b.listdir(folder):          # legacy layout: <model>/<stamp>.yaml
            mt = re.match(r".*/(\d{8}T\d{6}Z)\.yaml$", p)
            if not is_dir and mt and p not in out:
                out[p] = Version(model=slug(model), version=mt[1], path=p, kind=ONTOLOGY, legacy=True)
        return sorted(out.values(), key=lambda v: v.version, reverse=True)
    try:
        return _try(token, go, "read the history")
    except RuntimeError:
        return []


def all_versions(token: str | None) -> list[Version]:
    out = []
    for m in list_models(token):
        out += history(m, token)
    return sorted(out, key=lambda v: v.version, reverse=True)


def load(path: str, token: str | None) -> str:
    return read_text(path, token)


def diff(old_text: str, new_text: str, old_name: str = "older", new_name: str = "newer") -> str:
    return "".join(difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        fromfile=old_name, tofile=new_name, n=3))


def restore(v: Version, token: str | None, author: str = "") -> Version:
    """Copy an old version forward as the newest one (nothing is overwritten)."""
    text = load(v.path, token)
    return save(text, v.model, token, kind=v.kind, author=author,
                note=f"Restored version {pretty_stamp(v.version)}", restored_from=v.path)


def kind_of_text(text: str) -> str:
    """'ossie' for an Apache Ossie model, 'ontology' for a studio ontology."""
    import yaml
    try:
        d = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return OSSIE
    if isinstance(d, dict) and "classes" in d and "object_properties" in d:
        return ONTOLOGY
    return OSSIE
