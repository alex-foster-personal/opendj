"""PERF-CAPTURE-04: portable library scroll frame bench runner contract.

The just recipe must call scripts/library_scroll_frames_bench.py, not bash.
Extra Playwright args pass through; the runner stops at the first non-zero step.

[if] the bench recipe or runner drifts from that contract [then] fail the test, [else stop].
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import library_scroll_frames_bench as bench

pytestmark = pytest.mark.requirement("PERF-CAPTURE-04")

REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = REPO_ROOT / "justfile"


def _recipe_body(name: str) -> list[str]:
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name} *args:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        body.append(line)
    return body


def test_runner_step_order_cwd_and_passthrough(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] pnpm is on PATH [then] sync, build and playwright run in order from FRONTEND, [else stop]."""
    fake_pnpm = tmp_path / "pnpm"
    fake_pnpm.write_text("", encoding="utf-8")
    calls: list[tuple[list[str], Path, dict]] = []

    monkeypatch.setattr(bench.shutil, "which", lambda name: str(fake_pnpm) if name == "pnpm" else None)

    def fake_run(
        argv: list[str], *, cwd: Path, check: bool = False, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), cwd, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(bench.subprocess, "run", fake_run)

    assert bench.main(["--repeat-each", "2"]) == 0
    expected = bench.steps(str(fake_pnpm), ["--repeat-each", "2"])
    assert len(calls) == 3
    for (argv, cwd, kwargs), want in zip(calls, expected, strict=True):
        assert argv == want
        assert argv[0] == str(fake_pnpm)
        assert cwd == bench.FRONTEND
        assert kwargs.get("shell") not in (True, "true")
    last_argv = calls[2][0]
    assert bench.CONFIG in last_argv
    assert last_argv[last_argv.index(bench.SPEC) + 1 :] == ["--repeat-each", "2"]


def test_runner_stops_on_first_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the build step exits non-zero [then] playwright never runs and the code is returned, [else stop]."""
    fake_pnpm = tmp_path / "pnpm"
    fake_pnpm.write_text("", encoding="utf-8")
    step = 0

    monkeypatch.setattr(bench.shutil, "which", lambda name: str(fake_pnpm) if name == "pnpm" else None)

    def fake_run(
        argv: list[str], *, cwd: Path, check: bool = False, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal step
        step += 1
        code = 3 if step == 2 else 0
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(bench.subprocess, "run", fake_run)

    assert bench.main([]) == 3
    assert step == 2


def test_runner_refuses_missing_pnpm(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] pnpm is absent from PATH [then] exit with an error naming pnpm, [else stop]."""
    monkeypatch.setattr(bench.shutil, "which", lambda _name: None)
    run = MagicMock()
    monkeypatch.setattr(bench.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        bench.main([])
    assert "pnpm" in str(exc.value)
    run.assert_not_called()


def test_just_recipe_has_no_shell() -> None:
    """[if] library-scroll-frames-bench is defined [then] its body is the Python runner only, [else stop]."""
    body = _recipe_body("library-scroll-frames-bench")
    text = "\n".join(body)
    assert "#!" not in text
    assert "bash" not in text
    assert "set -euo" not in text
    assert "scripts/library_scroll_frames_bench.py" in text


def test_runner_targets_exist_on_disk() -> None:
    """[if] config or spec paths move [then] this test fails before the bench does, [else stop]."""
    assert (bench.FRONTEND / bench.CONFIG).is_file()
    assert (bench.FRONTEND / "tests" / "e2e" / bench.SPEC).is_file()
