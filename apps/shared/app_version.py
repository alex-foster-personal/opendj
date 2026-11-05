"""The one app version every Open DJ surface reports.

The launch-name ADR (``(private path)``)
names one canonical app version: the semver the release lane bumps, the updater
compares and the ``app-v<semver>`` tag carries. It lives in
``apps/desktop/electron/package.json`` ``version``, which electron-builder and
``app.getVersion()`` read natively; ``tauri.conf.json`` carries a copy that
``tests/scripts/test_app_identity_source.py`` holds equal until the Tauri shell
is deleted (ELECTRON-8791). The engine's ``engine_version``, the backend's
Settings ``version`` and ``/api/health`` used to carry their own literal
``0.1.0`` beside it, so a public 1.0 alpha reported 0.1.0 (public-launch sweep
N12, Wed 7 Oct 2026). They all read this module instead.

Resolution order, with no network and no subprocess:

1. A packaged engine: the payload manifest the launcher names in
   ``OPENDJ_PAYLOAD_MANIFEST``. ``scripts/build_engine_payload.py`` stamps
   ``identity.app_version`` from that ``package.json`` at build time.
2. A repo checkout (no manifest named): that ``package.json`` itself.
3. The source that applies is unreadable: :data:`UNKNOWN_VERSION`. Never a
   made-up number, and never the other source standing in for it.

It lives in ``apps.shared`` and imports nothing from ``apps``, so the engine
(``apps.engine_core.config``) and the web UI (``apps.webui``) can both read it
without crossing the import-layer contracts in ``.importlinter``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

MANIFEST_ENV: str = "OPENDJ_PAYLOAD_MANIFEST"
APP_PACKAGE_REL: Path = Path("apps/desktop/electron/package.json")
UNKNOWN_VERSION: str = "unknown"

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _version_field(raw: object) -> str | None:
    if isinstance(raw, str) and raw.strip() != "":
        return raw.strip()
    return None


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    # UnicodeDecodeError: a file that is not UTF-8 is unreadable too, and an
    # import-time version lookup must not take engine startup down with it.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _from_manifest(manifest_path: Path) -> str | None:
    doc = _read_json(manifest_path)
    identity = doc.get("identity") if isinstance(doc, dict) else None
    if not isinstance(identity, dict):
        return None
    return _version_field(identity.get("app_version"))


def app_package_version(repo_root: Path) -> str | None:
    """``version`` from the app's ``package.json`` under ``repo_root``, or None."""
    doc = _read_json(repo_root / APP_PACKAGE_REL)
    return _version_field(doc.get("version")) if isinstance(doc, dict) else None


def resolve_app_version(environ: Mapping[str, str] | None = None, repo_root: Path = REPO_ROOT) -> str:
    """The app version this process reports, per the module docstring."""
    env = os.environ if environ is None else environ
    manifest = env.get(MANIFEST_ENV, "").strip()
    if manifest:
        # A packaged engine answers from its own manifest or not at all: the
        # source tree beside it is not what shipped.
        return _from_manifest(Path(manifest)) or UNKNOWN_VERSION
    return app_package_version(repo_root) or UNKNOWN_VERSION


APP_VERSION: str = resolve_app_version()
