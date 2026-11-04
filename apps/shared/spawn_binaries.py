"""Resolved paths for programs the repo spawns (portable-by-default seam).

Node and shell callers must not launch bare ``uv`` or ``python3`` names; they
bootstrap through :func:`repo_venv_python` and ask this module for the rest.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from apps.shared.platform_paths import IS_WINDOWS, PROJECT_ROOT


def repo_venv_python() -> str:
    """Absolute path to this checkout's ``.venv`` interpreter."""
    if IS_WINDOWS:
        candidate = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not candidate.is_file():
        raise SystemExit(f"repo venv python missing at {candidate}; run `uv sync` in {PROJECT_ROOT}")
    return str(candidate)


def resolved_uv(search_path: str | None = None) -> str:
    """``uv`` on ``search_path`` (default: this process's ``PATH``), resolved."""
    search = os.environ.get("PATH", "") if search_path is None else search_path
    uv = shutil.which("uv", path=search)
    if uv is None:
        raise SystemExit(f"uv is not on PATH ({search!r})")
    return str(Path(uv).resolve())


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: python -m apps.shared.spawn_binaries <uv|repo-python>")
    target = argv[1]
    if target == "uv":
        print(resolved_uv())
        return 0
    if target == "repo-python":
        print(repo_venv_python())
        return 0
    raise SystemExit(f"unknown target {target!r}; expected uv or repo-python")


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
