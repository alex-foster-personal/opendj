"""Periodic-checks merge-count window (DEVOPS-02, DEVOPS-03).

The window is re-derived every run from GitHub's compare API
(``total_commits`` of ``{last_sha}...main``) against the SHA recorded in
the newest trusted ledger comment. Never ``git rev-list``: a shallow
runner checkout must not change the count.

    python -m scripts.periodic_window decide --repo OWNER/NAME ...
    python -m scripts.periodic_window alarm --candidates-json '[]' ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from scripts.gh_version_guard import require_gh_min_version

BOT_LOGIN = "github-actions[bot]"
DEFAULT_MARKER = re.compile(
    r"<!-- periodic-checks state sha=([0-9a-f]{40})(?: threshold=(\d+))? -->"
)
ROW_MARKER = re.compile(
    r"<!-- periodic-checks row=(\d+) state sha=([0-9a-f]{40})(?: threshold=(\d+))? -->"
)
_HTTP_STATUS = re.compile(r"HTTP\s+(\d{3})", re.IGNORECASE)


class GhError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GhLike(Protocol):
    def api_json(self, path: str) -> Any: ...
    def api_json_paginated(self, path: str) -> list[Any]: ...


@dataclass(frozen=True)
class StateMarker:
    sha: str
    created_at: str
    threshold: int | None
    row: int | None


@dataclass(frozen=True)
class WindowDecision:
    run: bool
    count: int
    base: str
    base_at: str
    head: str
    head_at: str
    threshold: int
    elapsed_days: int
    reason: str
    record_sha: bool


class Gh:
    """``gh api`` client. Failures raise GhError; 404 is a status, not a skip."""

    def api_json(self, path: str) -> Any:
        return self._load(["gh", "api", path])

    def api_json_paginated(self, path: str) -> list[Any]:
        raw = self._load(["gh", "api", "--paginate", "--slurp", path])
        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            return [item for page in raw for item in page]
        if isinstance(raw, list):
            return raw
        return []

    def _load(self, argv: list[str]) -> Any:
        """Run a `gh` argv and parse its stdout as JSON.

        `require_gh_min_version()` runs first: this is the module whose
        `--paginate --slurp` call (`api_json_paginated`) actually crashed in
        production (agentbox-15, job 103871571683, Fri 12 Sep 2026 onward,
        `unknown flag: --slurp` on gh 2.62.0) -- the failure that took down
        every scheduled `periodic-checks.yml` run. See
        scripts/gh_version_guard.py for the version floor and citation.
        """
        require_gh_min_version()
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"{' '.join(argv)} failed"
            match = _HTTP_STATUS.search(err)
            status = int(match.group(1)) if match else None
            raise GhError(err, status=status)
        return json.loads(proc.stdout)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def select_state_marker(
    comments: list[dict[str, Any]], *, row: int | None
) -> StateMarker | None:
    """Newest github-actions[bot] comment with exactly one matching marker."""
    trusted: list[StateMarker] = []
    for comment in comments:
        login = ((comment.get("user") or {}).get("login")) or ""
        if login != BOT_LOGIN:
            continue
        body = str(comment.get("body") or "")
        created_at = str(comment.get("created_at") or "")
        parsed = _markers_in_body(body, row=row)
        if len(parsed) != 1:
            continue
        sha, threshold, found_row = parsed[0]
        trusted.append(
            StateMarker(
                sha=sha,
                created_at=created_at,
                threshold=threshold,
                row=found_row,
            )
        )
    if not trusted:
        return None
    trusted.sort(key=lambda m: m.created_at)
    return trusted[-1]


def _markers_in_body(
    body: str, *, row: int | None
) -> list[tuple[str, int | None, int | None]]:
    found: list[tuple[str, int | None, int | None]] = []
    if row is None:
        for match in DEFAULT_MARKER.finditer(body):
            threshold = int(match.group(2)) if match.group(2) else None
            found.append((match.group(1), threshold, None))
        return found
    for match in ROW_MARKER.finditer(body):
        found_row = int(match.group(1))
        if found_row != row:
            continue
        threshold = int(match.group(3)) if match.group(3) else None
        found.append((match.group(2), threshold, found_row))
    return found


def elapsed_days_since(created_at: str, now: datetime) -> int:
    then = parse_iso(created_at)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return int((current - then).total_seconds() // 86400)


def compare_total_commits(gh: GhLike, repo: str, base: str) -> int:
    payload = gh.api_json(f"repos/{repo}/compare/{base}...main")
    try:
        return int(payload["total_commits"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GhError("compare payload missing integer total_commits") from exc


def resolve_main_head(gh: GhLike, repo: str) -> tuple[str, str]:
    payload = gh.api_json(f"repos/{repo}/commits/main")
    sha = str(payload.get("sha") or "")
    if len(sha) != 40:
        raise GhError("could not resolve main")
    commit = payload.get("commit") or {}
    committer = commit.get("committer") or {}
    head_at = str(committer.get("date") or "")
    if not head_at:
        raise GhError(f"could not date head {sha}")
    return sha, head_at


def decide_window(
    *,
    gh: GhLike,
    repo: str,
    ledger_issue: str,
    threshold: int,
    force: bool,
    clock_days: int,
    row: int | None = None,
    now: datetime | None = None,
) -> WindowDecision:
    """Re-derive the merge window. Never calls git."""
    current = now or datetime.now(UTC)
    head, head_at = resolve_main_head(gh, repo)
    comments = gh.api_json_paginated(f"repos/{repo}/issues/{ledger_issue}/comments")

    def done(
        *,
        run: bool,
        count: int,
        base: str,
        base_at: str,
        elapsed_days: int,
        reason: str,
    ) -> WindowDecision:
        return WindowDecision(
            run=run,
            count=count,
            base=base,
            base_at=base_at,
            head=head,
            head_at=head_at,
            threshold=threshold,
            elapsed_days=elapsed_days,
            reason=reason,
            record_sha=run,
        )

    marker = select_state_marker(comments, row=row)
    if marker is None:
        label = f"row={row} " if row is not None else ""
        reason = f"bootstrap: no {label}state marker on issue #{ledger_issue}"
        return done(
            run=True,
            count=-1,
            base="",
            base_at="",
            elapsed_days=-1,
            reason=reason,
        )

    elapsed = elapsed_days_since(marker.created_at, current) if marker.created_at else -1
    try:
        count = compare_total_commits(gh, repo, marker.sha)
    except GhError as exc:
        if exc.status == 404:
            label = f"row={row} " if row is not None else ""
            reason = (
                f"{label}state SHA {marker.sha} is absent from history "
                f"(compare 404); bootstrapping"
            )
            return done(
                run=True,
                count=-1,
                base=marker.sha,
                base_at=marker.created_at,
                elapsed_days=elapsed,
                reason=reason,
            )
        raise

    short = marker.sha[:9]
    if force:
        reason = (
            f"forced by workflow_dispatch ({count} merges since {short}"
            + (f", {elapsed}d elapsed" if elapsed >= 0 else "")
            + (f", row={row} threshold {threshold}" if row is not None else "")
            + ")"
        )
        return done(
            run=True,
            count=count,
            base=marker.sha,
            base_at=marker.created_at,
            elapsed_days=elapsed,
            reason=reason,
        )
    if count >= threshold:
        label = f"row={row} " if row is not None else ""
        reason = f"{count} merges since {short} (>= {label}threshold {threshold})"
        return done(
            run=True,
            count=count,
            base=marker.sha,
            base_at=marker.created_at,
            elapsed_days=elapsed,
            reason=reason,
        )
    if clock_days > 0 and elapsed >= clock_days:
        reason = (
            f"{elapsed}d elapsed since {short} (>= {clock_days}d weekly floor); "
            f"only {count} merges (threshold {threshold})"
        )
        return done(
            run=True,
            count=count,
            base=marker.sha,
            base_at=marker.created_at,
            elapsed_days=elapsed,
            reason=reason,
        )
    if row is not None:
        reason = (
            f"only {count} merges since {short} (row={row} threshold {threshold})"
        )
    else:
        reason = (
            f"only {count} merges since {short} (threshold {threshold}), "
            f"{elapsed}d elapsed (< {clock_days}d weekly floor)"
        )
    return done(
        run=False,
        count=count,
        base=marker.sha,
        base_at=marker.created_at,
        elapsed_days=elapsed,
        reason=reason,
    )


def cadence_alarm(
    runs: list[dict[str, Any]],
    *,
    cadence_seconds: int,
    now: datetime,
) -> str:
    """P0 if the cadence had zero runs; P1 if still red past one cadence."""
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if not runs:
        return "P0: e2e.yml scheduled cadence has zero completed runs"
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for run in runs:
        stamp = str(run.get("createdAt") or "")
        if not stamp:
            continue
        dated.append((parse_iso(stamp), run))
    if not dated:
        return "P0: e2e.yml scheduled cadence has zero completed runs"
    dated.sort(key=lambda item: item[0])
    newest_at, newest = dated[-1]
    newest_age = (current - newest_at).total_seconds()
    if newest_age > cadence_seconds:
        return "P0: e2e.yml scheduled cadence has zero completed runs"
    if not _is_fail(newest):
        return ""
    older_fail = [
        at
        for at, run in dated
        if _is_fail(run) and (current - at).total_seconds() > cadence_seconds
    ]
    if older_fail:
        return (
            "P1: e2e.yml scheduled workflow has been red for more than one "
            "cadence period"
        )
    return ""


def _is_fail(run: dict[str, Any]) -> bool:
    conclusion = str(run.get("conclusion") or "")
    return conclusion not in {"success", "skipped", "neutral"}


def render_skip_comment(
    *,
    window_desc: str,
    count: int,
    threshold: int,
    reason: str,
    run_url: str,
) -> str:
    merges = "unknown (bootstrap window)" if count == -1 else str(count)
    return (
        f"## Periodic check window skip `{window_desc}`\n"
        f"\n"
        f"- merges in window: **{merges}** (threshold {threshold})\n"
        f"- trigger: {reason}\n"
        f"- result: SKIP (merge count under threshold)\n"
        f"- run: {run_url}\n"
        f"\n"
        f"A skip is a report, not silence. No state SHA is recorded, so the\n"
        f"next window re-measures from the same base.\n"
    )


def write_github_output(path: Path, decision: WindowDecision, *, kind: str) -> None:
    if kind == "windows_parity":
        lines = [
            f"run_windows_parity={_bool(decision.run)}",
            f"windows_parity_count={decision.count}",
            f"windows_parity_base={decision.base}",
            f"windows_parity_reason={decision.reason}",
        ]
    else:
        lines = [
            f"run={_bool(decision.run)}",
            f"count={decision.count}",
            f"base={decision.base}",
            f"base_at={decision.base_at}",
            f"head={decision.head}",
            f"head_at={decision.head_at}",
            f"threshold={decision.threshold}",
            f"elapsed_days={decision.elapsed_days}",
            f"reason={decision.reason}",
        ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _cmd_decide(args: argparse.Namespace) -> int:
    force = str(args.force).lower() in {"true", "1", "yes"}
    row = int(args.row) if args.row not in (None, "", "0") else None
    if args.kind == "windows_parity" and row is None:
        row = 10
    decision = decide_window(
        gh=Gh(),
        repo=args.repo,
        ledger_issue=str(args.ledger_issue),
        threshold=int(args.threshold),
        force=force,
        clock_days=int(args.clock_days),
        row=row,
    )
    if args.github_output:
        write_github_output(Path(args.github_output), decision, kind=args.kind)
    print(decision.reason)
    return 0


def _cmd_alarm(args: argparse.Namespace) -> int:
    if args.candidates_file:
        raw = Path(args.candidates_file).read_text(encoding="utf-8")
    else:
        raw = args.candidates_json
    runs = json.loads(raw or "[]")
    if not isinstance(runs, list):
        raise SystemExit("candidates-json must be a JSON array")
    now = parse_iso(args.now) if args.now else datetime.now(UTC)
    print(
        cadence_alarm(runs, cadence_seconds=int(args.cadence_seconds), now=now),
        end="",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.periodic_window")
    sub = parser.add_subparsers(dest="cmd", required=True)

    decide = sub.add_parser("decide", help="compute one merge window")
    decide.add_argument("--repo", default=os.environ.get("REPO", ""))
    decide.add_argument("--ledger-issue", default=os.environ.get("LEDGER_ISSUE", "1492"))
    decide.add_argument("--threshold", required=True)
    decide.add_argument("--force", default="false")
    decide.add_argument("--clock-days", default="7")
    decide.add_argument("--kind", default="default", choices=("default", "windows_parity"))
    decide.add_argument("--row", default="")
    decide.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    decide.set_defaults(func=_cmd_decide)

    alarm = sub.add_parser("alarm", help="P0/P1 for a scheduled cadence")
    alarm.add_argument("--candidates-json", default="")
    alarm.add_argument("--candidates-file", default="")
    alarm.add_argument("--cadence-seconds", default="86400")
    alarm.add_argument("--now", default="")
    alarm.set_defaults(func=_cmd_alarm)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
