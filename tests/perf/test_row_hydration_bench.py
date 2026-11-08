"""PERF-UI-9952: the row hydration bench times build_track_rows per row without touching the user's data dir.

[if] the bench omits per-row medians, loses rows or writes into OPENDJ_DATA_DIR [then] fail, [else stop].
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.requirement("PERF-UI-9952")]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run_bench(*extra: str, env_data_dir: Path | None) -> dict[str, object]:
    env = {key: value for key, value in os.environ.items() if key != "OPENDJ_DATA_DIR"}
    if env_data_dir is not None:
        env["OPENDJ_DATA_DIR"] = os.fspath(env_data_dir)
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.perf.row_hydration_bench", "--tracks", "200", "--repeats", "2", "--json", *extra],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {os.fspath(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_json_run_prints_per_row_median_p95_and_200_rows(tmp_path: Path) -> None:
    """[if] --tracks 200 --repeats 2 --json misses a metric or rows != 200 [then] fail, [else stop]."""
    result = _run_bench(env_data_dir=None)
    assert result["rows"] == 200
    assert isinstance(result["per_row_ms_median"], float) and result["per_row_ms_median"] > 0
    assert isinstance(result["per_row_ms_p95"], float) and result["per_row_ms_p95"] >= result["per_row_ms_median"]
    assert result["profile_top"] == []


def test_profile_adds_ten_named_functions_with_cumulative_seconds() -> None:
    """[if] --profile does not add 10 entries naming functions and cumulative seconds [then] fail, [else stop]."""
    result = _run_bench("--profile", env_data_dir=None)
    top = result["profile_top"]
    assert isinstance(top, list) and len(top) == 10
    for entry in top:
        assert isinstance(entry["function"], str) and entry["function"]
        assert isinstance(entry["cumulative_s"], float) and entry["cumulative_s"] >= 0
    assert any("build_track_rows" in entry["function"] for entry in top)


def test_the_users_data_dir_is_left_unchanged(tmp_path: Path) -> None:
    """[if] the bench writes into the OPENDJ_DATA_DIR it was given [then] fail, [else stop]."""
    sentinel_dir = tmp_path / "users-data-dir"
    (sentinel_dir / "state").mkdir(parents=True)
    (sentinel_dir / "sentinel.txt").write_text("do not touch\n")
    before = _snapshot(sentinel_dir)
    _run_bench(env_data_dir=sentinel_dir)
    assert _snapshot(sentinel_dir) == before
    assert sorted(path.name for path in sentinel_dir.rglob("*")) == ["sentinel.txt", "state"]
