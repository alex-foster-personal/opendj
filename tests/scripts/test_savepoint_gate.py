"""Regression tests for scripts/savepoint_gate.py step selection and ordering.

- if --fast keeps a slow step (pytest-full or e2e-smoke) then broken
- if svelte-kit sync is not the first step in any mode then broken
- if a failing step does not stop the gate with a non-zero exit then broken
- if the pytest step loses -n then the gate silently went back to ~2x slower
- if the pytest step loses --collect-floor then parallelism can shrink the suite
  unnoticed, which is the whole risk -n introduces
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "savepoint_gate", REPO_ROOT / "scripts" / "savepoint_gate.py"
)
assert _SPEC is not None and _SPEC.loader is not None
savepoint_gate = importlib.util.module_from_spec(_SPEC)
sys.modules["savepoint_gate"] = savepoint_gate
_SPEC.loader.exec_module(savepoint_gate)


def test_fast_mode_drops_exactly_the_slow_steps() -> None:
    steps = savepoint_gate._steps()
    fast = [s for s in steps if not s.slow]
    slow = [s.name for s in steps if s.slow]
    assert slow == ["pytest-full", "e2e-smoke"], (
        f"the slow set moved: {slow}. --fast must keep proving units + types."
    )
    assert [s.name for s in fast] == ["svelte-kit-sync", "frontend-unit", "svelte-check"]


def test_svelte_kit_sync_is_always_first() -> None:
    steps = savepoint_gate._steps()
    assert steps[0].name == "svelte-kit-sync", (
        "an unsynced worktree cascades into ~46 phantom failures and a hang; "
        "sync must run before any frontend step"
    )


def test_pytest_step_is_parallel_and_floored() -> None:
    """-n buys the speed; --collect-floor is what stops it costing coverage."""
    step = next(s for s in savepoint_gate._steps() if s.name == "pytest-full")
    argv = step.argv

    assert "-n" in argv, "the gate's pytest step lost its xdist workers"
    assert argv[argv.index("-n") + 1] == savepoint_gate.PYTEST_WORKERS
    assert savepoint_gate.PYTEST_WORKERS == "auto", (
        "auto measured fastest and needs no per-machine number; a hardcoded "
        "worker count has to come with its own before/after measurement"
    )
    assert argv[argv.index("--dist") + 1] == savepoint_gate.PYTEST_DIST, (
        "loadgroup is what lets a suite opt into a shared worker with "
        "@pytest.mark.xdist_group instead of serialising the whole run"
    )

    assert "--collect-floor" in argv, (
        "-n redistributes tests but must never shrink them; without the floor "
        "a run that collects less reads as a smaller green suite"
    )
    floor = int(argv[argv.index("--collect-floor") + 1])
    assert floor >= 3700, f"the collect floor only ratchets up, got {floor}"


def test_a_failing_step_stops_the_gate_with_nonzero_exit(monkeypatch) -> None:
    ran: list[str] = []

    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def _fake_run(argv, cwd):
        name = ran_names[len(ran)]
        ran.append(name)
        return _Result(1 if name == "frontend-unit" else 0)

    ran_names = [s.name for s in savepoint_gate._steps()]
    monkeypatch.setattr(savepoint_gate.subprocess, "run", _fake_run)
    exit_code = savepoint_gate.main([])
    assert exit_code == 1
    assert ran == ["svelte-kit-sync", "frontend-unit"], (
        f"the gate kept running past a red step: {ran}"
    )


def test_all_green_returns_zero(monkeypatch) -> None:
    class _Result:
        returncode = 0

    monkeypatch.setattr(
        savepoint_gate.subprocess, "run", lambda argv, cwd: _Result()
    )
    assert savepoint_gate.main(["--fast"]) == 0
