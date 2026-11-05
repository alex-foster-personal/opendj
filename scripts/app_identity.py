"""The app's name, bundle identifier and version, from their one source.

``apps/desktop/electron/package.json`` carries them: ``productName``,
``opendj.identifier`` and ``version`` (electron-builder and ``app.getVersion()``
read the same file). The engine payload stamps them into its manifest
(``scripts/build_engine_payload.py``, ``scripts/build_engine_payload_win.py``),
so a payload build no longer reads the Tauri shell's ``tauri.conf.json``.
``tauri.conf.json`` keeps a copy for the Tauri shell while it ships, held equal
by ``tests/scripts/test_app_identity_source.py`` (ELECTRON-8791).

-Claude
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from apps.shared import app_version

APP_PACKAGE_REL: str = app_version.APP_PACKAGE_REL.as_posix()


class AppIdentityError(ValueError):
    """The app package cannot name the app: a build must stop, not guess."""


@dataclass(frozen=True)
class AppIdentity:
    product_name: str
    identifier: str
    version: str


def _required_string(value: object, field: str, source: Path) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise AppIdentityError(f"{source} has no non-empty string {field!r}; the build cannot name the app")
    return value.strip()


def read_app_identity(repo_root: Path) -> AppIdentity:
    """``productName``, ``opendj.identifier`` and ``version`` from the app package."""
    source = repo_root / APP_PACKAGE_REL
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppIdentityError(f"{source} could not be read as JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise AppIdentityError(f"{source} is not a JSON object")
    opendj = doc.get("opendj")
    return AppIdentity(
        product_name=_required_string(doc.get("productName"), "productName", source),
        identifier=_required_string(
            opendj.get("identifier") if isinstance(opendj, dict) else None, "opendj.identifier", source
        ),
        version=_required_string(doc.get("version"), "version", source),
    )
