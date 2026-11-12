"""Enforce branch-update safety before a PR branch moves (issues #272 and #671).

A `reviewer:codex` or `reviewer:claude` label is a short-lived lease while a
fleet actively gates, rebases, or merges. Builders and merge lanes must read
the lease before `git push` or `gh pr update-branch` so non-owners fail fast
with the active reviewer and exact head SHA instead of colliding on
`--force-with-lease` and replaying tests.

Requirements (mini-PRD):
  / Refuse updates when a stacked leaf inherits a human hold from its parent.
    [if] parent has blocked:* [then] exit 1 with the live base [else stop].
  / Fail closed when a stacked leaf's parent or reference frame is unmeasured.
    [if] parent ownership is ambiguous [then] exit 3 and require retry [else stop].
  / Refuse non-owner branch updates when a reviewer lease is active.
    [if] PR has reviewer:codex and actor fleet is not codex [then] exit 1 [else stop].
  / Handoff releases reviewer:* and adds handoff:finished-no-merge.
    [if] handoff completes [then] no reviewer label remains [else stop].
  / Emergency bypass posts explicit commit slices before allowing push.
    [if] emergency runs [then] PR comment lists each new commit [else stop].
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scripts.review_gh import TriageError, pr_edit_labels, pr_list_json, pr_post_comment, pr_view_json
from scripts.review_lane import pinned_head

REVIEWER_PREFIX = "reviewer:"
PARENT_HOLD_LABEL = "needs-human-disposition"
HANDOFF_LABEL = "handoff:finished-no-merge"
FLEETS = frozenset({"codex", "claude"})
ZERO_SHA = "0" * 40


class LeaseBlocked(RuntimeError):
    """A non-owner attempted to update a branch under an active reviewer lease."""


class InheritedHoldBlocked(RuntimeError):
    """A leaf attempted to move while its parent PR carried a human hold."""

    def __init__(self, hold: ParentHoldResult) -> None:
        self.hold = hold
        super().__init__(format_parent_hold(hold))

    @classmethod
    def from_snapshots(cls, leaf: PRSafetySnapshot, parent: PRSafetySnapshot) -> InheritedHoldBlocked:
        return cls(_parent_result(leaf, parent, blocking_parent_labels(parent.labels)))


@dataclass(frozen=True)
class ReviewerLease:
    pr: str
    fleet: str
    label: str
    head_sha: str


@dataclass(frozen=True)
class PRSafetySnapshot:
    pr: str
    head_branch: str
    head_sha: str
    base_branch: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ParentHoldResult:
    leaf_pr: str
    leaf_head: str
    leaf_head_sha: str
    base_branch: str
    parent_pr: str | None
    parent_head: str | None
    parent_head_sha: str | None
    inherited_labels: tuple[str, ...]


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
            f"ambiguous reviewer lease: multiple reviewer labels {found!r}; only one fleet may hold the lease at a time"
        )
    return found[0] if found else None


def blocking_parent_labels(labels: Sequence[str]) -> tuple[str, ...]:
    """Return the labels that make a stacked leaf inherit its parent's hold."""
    return tuple(sorted(label for label in labels if label.startswith("blocked:") or label == PARENT_HOLD_LABEL))


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


