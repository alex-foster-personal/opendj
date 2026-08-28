"""Tests for ``scripts/iteration_metrics.sh``, the iteration-speed timing shim.

The natural-language acceptance tests, one assertion each:

    if a wrapped command's exit code is not passed through, the shim is broken
    if a successful run does not append exactly one JSON line, the shim is broken
    if the appended line lacks ts/step/seconds/sha/host/exit, the shim is broken
    if a failing command is recorded as exit 0, the shim is broken
    if an unwritable store aborts the build, the shim is broken
    if an unwritable store fails silently instead of warning, the shim is broken
    if a missing step name or missing command is tolerated, the shim is broken

The store location is redirected with MDT_ITERATION_METRICS_DIR so no test ever touches
the maintainer's real ~/.local/share/mdt-iteration-metrics/metrics.jsonl.

The fail-open case is the important one here. It is the single sanctioned fail-open in this
repo, and it only earns that exemption if it is loud: a build that keeps going AND says
nothing would quietly stop recording, and the first anyone would know is a check 5 that had
gone blind weeks earlier.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[2] / "scripts" / "iteration_metrics.sh"


def _run(store: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, MDT_ITERATION_METRICS_DIR=str(store))
    return subprocess.run(
        ["/bin/bash", str(SHIM), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _records(store: Path) -> list[dict]:
    path = store / "metrics.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ----- exit-code passthrough --------------------------------------------------------


def test_successful_command_exits_zero(tmp_path: Path) -> None:
    assert _run(tmp_path / "store", "demo", "true").returncode == 0


def test_failing_command_exit_code_is_passed_through(tmp_path: Path) -> None:
    """If the wrapped command's exit code is swallowed then every gate silently passes."""
    result = _run(tmp_path / "store", "demo", "bash", "-c", "exit 37")
    assert result.returncode == 37


def test_command_stdout_reaches_the_caller(tmp_path: Path) -> None:
    result = _run(tmp_path / "store", "demo", "echo", "hello-from-wrapped")
    assert "hello-from-wrapped" in result.stdout


# ----- what gets recorded -----------------------------------------------------------


def test_one_line_is_appended_per_run(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _run(store, "demo", "true")
    _run(store, "demo", "true")
    assert len(_records(store)) == 2


def test_record_carries_the_full_schema(tmp_path: Path) -> None:
    """If any of ts/step/seconds/sha/host/exit is missing then check 5 cannot read it."""
    store = tmp_path / "store"
    _run(store, "savepoint-gate", "true")
    record = _records(store)[0]
    assert set(record) == {"ts", "step", "seconds", "sha", "host", "exit"}
    assert record["step"] == "savepoint-gate"
    assert record["exit"] == 0
    assert isinstance(record["seconds"], (int, float))


def test_failure_is_recorded_with_its_real_exit_code(tmp_path: Path) -> None:
    """If a failed run is filed as exit 0 then the store cannot tell slow from broken."""
    store = tmp_path / "store"
    _run(store, "demo", "bash", "-c", "exit 3")
    assert _records(store)[0]["exit"] == 3


def test_measured_duration_reflects_the_wrapped_command(tmp_path: Path) -> None:
    """If the timing does not track real elapsed time then every median is fiction."""
    store = tmp_path / "store"
    _run(store, "demo", "sleep", "1")
    assert _records(store)[0]["seconds"] >= 1.0


def test_store_directory_is_created_on_demand(tmp_path: Path) -> None:
    store = tmp_path / "nested" / "not" / "yet" / "there"
    assert _run(store, "demo", "true").returncode == 0
    assert (store / "metrics.jsonl").exists()


# ----- the one sanctioned fail-open -------------------------------------------------


def test_unwritable_store_does_not_break_the_build(tmp_path: Path) -> None:
    """If a broken metrics dir fails the build then measurement has become a gate."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory")
    result = _run(blocker / "store", "demo", "true")
    assert result.returncode == 0


def test_unwritable_store_warns_loudly(tmp_path: Path) -> None:
    """If the fail-open is silent then recording can stop for weeks unnoticed."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory")
    result = _run(blocker / "store", "demo", "true")
    assert "[WARN]" in result.stderr
    assert "NOT recorded" in result.stderr


def test_fail_open_still_passes_through_a_failure(tmp_path: Path) -> None:
    """If a broken store masked the command's failure then the gate would go green."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert _run(blocker / "store", "demo", "bash", "-c", "exit 9").returncode == 9


# ----- brittle everywhere else ------------------------------------------------------


@pytest.mark.parametrize("args", [(), ("step-with-no-command",)])
def test_usage_errors_exit_64(tmp_path: Path, args: tuple[str, ...]) -> None:
    """If a missing step or command is tolerated then metrics land under the wrong name."""
    result = _run(tmp_path / "store", *args)
    assert result.returncode == 64
    assert "[ERROR]" in result.stderr


def test_usage_error_writes_no_record(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _run(store, "step-with-no-command")
    assert _records(store) == []
