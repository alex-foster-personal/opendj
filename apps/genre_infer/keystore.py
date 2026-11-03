"""Genre provider keys from the macOS Keychain, so a Finder-launched engine has them (GENRE-09).

An app started from Finder or the Dock inherits launchd's environment, not a
shell's, so ``OPENROUTER_API_KEY`` and ``SERPER_API_KEY`` are never in
``os.environ`` there. The engine resolves each key from its environment first
(a developer's ``doppler run`` still wins), then from the login Keychain item
``service=com.opendj.desktop.genre-keys, account=<KEY_NAME>``
(the app's bundle id). Nothing is baked into a build: a dev build is seeded by
``scripts/dev_seed_keys.sh`` (``--local-fast`` runs it), a tester sets their own key.

Keys are written through ``python -m apps.genre_infer keys set NAME`` (value on
stdin), ``PUT /api/v1/genres/keys/{name}`` or the ``genres_set_key`` MCP tool.
Off macOS the Keychain is absent and only the environment counts.

-Claude
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping

# The running app's bundle id (apps/desktop/src-tauri/tauri.conf.json `identifier`) plus a suffix.
BUNDLE_ID = "com.opendj.desktop"
KEYCHAIN_SERVICE = f"{BUNDLE_ID}.genre-keys"
# Tests point this at a throwaway service so a developer's real keys never leak in.
KEYCHAIN_SERVICE_ENV = "MDT_GENRE_KEYCHAIN_SERVICE"
SECURITY_BIN = "/usr/bin/security"
# security(1) exits 44 when the item does not exist.
SECURITY_NOT_FOUND = 44


class KeystoreError(RuntimeError):
    """The Keychain refused a read or write for a reason other than "no such item"."""


def _service() -> str:
    return os.environ.get(KEYCHAIN_SERVICE_ENV, "").strip() or KEYCHAIN_SERVICE


def available() -> bool:
    return sys.platform == "darwin" and shutil.which(SECURITY_BIN) is not None


def read(name: str) -> str | None:
    """The Keychain value for ``name``, or None when there is no item (or no Keychain)."""
    if not available():
        return None
    proc = subprocess.run(
        [SECURITY_BIN, "find-generic-password", "-s", _service(), "-a", name, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == SECURITY_NOT_FOUND:
        return None
    if proc.returncode != 0:
        raise KeystoreError(f"Keychain read of {name} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip() or None


def write(name: str, value: str) -> None:
    """Create or replace the Keychain item for ``name``."""
    if not available():
        raise KeystoreError(f"no macOS Keychain on this host: set {name} in the engine's environment instead")
    if not value.strip():
        raise KeystoreError(f"refusing to store a blank {name}")
    proc = subprocess.run(
        [SECURITY_BIN, "add-generic-password", "-U", "-s", _service(), "-a", name, "-w", value.strip()],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise KeystoreError(f"Keychain write of {name} failed ({proc.returncode}): {proc.stderr.strip()}")


def resolve(name: str, environ: Mapping[str, str] | None = None) -> str:
    """``name`` from ``environ`` (default ``os.environ``), else the Keychain; blank when neither has it.

    An explicit ``environ`` (tests, callers passing their own mapping) is
    authoritative and the Keychain is not consulted.
    """
    if environ is not None:
        return environ.get(name, "").strip()
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return read(name) or ""


__all__ = ["BUNDLE_ID", "KEYCHAIN_SERVICE", "KEYCHAIN_SERVICE_ENV", "KeystoreError", "available", "read", "resolve", "write"]
