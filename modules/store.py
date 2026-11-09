"""
Save / load ontology versions in a Unity Catalog Volume.

Layout:  <ONTOLOGY_STORE>/<ontology-name>/<UTC timestamp>.yaml   (+ latest.yaml)

Writes are tried as the viewer first (their forwarded token, so Volume permissions
are respected); if the token lacks the Files scope, the app's service principal is
used — grant it READ VOLUME / WRITE VOLUME on the Volume in that case.
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone

STORE = os.getenv("ONTOLOGY_STORE", "").rstrip("/")


def enabled() -> bool:
    return STORE.startswith("/Volumes/")


def _clients(token: str | None):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    out = []
    if token:
        try:
            out.append(("you", WorkspaceClient(host=Config().host, token=token, auth_type="pat")))
        except Exception:  # noqa: BLE001
            pass
    out.append(("app service principal", WorkspaceClient()))
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-").lower() or "ontology"


def save(yaml_text: str, name: str, token: str | None) -> str:
    folder = f"{STORE}/{_slug(name)}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    errors = []
    for who, w in _clients(token):
        try:
            for path in (f"{folder}/{stamp}.yaml", f"{folder}/latest.yaml"):
                w.files.upload(path, io.BytesIO(yaml_text.encode("utf-8")), overwrite=True)
            return f"Saved {folder}/{stamp}.yaml (as {who})"
        except Exception as e:  # noqa: BLE001
            errors.append(f"{who}: {str(e)[:150]}")
    raise RuntimeError("Could not save — " + " | ".join(errors))


def list_versions(token: str | None) -> list[str]:
    for _, w in _clients(token):
        try:
            out = []
            for d in w.files.list_directory_contents(STORE):
                if d.is_directory:
                    for f in w.files.list_directory_contents(d.path):
                        if f.path.endswith(".yaml"):
                            out.append(f.path)
            return sorted(out, reverse=True)
        except Exception:  # noqa: BLE001
            continue
    return []


def load(path: str, token: str | None) -> str:
    errors = []
    for who, w in _clients(token):
        try:
            return w.files.download(path).contents.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{who}: {str(e)[:150]}")
    raise RuntimeError("Could not load — " + " | ".join(errors))
