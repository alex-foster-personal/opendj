"""Persistent mypy incremental cache for the quality gate (issue #1496).

The gate's mypy invocation is expensive cold and cheap warm. CI's checkout
wipes the workspace `.mypy_cache/` every run, so the ratchet paid full price
twice inside `tests/test_quality_gate.py` as well. This module keys a cache
directory outside the workspace on the mypy pin plus the `[tool.mypy]` block,
and refuses a stale directory when that key drifts.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MYPY_REQS = REPO / "ops" / "quality" / "mypy-requirements.txt"
PYPROJECT = REPO / "pyproject.toml"
CACHE_KEY_FILE = "cache-key.txt"
CACHE_ROOT_ENV = "MDT_MYPY_CACHE_DIR"
DEFAULT_ROOT = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / "mdt-mypy"


def _mypy_config_block() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data.get("tool", {}).get("mypy", {})


def config_identity() -> str:
    """Stable fingerprint of the mypy pin and scored config block."""
    pin = MYPY_REQS.read_text(encoding="utf-8").strip()
    config = json.dumps(_mypy_config_block(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{pin}\n{config}".encode()).hexdigest()
    return digest[:16]


def _full_cache_key() -> str:
    pin = MYPY_REQS.read_text(encoding="utf-8").strip()
    config = json.dumps(_mypy_config_block(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{pin}\n{config}".encode()).hexdigest()


def _default_cache_dir() -> Path:
    runner = os.environ.get("RUNNER_NAME", "local")
    return DEFAULT_ROOT / runner / config_identity()


def resolve_cache_dir() -> Path:
    """Return a cache directory, purging it when the config key no longer matches."""
    cache_dir = Path(os.environ.get(CACHE_ROOT_ENV, _default_cache_dir()))
    expected = _full_cache_key()
    key_file = cache_dir / CACHE_KEY_FILE
    if cache_dir.exists() and key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored != expected:
            shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text(expected + "\n", encoding="utf-8")
    cache_dir.chmod(0o700)
    return cache_dir


def mypy_cache_dir_flag() -> str:
    """`--cache-dir` target for a pinned mypy invocation."""
    return str(resolve_cache_dir())

_MYPY_CACHE_PROBE: int = 'warm-must-see-this'
