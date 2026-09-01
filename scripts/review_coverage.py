"""Reviewer COVERAGE preflight: did the bot reviewers actually review this PR?

Companion to ``review_thread_triage``, which is a silence detector: it asks
whether every bot review THREAD reached a disposition. That is the right
question, and it has a precondition nobody was checking. On a PR where no
reviewer ever ran there are ZERO threads, so "every thread reached a terminal
state" is vacuously true and the gate reports OK. Observed live on #682
(Tue 1 Sep 2026): Devin left nothing at all, and the gate printed
"bot review threads: 0 ... OK" and exited 0.

So this module answers the prior question -- did anyone LOOK -- and
``review_thread_triage`` answers what happened to what they found. Both run
under ``just review-triage <PR>``.

THE INSTRUMENT MATTERS MORE THAN THE VERDICT HERE. A reviewer's status check is
the weaker of two available signals and it can read like success while nothing
happened: #682 carried CodeRabbit "Review completed" alongside a real review,
while Devin carried "pass" with the description "Full review skipped: trial
expired and no credits remaining". A status-only probe passes the second. So
coverage requires an ARTIFACT -- a submitted review, an inline comment, or a
review summary comment -- and separately scans those artifact bodies for
not-reviewed markers, because CodeRabbit reports its rate limit in the COMMENT
rather than in the check description.

Current live state, both directions verified Tue 1 Sep 2026:
  #682  CodeRabbit reviewed for real (1 artifact); Devin MISS, trial expired.
  #528  CodeRabbit MISS, rate limited; Devin reviewed for real (5 artifacts).
Opposite verdicts on the same two reviewers is the control: a classifier stuck
at either answer cannot produce it.

Requirements (mini-PRD):
  / A reviewer whose check is absent, skipped, rate limited or credit-blocked
    reports NOT REVIEWED and fails the run.
    [if] a skipped reviewer counts toward clean coverage [then broken]
  / A reviewer reporting success while leaving NO artifact reports NOT REVIEWED.
    [if] "Review completed" with zero artifacts passes [then broken]
  / A reviewer that genuinely reviewed passes.
    [if] a real review cannot pass [then broken] -- without this an
    always-MISS classifier satisfies every other row here
  / A failure to MEASURE (bad PR number, gh error) exits 3, never a verdict.
    [if] an API error prints a clean board [then broken]
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

REPO = "maintainer/music-dj-tools"

#: Reviewers the merge gate expects. A name absent from a PR's checks is NOT a
#: pass; it is an unmeasured reviewer, which is the whole point of this tool.
EXPECTED_REVIEWERS: tuple[str, ...] = ("CodeRabbit", "Devin Review")

#: Substrings that mean the check reported success WITHOUT reviewing. Matched
#: case-insensitively against the check's description.
NOT_REVIEWED_MARKERS: tuple[str, ...] = (
    "rate limited",
    "review skipped",
    "trial expired",
    "no credits",
    "skipped:",
)

#: The bot login each expected reviewer posts under. A reviewer's STATUS is not
#: evidence it reviewed; #682 carried CodeRabbit "Review completed" with zero
#: submitted reviews and zero inline comments (Tue 1 Sep 2026). Only an ARTIFACT
#: -- a submitted review, an inline comment, or a review summary comment -- shows
#: that something actually looked at the diff.
REVIEWER_LOGINS: dict[str, tuple[str, ...]] = {
    "CodeRabbit": ("coderabbitai",),
    "Devin Review": ("devin-ai-integration",),
}


class TriageError(RuntimeError):
    """Measurement failed. Never rendered as a verdict."""


# ----- gh plumbing --------------------------------------------------------


def _gh(args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise TriageError(
            f"gh {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return proc.stdout


def _checks(pr: str) -> list[dict[str, str]]:
    raw = _gh(["pr", "checks", pr, "--json", "name,bucket,state,description"])
    if not raw.strip():
        raise TriageError(f"gh returned an empty check list for PR {pr}")
    return json.loads(raw)


# ----- review artifacts ---------------------------------------------------


@dataclass(frozen=True)
class ReviewerEvidence:
    """What this reviewer actually LEFT on the PR, as opposed to reported."""

    submitted_reviews: int
    inline_comments: int
    bodies: tuple[str, ...]

    @property
    def artifact_count(self) -> int:
        return self.submitted_reviews + self.inline_comments + len(self.bodies)


def _matches(login: str, name: str) -> bool:
    return any(stem in login.lower() for stem in REVIEWER_LOGINS.get(name, ()))


def _evidence(pr: str) -> dict[str, ReviewerEvidence]:
    """Collect per-reviewer artifacts from the three places bots post."""
    reviews = json.loads(_gh(["api", f"repos/{REPO}/pulls/{pr}/reviews"]))
    inline = json.loads(_gh(["api", f"repos/{REPO}/pulls/{pr}/comments"]))
    issue = json.loads(_gh(["api", f"repos/{REPO}/issues/{pr}/comments"]))

    collected: dict[str, ReviewerEvidence] = {}
    for name in EXPECTED_REVIEWERS:
        bodies: list[str] = []
        submitted = 0
        for review in reviews:
            if _matches((review.get("user") or {}).get("login", ""), name):
                submitted += 1
                if review.get("body"):
                    bodies.append(review["body"])
        comments = sum(
            1 for c in inline if _matches((c.get("user") or {}).get("login", ""), name)
        )
        for comment in issue:
            if _matches((comment.get("user") or {}).get("login", ""), name):
                bodies.append(comment.get("body") or "")
        collected[name] = ReviewerEvidence(submitted, comments, tuple(bodies))
    return collected


# ----- classification -----------------------------------------------------


@dataclass(frozen=True)
class ReviewerVerdict:
    name: str
    reviewed: bool
    reason: str


def classify_reviewer(
    name: str,
    checks: list[dict[str, str]],
    evidence: ReviewerEvidence | None = None,
) -> ReviewerVerdict:
    """Did this reviewer actually review, or merely report success?

    Two independent instruments, and the STATUS is the weaker one. A status can
    say "Review completed" while the reviewer left nothing at all on the PR,
    observed live on #682. So a clean status is necessary and not sufficient:
    an ARTIFACT must exist too. Pass ``evidence=None`` only when artifacts were
    deliberately not collected, e.g. a unit test of the status layer alone.
    """
    match = next((c for c in checks if c.get("name") == name), None)
    if match is None:
        return ReviewerVerdict(name, False, "no check reported on this PR")

    description = (match.get("description") or "").strip()
    bucket = match.get("bucket", "")

    # Markers can appear in the status OR in the artifact body. CodeRabbit
    # reports its rate limit in the comment, not the check description.
    haystacks = [description.lower()]
    if evidence is not None:
        haystacks.extend(body.lower() for body in evidence.bodies)
    for haystack in haystacks:
        for marker in NOT_REVIEWED_MARKERS:
            if marker in haystack:
                return ReviewerVerdict(name, False, description or marker)

    if bucket == "pending":
        return ReviewerVerdict(name, False, "still running")
    if bucket in {"skipping", "cancel"}:
        return ReviewerVerdict(name, False, f"check {bucket}")

    if evidence is not None and evidence.artifact_count == 0:
        return ReviewerVerdict(
            name,
            False,
            f"reported '{description or bucket}' but left NO review artifact "
            "(0 submitted reviews, 0 inline comments, 0 summary comments)",
        )

    return ReviewerVerdict(name, True, description or f"bucket={bucket}")


# ----- report -------------------------------------------------------------


def triage(pr: str) -> int:
    checks = _checks(pr)
    evidence = _evidence(pr)
    verdicts = [
        classify_reviewer(name, checks, evidence.get(name))
        for name in EXPECTED_REVIEWERS
    ]

    print(f"[review-coverage] PR #{pr}")
    print()
    print("  reviewer coverage")
    for verdict in verdicts:
        mark = "ok  " if verdict.reviewed else "MISS"
        found = evidence.get(verdict.name)
        artifacts = f" [{found.artifact_count} artifact(s)]" if found else ""
        print(f"    {mark} {verdict.name}: {verdict.reason}{artifacts}")

    unreviewed = [v for v in verdicts if not v.reviewed]
    print()

    if unreviewed:
        print(
            "[review-coverage] FAIL: "
            f"{len(unreviewed)} of {len(verdicts)} expected reviewers did not review."
        )
        print(
            "  A reviewer that did not run produces no findings, and no findings "
            "is not the same as no problems."
        )
        print(
            "  This is NOT satisfied by re-running. Either restore the reviewer, "
            "or amend EXPECTED_REVIEWERS and the CLAUDE.md clause together so the "
            "gate states what it actually checks."
        )
        return 1

    print(f"[review-coverage] PASS: all {len(verdicts)} expected reviewer(s) reviewed.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scripts.review_coverage <PR-NUMBER>", file=sys.stderr)
        return 2
    try:
        return triage(argv[1])
    except TriageError as exc:
        # A measurement failure is never a verdict. Distinct exit code so a
        # caller can tell "could not measure" from "measured and found bad".
        print(f"[review-coverage] COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