def _required_snapshot_value(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise TriageError(f"GitHub PR data has no valid {field}: {value!r}")
    return str(value).strip()


def _snapshot_from_data(data: dict[str, Any]) -> PRSafetySnapshot:
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise TriageError(f"GitHub PR data has no valid labels list: {labels!r}")
    normalized_labels: list[str] = []
    for label in labels:
        name = label if isinstance(label, str) else label.get("name") if isinstance(label, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise TriageError(f"GitHub PR data has a malformed label: {label!r}")
        normalized_labels.append(name.strip())
    return PRSafetySnapshot(
        pr=_required_snapshot_value(data, "number"),
        head_branch=_required_snapshot_value(data, "headRefName"),
        head_sha=_required_snapshot_value(data, "headRefOid"),
        base_branch=_required_snapshot_value(data, "baseRefName"),
        labels=tuple(sorted(set(normalized_labels))),
    )


def _snapshot_fields() -> tuple[str, ...]:
    return ("number", "headRefName", "headRefOid", "baseRefName", "labels")


def _assert_same_snapshot(
    before: PRSafetySnapshot,
    after: PRSafetySnapshot,
    *,
    role: str,
) -> None:
    if before != after:
        raise TriageError(
            f"{role} safety state changed during parent-hold measurement; retry after it settles: "
            f"before={before!r}, after={after!r}"
        )


def _parent_result(
    leaf: PRSafetySnapshot,
    parent: PRSafetySnapshot | None,
    inherited_labels: tuple[str, ...],
) -> ParentHoldResult:
    return ParentHoldResult(
        leaf_pr=leaf.pr,
        leaf_head=leaf.head_branch,
        leaf_head_sha=leaf.head_sha,
        base_branch=leaf.base_branch,
        parent_pr=parent.pr if parent else None,
        parent_head=parent.head_branch if parent else None,
        parent_head_sha=parent.head_sha if parent else None,
        inherited_labels=inherited_labels,
    )


def format_parent_hold(hold: ParentHoldResult) -> str:
    labels = ",".join(hold.inherited_labels) or "none"
    parent = f"#{hold.parent_pr}" if hold.parent_pr else "none"
    parent_head = hold.parent_head or "none"
    parent_head_sha = hold.parent_head_sha or "none"
    outcome = "blocked" if hold.inherited_labels else "clear"
    return (
        f"branch-safety: leaf=#{hold.leaf_pr} leaf-head={hold.leaf_head} "
        f"leaf-sha={hold.leaf_head_sha} base={hold.base_branch} parent={parent} "
        f"parent-head={parent_head} parent-sha={parent_head_sha} "
        f"inherited-holds={labels} outcome={outcome}"
    )


# ----- live fetch -----------------------------------------------------------


def fetch_snapshot(pr: str) -> PRSafetySnapshot:
    return _snapshot_from_data(pr_view_json(pr, _snapshot_fields()))


def fetch_snapshot_for_branch(branch: str) -> PRSafetySnapshot | None:
    rows = pr_list_json(head=branch, state="open", fields=_snapshot_fields())
    if not rows:
        return None
    if len(rows) > 1:
        raise TriageError(f"ambiguous branch safety state: {len(rows)} open PRs for head branch {branch!r}")
    return _snapshot_from_data(rows[0])


def _fetch_parent_snapshot(leaf: PRSafetySnapshot) -> PRSafetySnapshot:
    rows = pr_list_json(head=leaf.base_branch, state="open", fields=_snapshot_fields())
    if len(rows) != 1:
        raise TriageError(
            f"base {leaf.base_branch!r} for leaf PR #{leaf.pr} must have exactly one open parent PR; found {len(rows)}"
        )
    parent = _snapshot_from_data(rows[0])
    if parent.head_branch != leaf.base_branch:
        raise TriageError(
            f"parent PR #{parent.pr} reports head {parent.head_branch!r}, "
            f"not leaf PR #{leaf.pr}'s base {leaf.base_branch!r}"
        )
    return parent


def evaluate_parent_hold(leaf: PRSafetySnapshot) -> ParentHoldResult:
    """Measure and enforce the hold inherited through the leaf's live base."""
    if leaf.base_branch in {"main", "master"}:
        _assert_same_snapshot(leaf, fetch_snapshot(leaf.pr), role="leaf")
        return _parent_result(leaf, None, ())

    parent = _fetch_parent_snapshot(leaf)
    _assert_same_snapshot(leaf, fetch_snapshot(leaf.pr), role="leaf")

    inherited = blocking_parent_labels(parent.labels)
    result = _parent_result(leaf, parent, inherited)
    if inherited:
        raise InheritedHoldBlocked(result)

    _assert_same_snapshot(parent, _fetch_parent_snapshot(leaf), role="parent")
    return result


def _lease_for_snapshot(snapshot: PRSafetySnapshot) -> ReviewerLease | None:
    pinned = pinned_head(snapshot.pr)
    if pinned != snapshot.head_sha:
        raise TriageError(
            f"leaf PR #{snapshot.pr} head moved from {snapshot.head_sha} to {pinned} "
            "during branch-safety check; retry after it settles"
        )
    return lease_from_pr_view(snapshot.pr, snapshot.labels, pinned)


def fetch_lease(pr: str) -> ReviewerLease | None:
    data = pr_view_json(pr, ("labels", "number"))
    head = pinned_head(pr)
    return lease_from_pr_view(str(data.get("number", pr)), data.get("labels", []), head)


def fetch_lease_for_branch(branch: str) -> ReviewerLease | None:
    rows = pr_list_json(head=branch, state="open", fields=("number", "labels", "headRefOid"))
    if not rows:
        return None
    if len(rows) > 1:
        raise TriageError(f"ambiguous branch lease: {len(rows)} open PRs for head branch {branch!r}")
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
        raise TriageError(f"git rev-list {base_sha}..{head_sha} failed: {proc.stderr.strip() or '<empty>'}")
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
    pr_post_comment(pr, "\n".join(body_lines))


# ----- handoff --------------------------------------------------------------


def handoff(pr: str, fleet: str, comment_file: str | None) -> None:
    if fleet not in FLEETS:
        raise TriageError(f"unknown fleet {fleet!r}; expected one of {sorted(FLEETS)}")
    label = f"{REVIEWER_PREFIX}{fleet}"
    pr_edit_labels(pr, remove=(label,), add=(HANDOFF_LABEL,))
    after = fetch_lease(pr)
    if after is not None:
        raise TriageError(f"handoff left reviewer lease {after.label!r} on PR #{pr}; expected none")
    if comment_file:
        body = open(comment_file, encoding="utf-8").read()
        pr_post_comment(pr, body)


# ----- check paths ----------------------------------------------------------


def check_pr(pr: str, fleet: str | None, emergency: bool) -> None:
    snapshot = fetch_snapshot(pr)
    print(format_parent_hold(evaluate_parent_hold(snapshot)))
    lease = _lease_for_snapshot(snapshot)
    if lease is None:
        return
    if emergency and os.environ.get("MDT_REVIEWER_LEASE_EMERGENCY") == "1":
        emergency_notify(pr, lease, _intended_head())
        return
    assert_update_allowed(lease, actor_fleet(fleet))


def check_branch(branch: str, fleet: str | None, emergency: bool) -> None:
    snapshot = fetch_snapshot_for_branch(branch)
    if snapshot is None:
        return
    print(format_parent_hold(evaluate_parent_hold(snapshot)))
    lease = _lease_for_snapshot(snapshot)
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
        snapshot = fetch_snapshot_for_branch(branch)
        if snapshot is None:
            continue
        print(format_parent_hold(evaluate_parent_hold(snapshot)))
        lease = _lease_for_snapshot(snapshot)
        intended_head = local_sha
        if remote_sha not in (ZERO_SHA, snapshot.head_sha):
            raise TriageError(
                f"remote tip {remote_sha} disagrees with measured head {snapshot.head_sha} "
                f"for PR #{snapshot.pr}; re-fetch and reconcile before push"
            )
        if lease is not None and emergency and os.environ.get("MDT_REVIEWER_LEASE_EMERGENCY") == "1":
            emergency_notify(lease.pr, lease, intended_head)
            continue
        assert_update_allowed(lease, actor_fleet(None))
        after = fetch_snapshot_for_branch(branch)
        if after is None:
            raise TriageError(f"PR #{snapshot.pr} disappeared during branch-safety check; retry")
        _assert_same_snapshot(snapshot, after, role="leaf")


# ----- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enforce reviewer leases and inherited holds before updates.")
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
            branch = args.branch or subprocess.check_output(["git", "branch", "--show-current"], text=True).strip()
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
    except (InheritedHoldBlocked, LeaseBlocked) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TriageError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
