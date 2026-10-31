"""``scripts.gh_actions_run_cancel`` logs every cancel for fleet tooling (X25).

Regression lines:
  - if a successful cancel does not append to the configured log then broken
  - if dry-run calls gh then broken
  - if a completed-run 409 is not logged as already_completed then broken
  - if a not-yet-queued 409 is not logged as skipped_not_yet_queued then broken
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.ci_health_core import PreconditionError
from scripts.gh_actions_run_cancel import (
    LOG_ENV,
    CancelOutcome,
    append_cancel_log,
    cancel_log_path,
    cancel_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cancel_log_path_defaults_to_fleet_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """if the default path is not the fleet log then CI can write off-host caches"""
    monkeypatch.delenv(LOG_ENV, raising=False)
    assert cancel_log_path() == Path.home() / "jobs" / "logs" / "actions-run-cancel.log"


def test_append_cancel_log_uses_unix_newlines(tmp_path: Path) -> None:
    log = tmp_path / "log"
    append_cancel_log(
        run_id=1,
        outcome=CancelOutcome.CANCELLED,
        reason="manual-e2e",
        actor="tester",
        workflow="E2E",
        repo="private_owner/music-dj-tools",
        log_path=log,
    )
    assert b"\r" not in log.read_bytes()


def test_append_cancel_log_writes_a_structured_line(tmp_path: Path) -> None:
    log = tmp_path / "actions-run-cancel.log"
    append_cancel_log(
        run_id=99,
        outcome=CancelOutcome.CANCELLED,
        reason="manual-e2e",
        actor="tester",
        workflow="E2E",
        repo="private_owner/music-dj-tools",
        log_path=log,
    )
    text = log.read_text(encoding="utf-8")
    assert "actions-run-cancel ts=" in text
    assert "run_id=99" in text
    assert "reason=manual-e2e" in text
    assert "workflow=E2E" in text


def test_dry_run_never_calls_gh(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _boom(_args: list[str]) -> str:
        calls.append(_args)
        raise AssertionError("gh must not run on dry-run")

    outcome = cancel_run(
        1,
        reason="manual-e2e",
        workflow="E2E",
        dry_run=True,
        log_path=tmp_path / "log",
        run_gh=_boom,
        gh_api_json=lambda _path: {},
    )
    assert outcome is CancelOutcome.DRY_RUN
    assert calls == []


def test_cancel_run_logs_success(tmp_path: Path) -> None:
    log = tmp_path / "log"
    outcome = cancel_run(
        42,
        reason="manual-e2e",
        workflow="E2E",
        log_path=log,
        run_gh=lambda _args: "",
        gh_api_json=lambda _path: {},
    )
    assert outcome is CancelOutcome.CANCELLED
    text = log.read_text(encoding="utf-8")
    assert "outcome=cancel_requested" in text
    assert "outcome=cancelled" in text


_SELF_CANCEL_CHILD = textwrap.dedent(
    """
    import os
    from pathlib import Path

    from scripts.gh_actions_run_cancel import cancel_run

    os.environ["GITHUB_ACTIONS"] = "true"
    cancel_run(
        9,
        reason="manual-e2e",
        workflow="E2E",
        log_path=Path(os.environ["MDT_CANCEL_PROBE_LOG"]),
    )
    """
)

_SELF_CANCEL_GH = textwrap.dedent(
    """
    import os
    import signal
    import sys

    _EXPECTED = [
        "api",
        "--method",
        "POST",
        "repos/private_owner/music-dj-tools/actions/runs/9/cancel",
    ]

    if sys.argv[1:] != _EXPECTED:
        print("unexpected gh argv: " + " ".join(sys.argv), file=sys.stderr)
        raise SystemExit(2)

    with open(os.environ["MDT_CANCEL_PROBE_GH_LOG"], "a", encoding="utf-8") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\\n")

    os.kill(os.getppid(), signal.SIGKILL)
    """
)


def _install_self_cancel_gh_stub(tmp_path: Path) -> Path:
    """A real ``gh`` on PATH: records the cancel POST, then SIGKILLs its parent like Actions."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "gh"
    launcher.write_text("#!" + sys.executable + "\n" + _SELF_CANCEL_GH, encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path / "gh-invocations.log"


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(signal, "SIGKILL"),
    reason="Actions self-cancel probe models POSIX SIGKILL after gh POST",
)
def test_cancel_run_publishes_github_notice_before_post(tmp_path: Path) -> None:
    """if GITHUB_ACTIONS is set then intent is visible before self-cancel can kill the job"""
    gh_log = _install_self_cancel_gh_stub(tmp_path)
    audit_log = tmp_path / "cancel-audit.log"
    env = os.environ.copy()
    env.pop("PYTHONUNBUFFERED", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["MDT_CANCEL_PROBE_LOG"] = str(audit_log)
    env["MDT_CANCEL_PROBE_GH_LOG"] = str(gh_log)
    completed = subprocess.run(
        [sys.executable, "-c", _SELF_CANCEL_CHILD],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    out = completed.stdout
    assert "::notice title=actions-run-cancel::" in out
    assert "outcome=cancel_requested" in out
    assert "outcome=cancelled" not in out
    audit = audit_log.read_text(encoding="utf-8")
    assert "outcome=cancel_requested" in audit
    assert "outcome=cancelled" not in audit
    assert gh_log.read_text(encoding="utf-8").strip() == (
        "api --method POST repos/private_owner/music-dj-tools/actions/runs/9/cancel"
    )


def test_completed_run_409_counts_as_already_completed(tmp_path: Path) -> None:
    def _raise_409(_args: list[str]) -> str:
        raise PreconditionError(
            "gh api --method POST repos/example/actions/runs/1/cancel failed with exit 1: "
            "HTTP 409"
        )

    log = tmp_path / "log"
    outcome = cancel_run(
        1,
        reason="manual-e2e",
        log_path=log,
        run_gh=_raise_409,
        gh_api_json=lambda _path: {"status": "completed"},
    )
    assert outcome is CancelOutcome.ALREADY_COMPLETED
    assert "outcome=already_completed" in log.read_text(encoding="utf-8")


def test_cancel_run_prefers_github_actor_over_os_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if actor is omitted on Actions then the audit row names GITHUB_ACTOR not the runner account"""
    monkeypatch.setenv("GITHUB_ACTOR", "maintainer")
    monkeypatch.setenv("USER", "runner-shared")
    log = tmp_path / "log"
    cancel_run(
        5,
        reason="manual-e2e",
        workflow="E2E",
        log_path=log,
        run_gh=lambda _args: "",
        gh_api_json=lambda _path: {},
    )
    assert "actor=maintainer" in log.read_text(encoding="utf-8")


def test_not_yet_queued_409_is_logged_without_raising(tmp_path: Path) -> None:
    def _raise_not_queued(_args: list[str]) -> str:
        raise PreconditionError(
            "gh api --method POST repos/example/actions/runs/1/cancel failed with exit 1: "
            "gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)"
        )

    log = tmp_path / "log"
    outcome = cancel_run(
        1,
        reason="manual-e2e",
        log_path=log,
        run_gh=_raise_not_queued,
        gh_api_json=lambda _path: {},
    )
    assert outcome is CancelOutcome.SKIPPED_NOT_YET_QUEUED
    assert "outcome=skipped_not_yet_queued" in log.read_text(encoding="utf-8")
