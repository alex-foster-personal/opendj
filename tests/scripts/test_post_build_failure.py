"""OBS-01 Part 1: the build-failure CLI posts kind=build to the one sink.

Runs the real module as a subprocess against a temp JSONL. No network.

Regression lines:
  - if the CLI exit 0 path writes no JSONL row, then broken
  - if the row is missing kind=build, error_id, host, or sha, then broken
  - if a headless-dmg FAILED verdict cannot be parsed into a build event,
    then broken
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.telemetry.sink import event_from_headless_dmg_log

pytestmark = pytest.mark.requirement("OBS-01")

REPO = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_cli_writes_a_kind_build_row(tmp_path: Path) -> None:
    """if post_build_failure runs then the sink JSONL has kind=build."""
    sink = tmp_path / "opendj-error-sink.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.post_build_failure",
            "--message",
            "CI failed run=99",
            "--source-site",
            "build:CI",
            "--sha",
            SHA,
            "--host",
            "nucbox-wsl",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={
            **{k: v for k, v in __import__("os").environ.items() if k != "OPENDJ_TELEMETRY"},
            "OPENDJ_ERROR_SINK_LOG": str(sink),
            "OPENDJ_TELEMETRY": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "build"
    assert row["error_id"].startswith("eid-")
    assert row["host"] == "nucbox-wsl"
    assert row["build_sha"] == SHA
    assert "eid-" in result.stdout


def test_headless_dmg_failed_verdict_becomes_a_build_event() -> None:
    """if the log's REPORT TO USER line is FAILED then kind=build with that text."""
    log = (
        "[INFO] session=Aqua host=silver sha=abc1234 worktree=/tmp/wt\n"
        "REPORT TO USER: headless dmg build FAILED (rc=1) after 12s at sha abc1234; "
        "read the [ERROR] lines above\n"
    )
    event = event_from_headless_dmg_log(log, host="silver", build_sha=SHA)
    assert event is not None
    assert event.kind == "build"
    assert event.host == "silver"
    assert event.build_sha == SHA
    assert event.source_site == "build:headless-dmg"
    assert "FAILED" in event.message


def test_headless_dmg_ok_verdict_is_not_an_error() -> None:
    """if the log's verdict is OK then no build-failure event is minted."""
    log = (
        "REPORT TO USER: headless dmg build OK in 12s: /tmp/OpenDJ.dmg "
        "(sha abc1234, signed+notarized)\n"
    )
    assert event_from_headless_dmg_log(log, host="silver", build_sha=SHA) is None
