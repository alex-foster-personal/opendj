"""Explicit local vs remote library residency. One mode per machine.

``MDT_LIBRARY_MODE`` is ``local`` or ``remote``. This module never infers
remote from hostname. Unset is treated as local only on darwin; anywhere
else an unset or blank value is a hard fail so a leftover ``/Users`` tree
cannot silently become the library.

Remote mode requires ``MDT_CRATE_ROOT`` (absolute). Missing crate root is
a process-start failure, not a per-request fallback.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

LibraryMode = Literal["local", "remote"]

MODE_ENV: str = "MDT_LIBRARY_MODE"
CRATE_ROOT_ENV: str = "MDT_CRATE_ROOT"
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_ENV_FILE: Path = _PROJECT_ROOT / ".env"
_MODE_KEYS: tuple[str, ...] = (MODE_ENV, CRATE_ROOT_ENV)


def _read_dotenv(dotenv_path: Path) -> dict[str, str]:
    if not dotenv_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def apply_library_env(
    *,
    environ: dict[str, str] | None = None,
    dotenv_path: Path = _ENV_FILE,
) -> None:
    """Copy library-mode keys from root ``.env`` into the process env.

    Process environment wins. Does not invent a mode from hostname.
    """
    target = os.environ if environ is None else environ
    dotenv_values = _read_dotenv(dotenv_path)
    for key in _MODE_KEYS:
        if key in target and str(target[key]).strip() != "":
            continue
        value = dotenv_values.get(key)
        if value is None or value.strip() == "":
            continue
        target[key] = value.strip()


def library_mode(*, environ: Mapping[str, str] | None = None) -> LibraryMode:
    """Return the explicit library mode for this process.

    Unset is local on darwin only. Non-darwin hosts must set
    ``MDT_LIBRARY_MODE`` so a leftover ``/Users`` tree cannot win.
    """
    env = os.environ if environ is None else environ
    raw = env.get(MODE_ENV)
    if raw is None or str(raw).strip() == "":
        if sys.platform == "darwin":
            return "local"
        raise RuntimeError(
            f"{MODE_ENV} is required on this platform (set local or remote). "
            "Unset is treated as local only on darwin."
        )
    mode = str(raw).strip().lower()
    if mode in ("local", "remote"):
        return mode  # type: ignore[return-value]
    raise RuntimeError(f"{MODE_ENV} must be local or remote, got {raw!r}")


def crate_root(*, environ: Mapping[str, str] | None = None) -> Path:
    """Absolute crate root. Required in remote mode; never guessed."""
    env = os.environ if environ is None else environ
    raw = env.get(CRATE_ROOT_ENV)
    if raw is None or str(raw).strip() == "":
        raise RuntimeError(
            f"{CRATE_ROOT_ENV} is required when {MODE_ENV}=remote"
        )
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{CRATE_ROOT_ENV} must be an absolute path, got {raw!r}")
    return path


def assert_ready(*, environ: Mapping[str, str] | None = None) -> None:
    """Fail at process start when remote mode cannot serve the crate."""
    if library_mode(environ=environ) != "remote":
        return
    root = crate_root(environ=environ)
    if not root.is_dir():
        raise RuntimeError(
            f"{MODE_ENV}=remote but {CRATE_ROOT_ENV} {root} is not a directory"
        )


def is_mac_users_path(path: str) -> bool:
    """True for a Mac ``/Users/...`` library prefix (the leftover-tree trap)."""
    return path == "/Users" or path.startswith("/Users/")
