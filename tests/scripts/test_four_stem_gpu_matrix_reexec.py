"""Lock the GPU matrix re-exec boundary: env and argv contract, and re-exec before the farm import.

Fakes only: ``os.execve`` is replaced, so no process is replaced and no GPU or
Modal farm is touched. ``gpu=`` is resolved when ``scripts.modal_vocal_farm`` is
imported, so the re-exec must happen first or a run is billed to the wrong card.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.bench import four_stem_gpu_matrix as gm

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
MODULE: str = "scripts.bench.four_stem_gpu_matrix"


class _Replaced(Exception):
    """Stands in for the process being replaced by a successful execve."""


def _record_execve(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], dict[str, str]]]:
    """Replace os.execve with a recorder that raises _Replaced, like a real exec never returning."""
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        """Record the replacement-process arguments, then stop like a real exec."""
        calls.append((path, list(argv), dict(env)))
        raise _Replaced

    monkeypatch.setattr(gm.os, "execve", fake_execve)
    return calls


def test_reexec_when_farm_gpu_differs_sets_env_and_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """If MDT_FARM_GPU names another card, re-exec this module with MDT_FARM_GPU=GPU and the CLI args kept."""
    calls = _record_execve(monkeypatch)
    monkeypatch.setenv("MDT_FARM_GPU", "A10G")
    monkeypatch.setenv("MDT_KEEP_ME", "kept")
    monkeypatch.setattr(gm.sys, "argv", ["four_stem_gpu_matrix.py", "--models", "htdemucs", "--track", "T1"])

    with pytest.raises(_Replaced):
        gm._reexec_if_wrong_gpu()

    assert len(calls) == 1
    path, argv, env = calls[0]
    assert path == sys.executable
    assert argv == [sys.executable, "-m", MODULE, "--models", "htdemucs", "--track", "T1"]
    assert env["MDT_FARM_GPU"] == gm.GPU
    assert env["MDT_KEEP_ME"] == "kept"
    assert "MDT_FARM_GPU_REEXEC" not in env


def test_reexec_when_farm_gpu_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """If MDT_FARM_GPU is absent, re-exec is requested and the replacement process gets the configured GPU."""
    calls = _record_execve(monkeypatch)
    monkeypatch.delenv("MDT_FARM_GPU", raising=False)
    monkeypatch.setattr(gm.sys, "argv", ["four_stem_gpu_matrix.py"])

    with pytest.raises(_Replaced):
        gm._reexec_if_wrong_gpu()

    assert [(c[0], c[1], c[2]["MDT_FARM_GPU"]) for c in calls] == [
        (sys.executable, [sys.executable, "-m", MODULE], gm.GPU)
    ]


def test_no_reexec_when_farm_gpu_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """If MDT_FARM_GPU already equals the configured GPU, return without calling execve."""
    calls = _record_execve(monkeypatch)
    monkeypatch.setenv("MDT_FARM_GPU", gm.GPU)

    assert gm._reexec_if_wrong_gpu() is None
    assert calls == []


def test_main_reexecs_before_farm_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """If re-exec is requested, main replaces the process before the farm helper ever runs."""
    calls = _record_execve(monkeypatch)
    monkeypatch.setenv("MDT_FARM_GPU", "A10G")
    monkeypatch.setattr(gm.sys, "argv", ["four_stem_gpu_matrix.py"])
    farm_calls: list[tuple[str, ...]] = []

    def fake_farm(models: tuple[str, ...]) -> Any:
        """Record a farm import that must never happen before the re-exec."""
        farm_calls.append(models)
        raise AssertionError("farm imported before re-exec")

    monkeypatch.setattr(gm, "_import_and_check_farm", fake_farm)

    with pytest.raises(_Replaced):
        gm.main()

    assert len(calls) == 1
    assert farm_calls == []


def test_main_orders_reexec_then_farm_when_gpu_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the GPU already matches, main runs the re-exec check first and only then the farm import."""
    order: list[str] = []
    monkeypatch.setattr(gm.sys, "argv", ["four_stem_gpu_matrix.py"])
    monkeypatch.setattr(gm, "_reexec_if_wrong_gpu", lambda: order.append("reexec"))

    def fake_farm(models: tuple[str, ...]) -> Any:
        """Record the farm step and stop main before any track or GPU work."""
        order.append("farm")
        raise SystemExit("stop after farm")

    monkeypatch.setattr(gm, "_import_and_check_farm", fake_farm)

    with pytest.raises(SystemExit, match="stop after farm"):
        gm.main()

    assert order == ["reexec", "farm"]


def test_module_import_does_not_import_farm_or_modal() -> None:
    """Importing the runner must not import the farm or modal; both stay function-local after the re-exec."""
    code = f"import sys, {MODULE}; print(int('scripts.modal_vocal_farm' in sys.modules), int('modal' in sys.modules))"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.split() == ["0", "0"]
