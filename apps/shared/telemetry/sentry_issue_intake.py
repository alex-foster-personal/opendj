"""Classify inbound Sentry issues for autofix automations.

A placeholder probe (TEST-SENTRY / issue 12345 / "Test Sentry issue") is not
a production stack. Inventing a product fix for it is a defect. Sentry API
access still lives outside this repo; this module only answers "may we treat
this ref as a real fault?" from the fields an automation already has.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass

PLACEHOLDER_SHORT_ID_PREFIXES: tuple[str, ...] = ("TEST-",)
PLACEHOLDER_TITLES: frozenset[str] = frozenset({"test sentry issue"})
PLACEHOLDER_PROJECT_SLUGS: frozenset[str] = frozenset({"test-project"})


@dataclass(frozen=True)
class SentryIssueRef:
    issue_id: str
    short_id: str
    title: str
    project_slug: str = ""
    issue_url: str = ""
    exception_type: str = ""
    culprit: str = ""


@dataclass(frozen=True)
class IntakeVerdict:
    actionable: bool
    kind: str
    reason: str


def _norm(value: str) -> str:
    return value.strip()


def is_placeholder_sentry_issue(ref: SentryIssueRef) -> bool:
    """True when the ref is a pipeline probe, not a production fingerprint."""
    short_id = _norm(ref.short_id)
    title = _norm(ref.title).casefold()
    project = _norm(ref.project_slug).casefold()
    if any(short_id.startswith(prefix) for prefix in PLACEHOLDER_SHORT_ID_PREFIXES):
        return True
    if title in PLACEHOLDER_TITLES:
        return True
    return project in PLACEHOLDER_PROJECT_SLUGS


def classify_sentry_issue(ref: SentryIssueRef) -> IntakeVerdict:
    """Return whether an automation may open a product-fix PR for this issue."""
    if is_placeholder_sentry_issue(ref):
        return IntakeVerdict(
            actionable=False,
            kind="placeholder",
            reason="placeholder Sentry probe; do not invent a production stack",
        )
    if not _norm(ref.exception_type) and not _norm(ref.culprit):
        return IntakeVerdict(
            actionable=False,
            kind="missing_stack",
            reason="no exception type or culprit; need Sentry MCP or API before a fix",
        )
    return IntakeVerdict(actionable=True, kind="actionable", reason="has a stack signal")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-id", required=True)
    parser.add_argument("--short-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--project-slug", default="")
    parser.add_argument("--issue-url", default="")
    parser.add_argument("--exception-type", default="")
    parser.add_argument("--culprit", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ref = SentryIssueRef(
        issue_id=args.issue_id,
        short_id=args.short_id,
        title=args.title,
        project_slug=args.project_slug,
        issue_url=args.issue_url,
        exception_type=args.exception_type,
        culprit=args.culprit,
    )
    verdict = classify_sentry_issue(ref)
    json.dump({"ref": asdict(ref), "verdict": asdict(verdict)}, sys.stdout)
    sys.stdout.write("\n")
    return 0 if verdict.actionable else 2


if __name__ == "__main__":
    raise SystemExit(main())
