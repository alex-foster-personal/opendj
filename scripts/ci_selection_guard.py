"""Fail a PR or merge-lane run whose test selection is the whole suite, or too wide, without a written reason.

Bundle merges (the maintainer, Tue 27 Oct 2026): merges land as bundles of 20 to 50 PRs, and agents
own the judgment of which tests run, the minimal required set, named in the PR. No PR,
bundle or merge lane runs the whole suite or "most" of it without a written reason; the
full suite belongs to the nightly run (full-ci.yml). Rule: (private path).
Decision record: (private path).

Input is the record `python -m scripts.ci_test_selection select` writes (selection.json)
and the GitHub event payload (the PR body and labels). Verdicts, one line on stdout:

    PASS     the selection is at most --max-share of the lane's modules, or it is wider
             and the run carries a written reason.
    FAIL     the selection is the full suite or wider than --max-share, and nothing
             written says why.
    UNKNOWN  the record cannot be read, or an affected record does not say how big the
             lane is, so its share cannot be measured. Never a pass.

A written reason is a `CI-Wide-Reason: <text>` line in the PR body (at least
MIN_REASON_CHARS characters of text), or the `ci:trunk-repair` label, which is itself the
operator's written decision to run everything. A FULL record's own machine reason ("global
path changed: pyproject.toml") says what forced it, not why the author accepts it, so it
is printed but never counts. A FULL run because a selection switch is off
(CI_PR_TEST_SELECTION, or CI_QUEUE_TEST_SELECTION for a queue draft) passes: the repo
variable is an admin's standing written decision, and failing it would block every PR, or
every bundle, the moment the guard is enforced.

A Trunk queue draft has no author body (Sol, PR #7598, P1), so it is judged on the bundle
record scripts/ci_bundle_declarations.py collects from the PRs it carries: a FULL bundle
passes when one of those PRs wrote a reason, which the PR that forced it already had to do
at its own head; a wide affected bundle of two or more PRs passes, because it is the union
of selections each guarded at its own head; an unreadable bundle record is UNKNOWN.

Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN.

Usage (the `scope` job in .github/workflows/ci.yml):
    python3 -m scripts.ci_selection_guard check --record DIR/selection.json \\
        --event "$GITHUB_EVENT_PATH" --bundle "$CI_BUNDLE_DECLARATIONS" --max-share 0.50

Standard library only (the selector module it imports is too), so the scope job runs it with
the runner image's python3.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci_test_selection import bundle_record_problem

REPAIR_LABEL = "ci:trunk-repair"
SWITCH_ON = "affected"
MIN_REASON_CHARS = 20
_WIDE_REASON = re.compile(r"^[ \t]*CI-Wide-Reason:[ \t]*(?P<text>.*\S)?[ \t]*$", re.M)

EXIT = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}


@dataclass(frozen=True)
class Verdict:
    status: str
    lines: list[str]

    @property
    def exit_code(self) -> int:
        return EXIT[self.status]


# ----- reading the inputs ------------------------------------------------------


def written_reason(body: str) -> str | None:
    """The first `CI-Wide-Reason:` text long enough to be a reason, else None.

    CRLF is normalized first: a body edited in GitHub's web UI ends lines with `\\r\\n`, and
    `$` under re.M stops only before `\\n` (Sentry on PR #7598).
    """
    for match in _WIDE_REASON.finditer(body.replace("\r\n", "\n").replace("\r", "\n")):
        text = (match["text"] or "").strip()
        if len(text) >= MIN_REASON_CHARS:
            return text
    return None


def _pull_request(event: dict) -> dict:
    return event.get("pull_request") or {}


def _labels(event: dict) -> list[str]:
    return [str(label["name"]) for label in _pull_request(event).get("labels") or ()]


# ----- the verdict -------------------------------------------------------------


def _switched_off(record: dict) -> str | None:
    """The repo variable that kept this run on the full suite, when that is why it is FULL.

    A record with no `switch` field cannot claim this: an absent field is not an admin decision.
    """
    if "switch" not in record:
        return None
    if record["switch"] != SWITCH_ON:
        return f"CI_PR_TEST_SELECTION={record.get('switch')!r}"
    if record.get("queue_draft") is True and record.get("queue_switch") != SWITCH_ON:
        return f"CI_QUEUE_TEST_SELECTION={record.get('queue_switch')!r}"
    return None


def _judge_bundle(head: str, mode: str, bundle: Any, record: dict) -> Verdict:
    """A wide or FULL queue draft, judged on the PRs it carries (it has no body of its own)."""
    prs, reasons = bundle["prs"], bundle["reasons"]
    if reasons:
        cited = "; ".join(f"#{pr}: {text}" for pr, text in reasons.items())
        return Verdict("PASS", [head, f"bundle of {len(prs)} PR(s), written reason(s): {cited}"])
    if mode == "affected" and len(prs) >= 2:
        return Verdict(
            "PASS", [head, f"bundle of {len(prs)} PRs: the union of selections each guarded at its own head"]
        )
    return Verdict(
        "FAIL",
        [head, f"bundle of {len(prs)} PR(s) ({record.get('reason', '')}), and none wrote a `CI-Wide-Reason:` line"],
    )


def _draft_bundle_problem(bundle: object) -> str | None:
    """Why a queue draft's bundle record cannot be judged: malformed, disowning or `unknown`."""
    problem = bundle_record_problem(bundle)
    if problem or not isinstance(bundle, dict):
        return problem or "not a bundle record"
    if not bundle["bundle"]:
        return "the record is a queue draft but the bundle record says it is not"
    if bundle.get("unknown"):
        return str(bundle["unknown"])
    return None


def _record_problem(record: dict, bundle: object) -> Verdict | None:
    """UNKNOWN for an unmeasurable record or bundle, FAIL for an invalid declaration.

    A queue draft's bundle record is checked here, before any share or switch exemption
    (Sol, PR #7598): an absent, malformed or `unknown` one is UNKNOWN.
    """
    if record.get("queue_draft") is True:
        problem = _draft_bundle_problem(bundle)
        if problem:
            return Verdict("UNKNOWN", [f"the queue draft's bundle record is unreadable: {problem}"])
    elif bundle is not None and bundle_record_problem(bundle):
        return Verdict("UNKNOWN", [f"the bundle record is unreadable: {bundle_record_problem(bundle)}"])
    modules = record.get("selected_modules")
    if not isinstance(modules, list) or not all(isinstance(module, str) for module in modules):
        return Verdict("UNKNOWN", [f"selected_modules is {type(modules).__name__}, not a list of module paths"])
    if record.get("declaration_error"):
        detail = f"invalid declaration: {record['declaration_error']}"
        return Verdict("FAIL", [f"{detail}; a written reason cannot excuse a test that never runs"])
    return None


def judge(record: dict, event: dict, max_share: float, bundle: object = None) -> Verdict:
    """PASS, FAIL or UNKNOWN for one selection record.

    `bundle` is the scripts.ci_bundle_declarations record; None or `{"bundle": false}` is a PR head.
    """
    problem = _record_problem(record, bundle)
    if problem:
        return problem
    mode = record.get("mode")
    selected = len(record["selected_modules"])
    universe = record.get("universe_modules")
    if mode == "full":
        share = 1.0
        size = f"the FULL suite ({record.get('reason', 'no reason recorded')})"
    elif mode == "affected":
        if not isinstance(universe, int) or isinstance(universe, bool) or universe <= 0:
            return Verdict(
                "UNKNOWN",
                [f"an affected record with universe_modules={universe!r}: the share cannot be measured"],
            )
        share = selected / universe
        size = f"{selected} of {universe} lane modules ({share:.0%})"
    else:
        return Verdict("UNKNOWN", [f"record mode {mode!r} is not `full` or `affected`"])

    head = f"selection: {size}; limit {max_share:.0%} without a written reason"
    if share <= max_share:
        return Verdict("PASS", [head, "within the limit"])
    switched_off = _switched_off(record) if mode == "full" else None
    if switched_off:
        return Verdict(
            "PASS", [head, f"selection is switched off by repo variable {switched_off}, an admin's decision"]
        )
    if record.get("queue_draft") is True:
        return _judge_bundle(head, mode, bundle, record)
    return _judge_pr_head(head, event)


def _judge_pr_head(head: str, event: dict) -> Verdict:
    """A wide or FULL PR head passes only on its own body's reason or the repair label."""
    reason = written_reason(str(_pull_request(event).get("body") or ""))
    if reason:
        return Verdict("PASS", [head, f"wide, with a written reason: CI-Wide-Reason: {reason}"])
    if REPAIR_LABEL in _labels(event):
        return Verdict("PASS", [head, f"wide, with the `{REPAIR_LABEL}` label as the written reason"])
    return Verdict(
        "FAIL",
        [
            head,
            "no written reason: add a `CI-Wide-Reason: <why this change needs this many tests>` "
            f"line (at least {MIN_REASON_CHARS} characters) to the PR body and push, or narrow "
            "the change; see (private path)",
        ],
    )


# ----- CLI ---------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"{path} holds {type(loaded).__name__}, not a JSON object")
    return loaded


def _check(arguments: argparse.Namespace) -> int:
    try:
        record = _load_json(Path(arguments.record))
        event = _load_json(Path(arguments.event))
        bundle = json.loads(arguments.bundle) if arguments.bundle else None
    except (OSError, ValueError, TypeError) as error:
        verdict = Verdict("UNKNOWN", [f"cannot read the inputs: {error}"])
    else:
        verdict = judge(record, event, arguments.max_share, bundle)
    for line in verdict.lines:
        print(f"[selection-guard] {line}", file=sys.stderr)
    if verdict.status != "PASS":
        print(f"::error title=test selection guard {verdict.status}::{verdict.lines[-1]}", file=sys.stderr)
    print(verdict.status)
    return verdict.exit_code


def _share(text: str) -> float:
    value = float(text)
    if not 0.0 < value <= 1.0:
        raise argparse.ArgumentTypeError(f"--max-share {text} is outside (0, 1]")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="judge one selection record")
    check.add_argument("--record", required=True, help="selection.json from ci_test_selection select")
    check.add_argument("--event", required=True, help="the GitHub event payload (GITHUB_EVENT_PATH)")
    check.add_argument(
        "--bundle",
        required=True,
        help="CI_BUNDLE_DECLARATIONS, the scope job's bundle record; empty when the collector did not run",
    )
    check.add_argument("--max-share", type=_share, required=True, help="widest share allowed without a reason")
    arguments = parser.parse_args(argv)
    return _check(arguments)


if __name__ == "__main__":
    sys.exit(main())
