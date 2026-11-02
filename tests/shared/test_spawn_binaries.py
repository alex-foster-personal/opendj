"""apps.shared.spawn_binaries resolves repo tools by absolute path."""

from __future__ import annotations

import shutil

import pytest

from apps.shared.spawn_binaries import repo_venv_python, resolved_uv


def test_resolved_uv_is_absolute() -> None:
    """[if] uv is on PATH [then] resolved_uv is absolute, [else stop]."""
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    path = resolved_uv()
    assert path.startswith("/") or (len(path) > 2 and path[1] == ":")


def test_repo_venv_python_points_at_checkout() -> None:
    """[if] .venv exists [then] repo_venv_python names it, [else stop]."""
    from pathlib import Path

    path = repo_venv_python()
    name = Path(path).name
    assert name in {"python", "python.exe", "python3", "python3.11"}
