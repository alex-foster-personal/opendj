"""Enforce reviewer:* label leases before a PR branch is updated (issue #272).

A `reviewer:codex` or `reviewer:claude` label is a short-lived lease while a
fleet actively gates, rebases, or merges. Builders and merge lanes must read
the lease before `git push` or `gh pr update-branch` so non-owners fail fast
with the active reviewer and exact head SHA instead of colliding on
`--force-with-lease` and replaying tests.

Requirements (mini-PRD):
  / Refuse non-owner branch updates when a reviewer lease is active.
    [if] PR has reviewer:codex and actor fleet is not codex [then] exit 1 [else stop].
  / Handoff releases reviewer:* and adds handoff:finished-no-merge.
    [if] handoff completes [then] no reviewer label remains [else stop].
  / Emergency bypass posts explicit commit slices before allowing push.
    [if] emergency runs [then] PR comment lists each new commit [else stop].
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scripts.review_gh import TriageError, _gh
from scripts.review_lane import pinned_head

REVIEWER_PREFIX = "reviewer:"
HANDOFF_LABEL = "handoff:finished-no-merge"
FLEETS = frozenset({"codex", "claude"})
ZERO_SHA = "0" * 40


class LeaseBlocked(RuntimeError):
    """A non-owner attempted to update a branch under an active reviewer lease."""


@dataclass(frozen=True)
class ReviewerLease:
    pr: str
    fleet: str
    label: str
    head_sha: str


# ----- pure helpers ---------------------------------------------------------


def _label_names(labels: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for label in labels:
        if isinstance(label, str):
            out.append(label)
        elif isinstance(label, dict) and "name" in label:
            out.append(str(label["name"]))
    return out


def active_reviewer(labels: Sequence[str]) -> str | None:
    """Return the fleet name for the first reviewer:* label, or None."""
    found: list[str] = []
    for label in labels:
        if not label.startswith(REVIEWER_PREFIX):
            continue
        fleet = label[len(REVIEWER_PREFIX) :]
        if fleet in FLEETS:
            found.append(fleet)
    if len(found) > 1:
        raise TriageError(
            f"ambiguous reviewer lease: multiple reviewer labels {found!r}; "
            "only one fleet may hold the lease at a time"
        )
    return found[0] if found else None


def assert_update_allowed(lease: ReviewerLease | None, actor_fleet: str | None) -> None:
    if lease is None:
        return
    if actor_fleet != lease.fleet:
        raise LeaseBlocked(format_blocked(lease))


def format_blocked(lease: ReviewerLease) -> str:
    return (
        f"reviewer-lease: refusing branch update for PR #{lease.pr}\n"
        f"active reviewer: {lease.label}\n"
        f"exact head: {lease.head_sha}\n"
        f"set MDT_REVIEWER_FLEET={lease.fleet} if you hold this lease, or finish handoff first"
    )


def lease_from_pr_view(pr: str, labels: Sequence[Any], head_sha: str) -> ReviewerLease | None:
    fleet = active_reviewer(_label_names(labels))
    if fleet is None:
        return None
    return ReviewerLease(
        pr=str(pr),
        fleet=fleet,
        label=f"{REVIEWER_PREFIX}{fleet}",
        head_sha=head_sha,
    )


# ----- live fetch -----------------------------------------------------------


def fetch_lease(pr: str) -> ReviewerLease | None:
    raw = _gh(["pr", "view", pr, "--json", "labels,number"])
    data = json.loads(raw)
    head = pinned_head(pr)
    return lease_from_pr_view(str(data.get("number", pr)), data.get("labels", []), head)


def fetch_lease_for_branch(branch: str) -> ReviewerLease | None:
    raw = _gh(
        [
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,labels,headRefOid",
        ]
    )
    rows = json.loads(raw)
    if not rows:
        return None
    if len(rows) > 1:
        raise TriageError(
            f"ambiguous branch lease: {len(rows)} open PRs for head branch {branch!r}"
        )
    row = rows[0]
    pr = str(row["number"])
    head = pinned_head(pr)
    return lease_from_pr_view(pr, row.get("labels", []), head)


def _intended_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def actor_fleet(cli_fleet: str | None) -> str | None:
    if cli_fleet:
        if cli_fleet not in FLEETS:
            raise TriageError(f"unknown fleet {cli_fleet!r}; expected one of {sorted(FLEETS)}")
        return cli_fleet
    env = os.environ.get("MDT_REVIEWER_FLEET", "").strip()
    if not env:
        return None
    if env not in FLEETS:
        raise TriageError(f"unknown MDT_REVIEWER_FLEET={env!r}; expected one of {sorted(FLEETS)}")
    return env


# ----- emergency notify -----------------------------------------------------


def _commit_slices(base_sha: str, head_sha: str) -> list[str]:
    proc = subprocess.run(
        ["git", "rev-list", "--reverse", f"{base_sha}..{head_sha}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise TriageError(
            f"git rev-list {base_sha}..{head_sha} failed: {proc.stderr.strip() or '<empty>'}"
        )
    shas = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    lines: list[str] = []
    for sha in shas:
        show = subprocess.run(
            ["git", "show", "-s", "--format=%h %s", sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if show.returncode != 0:
            raise TriageError(f"git show -s {sha} failed: {show.stderr.strip() or '<empty>'}")
        lines.append(show.stdout.strip())
    return lines


def emergency_notify(pr: str, lease: ReviewerLease, intended_head: str) -> None:
    slices = _commit_slices(lease.head_sha, intended_head)
    body_lines = [
        "EMERGENCY reviewer-lease bypass",
        f"active reviewer: {lease.label}",
        f"lease head: {lease.head_sha}",
        f"intended head: {intended_head}",
        "commit slices:",
    ]
    if slices:
        body_lines.extend(f"- {line}" for line in slices)
    else:
        body_lines.append("- (no new commits between lease head and intended head)")
    _gh(["pr", "comment", pr, "--body", "\n".join(body_lines)])


# ----- handoff --------------------------------------------------------------


def handoff(pr: str, fleet: str, comment_file: str | None) -> None:
    if fleet not in FLEETS:
        raise TriageError(f"unknown fleet {fleet!r}; expected one of {sorted(FLEETS)}")
    label = f"{REVIEWER_PREFIX}{fleet}"
    _gh(
        [
            "pr",
            "edit",
            pr,
            "--remove-label",
            label,
            "--add-label",
            HANDOFF_LABEL,
        ]
    )
    after = fetch_lease(pr)
    if after is not None:
        raise TriageError(
            f"handoff left reviewer lease {after.label!r} on PR #{pr}; expected none"
        )
    if comment_file:
        body = open(comment_file, encoding="utf-8").read()
        _gh(["pr", "comment", pr, "--body", body])


# ----- check paths ----------------------------------------------------------


def check_pr(pr: str, fleet: str | None, emergency: bool) -> None:
    lease = fetch_lease(pr)
    if lease is None:
        return
    if emergency and os.environ.get("MDT_REVIEWER_LEASE_EMERGENCY") == "1":
        emergency_notify(pr, lease, _intended_head())
        return
    assert_update_allowed(lease, actor_fleet(fleet))


def check_branch(branch: str, fleet: str | None, emergency: bool) -> None:
    lease = fetch_lease_for_branch(branch)
    if lease is None:
        return
    if emergency and os.environ.get("MDT_REVIEWER_LEASE_EMERGENCY") == "1":
        emergency_notify(lease.pr, lease, _intended_head())
        return
    assert_update_allowed(lease, actor_fleet(fleet))


def check_push(emergency: bool) -> None:
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha == ZERO_SHA:
            continue
        if not local_ref.startswith("refs/heads/"):
            continue
        branch = local_ref.removeprefix("refs/heads/")
        if branch in {"main", "master"}:
            continue
        lease = fetch_lease_for_branch(branch)
        if lease is None:
            continue
        intended_head = local_sha
        if remote_sha != ZERO_SHA and remote_sha != lease.head_sha:
            raise TriageError(
                f"remote tip {remote_sha} disagrees with lease head {lease.head_sha} "
                f"for PR #{lease.pr}; re-fetch and reconcile before push"
            )
        if emergency and os.environ.get("MDT_REVIEWER_LEASE_EMERGENCY") == "1":
            emergency_notify(lease.pr, lease, intended_head)
            continue
        assert_update_allowed(lease, actor_fleet(None))
        after = fetch_lease_for_branch(branch)
        if after is not None and after.head_sha != lease.head_sha:
            raise TriageError(
                f"PR #{lease.pr} head moved from {lease.head_sha} to {after.head_sha} "
                "during reviewer-lease check; re-run after it settles"
            )


# ----- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enforce reviewer label leases before branch updates.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Check lease before updating a PR branch")
    check.add_argument("--pr", required=True)
    check.add_argument("--fleet", choices=sorted(FLEETS))
    check.add_argument("--emergency", action="store_true")

    check_branch_cmd = sub.add_parser("check-branch", help="Check lease for an open PR on a branch")
    check_branch_cmd.add_argument("--branch")
    check_branch_cmd.add_argument("--fleet", choices=sorted(FLEETS))
    check_branch_cmd.add_argument("--emergency", action="store_true")

    sub.add_parser("check-push", help="Git pre-push hook: read ref lines from stdin")

    handoff_cmd = sub.add_parser("handoff", help="Release reviewer lease and mark handoff")
    handoff_cmd.add_argument("--pr", required=True)
    handoff_cmd.add_argument("--fleet", required=True, choices=sorted(FLEETS))
    handoff_cmd.add_argument("--comment-file")

    notify = sub.add_parser("emergency-notify", help="Post emergency slice comment on a PR")
    notify.add_argument("--pr", required=True)
    notify.add_argument("--base", required=True, dest="base_sha")
    notify.add_argument("--head", required=True, dest="head_sha")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check":
            check_pr(args.pr, args.fleet, args.emergency)
        elif args.command == "check-branch":
            branch = args.branch or subprocess.check_output(
                ["git", "branch", "--show-current"], text=True
            ).strip()
            if not branch:
                raise TriageError("could not determine current branch")
            check_branch(branch, args.fleet, args.emergency)
        elif args.command == "check-push":
            check_push(os.environ.get("MDT_REVIEWER_LEASE_EMERGENCY") == "1")
        elif args.command == "handoff":
            handoff(args.pr, args.fleet, args.comment_file)
        elif args.command == "emergency-notify":
            lease = fetch_lease(args.pr)
            if lease is None:
                raise TriageError(f"PR #{args.pr} has no active reviewer lease to notify")
            emergency_notify(args.pr, lease, args.head_sha)
        else:
            raise TriageError(f"unknown command {args.command!r}")
    except LeaseBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TriageError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
