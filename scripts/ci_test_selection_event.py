"""Event classification, kill switches, and TEST-CUT policy for PR test selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_test_selection_rules import FullSuite, Policy


SWITCH_VARIABLE = "CI_PR_TEST_SELECTION"
QUEUE_SWITCH_VARIABLE = "CI_QUEUE_TEST_SELECTION"
MAIN_SWITCH_VARIABLE = "CI_MAIN_PUSH_TEST_SELECTION"
MAIN_SWITCH_OFF = "full"
MAIN_RANGE_VARIABLE = "CI_MAIN_PUSH_RANGE"
MAIN_REF = "refs/heads/main"
BUNDLE_VARIABLE = "CI_BUNDLE_DECLARATIONS"
SWITCH_ON = "affected"
#: A Trunk queue draft is classified once, here, and the bundle collector imports it (Sol,
#: PR #7598): the head ref is `trunk-merge/pr-N/...` or `trunk-temp/pr-N/...`, the author is
#: the Trunk App, and the head lives in this repository. A branch name alone is
#: caller-controlled, so a human's `trunk-merge/x` branch is an ordinary PR head.
QUEUE_DRAFT_HEAD = re.compile(r"^trunk-(?:merge|temp)/pr-\d+/")
TRUNK_LOGIN = "trunk-io[bot]"
BY_NAME_VARIABLE = "CI_PR_SELECT_BY_NAME"
BY_NAME_ON = "ci-config"
DEMOTE_VARIABLE = "CI_PR_DEMOTE_WEAK_LINKS"
#: Schema 2 (Tue 27 Oct 2026) adds `queue_switch`, `declared_modules`, `universe_modules` and
#: `bundle_prs`. Schema 3 (Fri 30 Oct 2026, TEST-BUDGET) adds `budget`: the runner-minute
#: budget the run was fitted to, the planned minutes before and after, and the weak-link
#: modules it dropped (scripts/ci_test_budget.py); None when `select` ran without `--budget`.
#: Schema 4 adds `policy` and `demoted` (TEST-CUT items 2 and 3, (private path)).
#: Schema 5 (Tue 3 Nov 2026) adds `main_push_base`: the commit a main push diffed against.
SCHEMA = 5
RECORD_NAME = "selection.json"
SHARD_MODULES_NAME = "shard-modules.txt"


# ---------------------------------------------------------------------------
# event classification and the kill switch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    name: str
    head_ref: str
    head_sha: str
    labels: tuple[str, ...]
    pr_number: int | None
    #: The PR body at the triggering event; a queue draft's is Trunk's banner.
    body: str = ""
    #: True only for an authenticated Trunk queue draft (`is_queue_draft`).
    queue_draft: bool = False

    @property
    def main_push(self) -> bool:
        """A push to main: `head_ref` is the pushed ref and `head_sha` the pushed commit."""
        return self.name == "push" and self.head_ref == MAIN_REF

    @classmethod
    def from_payload(cls, name: str, payload: dict, repository: str) -> Event:
        """`repository` is GITHUB_REPOSITORY, which authenticates a queue draft's head.

        A push payload has no `pull_request`: its `ref` and `after` are the head.
        """
        pull_request = payload.get("pull_request") or {}
        head = pull_request.get("head") or {}
        pushed = name == "push" and not pull_request
        return cls(
            name=name,
            head_ref=str((payload.get("ref") if pushed else head.get("ref")) or ""),
            head_sha=str((payload.get("after") if pushed else head.get("sha")) or ""),
            labels=tuple(str(label["name"]) for label in pull_request.get("labels") or ()),
            pr_number=pull_request.get("number"),
            body=str(pull_request.get("body") or ""),
            queue_draft=is_queue_draft(payload, repository),
        )


def is_queue_draft(payload: dict, repository: str) -> bool:
    """True for a Trunk queue draft: its head ref, its author and its head repository agree."""
    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") or {}
    return (
        bool(repository)
        and QUEUE_DRAFT_HEAD.match(str(head.get("ref") or "")) is not None
        and (pull_request.get("user") or {}).get("login") == TRUNK_LOGIN
        and (head.get("repo") or {}).get("full_name") == repository
    )


def full_suite_reason(
    switch: str | None, event: Event, queue_switch: str | None = None, main_switch: str | None = None
) -> str | None:
    """Why this run takes the full suite, or None when it is a PR head or a main push that selects.

    `queue_switch` is `CI_QUEUE_TEST_SELECTION`; None is the variable unset, which keeps a
    queue draft on the full suite. `main_switch` is `CI_MAIN_PUSH_TEST_SELECTION`: `full` keeps
    a main push on the full suite, and any value but unset, empty or `affected` does too, named.
    """
    if event.main_push:
        if (main_switch or "").strip() not in ("", SWITCH_ON):
            return (
                f"repo variable {MAIN_SWITCH_VARIABLE}={main_switch!r} keeps main pushes on the full "
                f"suite; it selects only when unset or {SWITCH_ON!r}"
            )
    elif event.name != "pull_request":
        return f"event `{event.name}` is not a pull request head or a push to main; only those select"
    if event.queue_draft and queue_switch != SWITCH_ON:
        return (
            f"head `{event.head_ref}` is a Trunk merge-queue draft; the queue runs everything "
            f"while repo variable {QUEUE_SWITCH_VARIABLE}={queue_switch!r} is not {SWITCH_ON!r}"
        )
    if switch != SWITCH_ON:
        return f"repo variable {SWITCH_VARIABLE}={switch!r}; selection runs only when it equals {SWITCH_ON!r}"
    return None


def policy_from(by_name: str | None, demote: str | None, root: Path) -> Policy:
    """The TEST-CUT policy the two repo variables ask for. Unset or empty is off.

    Raises FullSuite naming the variable on any value it does not understand.
    """
    by_name = (by_name or "").strip()
    if by_name not in ("", BY_NAME_ON):
        raise FullSuite(f"repo variable {BY_NAME_VARIABLE}={by_name!r} is neither unset nor {BY_NAME_ON!r}")
    directories = tuple(entry.strip().rstrip("/") for entry in (demote or "").split(",") if entry.strip())
    if (demote or "").strip() and not directories:
        raise FullSuite(f"repo variable {DEMOTE_VARIABLE}={demote!r} names no directory")
    for directory in directories:
        under_tests = directory == "tests" or directory.startswith("tests/")
        # `_under` compares strings, so `tests/./scripts` would match nothing yet read as set.
        canonical = all(part not in ("", ".", "..") for part in directory.split("/"))
        if not under_tests or not canonical or not (root / directory).is_dir():
            raise FullSuite(
                f"repo variable {DEMOTE_VARIABLE} names `{directory}`, which is not a directory under tests/"
            )
    return Policy(ci_config_by_name=by_name == BY_NAME_ON, demote_weak_under=directories)
