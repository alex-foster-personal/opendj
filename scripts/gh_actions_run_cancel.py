"""Fleet-visible audit log for GitHub Actions run cancellations (10xHunter X25).

Manual ``gh run cancel`` on PR E2E runs left no row in fleet logs. Every cancel,
whether automated or operator-driven, goes through :func:`cancel_run` so nucbox can
grep ``~/jobs/logs/actions-run-cancel.log``. On ephemeral hosted runners the same row
is also emitted as a GitHub Actions ``::notice`` before the cancel POST so self-cancel
cannot drop the audit intent.

Supersedes: bare ``gh run cancel`` and ``gh api --method POST
repos/{owner}/{repo}/actions/runs/{id}/cancel`` for in-repo automation
(``scripts.ci_fast_cancel``, ``scripts.ci_trunk_tip_only``). Operators and E2E
teardown should call ``python -m scripts.gh_actions_run_cancel`` instead.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

DEFAULT_REPO = "private_owner/music-dj-tools"
LOG_ENV = "MDT_ACTIONS_CANCEL_LOG"
LINE_PREFIX = "actions-run-cancel"

_NOT_YET_QUEUED_CANCEL_MARKERS = ("HTTP 409", "not been queued yet")


class CancelOutcome(Enum):
    """Result of one cancel POST."""

    REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SKIPPED_NOT_YET_QUEUED = "skipped_not_yet_queued"
    ALREADY_COMPLETED = "already_completed"
    DRY_RUN = "dry_run"


def cancel_log_path() -> Path:
    override = os.environ.get(LOG_ENV)
    if override:
        return Path(override).expanduser()
    # Fleet sink only: :func:`append_cancel_log` mkdirs the parent on write. No
    # machine-local fallback that disappears on ephemeral runners without an override.
    return Path.home() / "jobs" / "logs" / "actions-run-cancel.log"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_cancel_log_line(
    *,
    run_id: int,
    outcome: CancelOutcome,
    reason: str,
    actor: str,
    workflow: str,
    repo: str,
) -> str:
    return (
        f"{LINE_PREFIX} ts={_utc_now()} repo={repo} run_id={run_id} "
        f"outcome={outcome.value} reason={reason} actor={actor} workflow={workflow}"
    )


def _publish_cancel_audit(line: str) -> None:
    """Fleet-visible sinks: stdout, nucbox file (see :func:`append_cancel_log`), and on
    ephemeral hosted runners a GitHub Actions ``::notice`` so the row survives self-cancel.
    """
    print(line, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = line.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::notice title={LINE_PREFIX}::{escaped}", flush=True)


def append_cancel_log(
    *,
    run_id: int,
    outcome: CancelOutcome,
    reason: str,
    actor: str,
    workflow: str,
    repo: str,
    log_path: Path | None = None,
) -> None:
    path = log_path or cancel_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _format_cancel_log_line(
        run_id=run_id,
        outcome=outcome,
        reason=reason,
        actor=actor,
        workflow=workflow,
        repo=repo,
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    _publish_cancel_audit(line)


def _is_not_yet_queued(exc: Exception) -> bool:
    message = str(exc)
    return all(marker in message for marker in _NOT_YET_QUEUED_CANCEL_MARKERS)


def _resolve_actor(actor: str) -> str:
    """GitHub workflow initiator when unset; local OS user only off Actions."""
    if actor:
        return actor
    return (
        os.environ.get("GITHUB_ACTOR")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or "unknown"
    )


def cancel_run(
    run_id: int,
    *,
    repo: str = DEFAULT_REPO,
    reason: str,
    actor: str = "",
    workflow: str = "",
    dry_run: bool = False,
    run_gh: Callable[[list[str]], str] | None = None,
    gh_api_json: Callable[[str], dict | list | None] | None = None,
    log_path: Path | None = None,
) -> CancelOutcome:
    actor = _resolve_actor(actor)
    if dry_run:
        append_cancel_log(
            run_id=run_id,
            outcome=CancelOutcome.DRY_RUN,
            reason=reason,
            actor=actor,
            workflow=workflow,
            repo=repo,
            log_path=log_path,
        )
        return CancelOutcome.DRY_RUN

    from scripts.ci_health_core import PreconditionError, _gh_api_json, _run_gh

    run_gh = run_gh or _run_gh
    gh_api_json = gh_api_json or _gh_api_json

    append_cancel_log(
        run_id=run_id,
        outcome=CancelOutcome.REQUESTED,
        reason=reason,
        actor=actor,
        workflow=workflow,
        repo=repo,
        log_path=log_path,
    )

    try:
        run_gh(["api", "--method", "POST", f"repos/{repo}/actions/runs/{run_id}/cancel"])
        outcome = CancelOutcome.CANCELLED
    except PreconditionError as exc:
        if _is_not_yet_queued(exc):
            outcome = CancelOutcome.SKIPPED_NOT_YET_QUEUED
        elif "HTTP 409" in str(exc):
            status = gh_api_json(f"repos/{repo}/actions/runs/{run_id}")
            if isinstance(status, dict) and status.get("status") == "completed":
                line = f"cancel-already-completed run_id={run_id} reason=already-completed"
                print(f"::notice::{line}")
                print(line)
                outcome = CancelOutcome.ALREADY_COMPLETED
            else:
                raise
        else:
            raise

    append_cancel_log(
        run_id=run_id,
        outcome=outcome,
        reason=reason,
        actor=actor,
        workflow=workflow,
        repo=repo,
        log_path=log_path,
    )
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--workflow", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cancel_run(
        args.run_id,
        repo=args.repo,
        reason=args.reason,
        actor=args.actor,
        workflow=args.workflow,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
