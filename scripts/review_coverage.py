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
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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

#: The subset of the above that means "this reviewer is DOWN", as opposed to
#: "this reviewer declined or was throttled this once". Only these justify the
#: standing exemption in `KNOWN_UNAVAILABLE_REVIEWERS`, and only when they
#: appear in the CURRENT check's description. See `ReviewerVerdict.outage`.
OUTAGE_MARKERS: tuple[str, ...] = (
    "trial expired",
    "no credits",
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

#: Reviewers KNOWN to be unavailable, with the owner of restoring each. A gate
#: that can never go green blocks all work, so a known-dead reviewer must not
#: fail the run on its own -- but it must stay VISIBLE, because a silent
#: exemption is how "never reviewed" starts reading as "clean" again.
#:
#: The exemption is an INVARIANT, not a value: if a listed reviewer actually
#: reviews, the run FAILS and tells you to delete its entry. So this list
#: cannot quietly outlive the outage it describes, which is the failure mode of
#: every "do not trust X" note that was ever left in a repo.
#: EMPTY, and that is the invariant working rather than the list being
#: forgotten. "Devin Review" sat here from #530 (trial expired, no credits).
#: On #704, Tue 1 Sep 2026, Devin reviewed again -- completed in 1m 9s with 3
#: artifacts including a real inline finding -- so the gate failed exactly as
#: designed, saying the exemption was now hiding a live reviewer, and the entry
#: is deleted per its own remediation. Nothing about the outage is inferred
#: here: the trigger was an artifact on a diff, not a status line.
KNOWN_UNAVAILABLE_REVIEWERS: dict[str, str] = {}


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
    # True only while the reviewer is still working. A reviewer that has not
    # FINISHED has not been shown to be unavailable, so the exemption below
    # must not apply to it: its findings can still arrive, and arriving after
    # a merge is exactly the disposition gap the gate exists to prevent.
    in_progress: bool = False
    # True only when an OUTAGE_MARKERS string appeared in THIS run's check
    # description. Deliberately not set from a historical comment body: an old
    # "trial expired" artifact never leaves the PR, so keying the exemption on
    # bodies makes it permanent and un-retractable. The exemption is a claim
    # about NOW, so it must rest on evidence from now.
    outage: bool = False


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

    # PENDING IS DECIDED FIRST, ahead of the marker scan below, and the order
    # is the whole point. A marker found in a HISTORICAL comment body describes
    # a PAST run; the bucket describes the CURRENT one. Scanning bodies first
    # let an old artifact outvote a live run: Devin rerun on a PR that already
    # carries its own earlier "no credits" comment matched the marker, returned
    # with in_progress False, and was exempted by `partition_verdicts` while the
    # rerun was still producing findings. When the two instruments disagree the
    # current run wins, and it wins fail-closed, because a reviewer that is
    # still working has not been shown to be unavailable.
    if bucket == "pending":
        return ReviewerVerdict(name, False, "still running", in_progress=True)

    # The description describes THIS run, so a marker here is current evidence
    # and is the only thing that may set `outage`.
    lowered = description.lower()
    # `outage` is asked of the WHOLE description, not of whichever marker
    # happened to match first. Devin's live description reads "Full review
    # skipped: trial expired and no credits remaining", where "review skipped"
    # matches ahead of "trial expired"; keying the outage on the first hit made
    # the real outage invisible and un-exempted the one reviewer the exemption
    # exists for. Marker ORDER must not decide a question about CONTENT.
    outage = any(marker in lowered for marker in OUTAGE_MARKERS)
    for marker in NOT_REVIEWED_MARKERS:
        if marker in lowered:
            return ReviewerVerdict(name, False, description or marker, outage=outage)

    # Bodies can carry the signal too -- CodeRabbit reports its rate limit in
    # the comment rather than the description. But a body is not dated to this
    # run, so it can show that a review did not happen and must NOT be allowed
    # to certify an outage that may have ended.
    if evidence is not None:
        for body in evidence.bodies:
            lowered_body = body.lower()
            for marker in NOT_REVIEWED_MARKERS:
                if marker in lowered_body:
                    return ReviewerVerdict(name, False, description or marker)

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


def partition_verdicts(
    verdicts: list[ReviewerVerdict],
    known_unavailable: Mapping[str, str] = MappingProxyType(KNOWN_UNAVAILABLE_REVIEWERS),
) -> tuple[list[ReviewerVerdict], list[ReviewerVerdict], list[ReviewerVerdict]]:
    """Split verdicts into (exempt, blocking, stale-exemption).

    Pure, so the exemption policy can be exercised directly on the domain type
    rather than by standing in front of the GitHub calls. The classifier tests
    above already cover turning real check payloads into verdicts; this covers
    what the tool then DOES with them, which is where the exemption lives.
    """
    # The exemption rests on POSITIVE evidence that this reviewer is down right
    # now -- `v.outage`, set only from the current check's description -- and
    # never on the mere absence of a review. Three ways the weaker form failed:
    # a reviewer still running (`in_progress`) had not been shown unavailable;
    # a `skipping` or `cancel` bucket is a terminal non-review for reasons that
    # have nothing to do with credits; and a restored reviewer still carrying
    # its old outage comment would have stayed exempt forever, since that
    # comment never leaves the PR. Requiring `outage` closes all three, and it
    # is the difference between "I could not find a review" and "I found proof
    # this reviewer is down".
    unavailable = [
        v for v in verdicts
        if not v.reviewed
        and not v.in_progress
        and v.outage
        and v.name in known_unavailable
    ]
    exempt_names = {v.name for v in unavailable}
    unreviewed = [
        v for v in verdicts
        if not v.reviewed and v.name not in exempt_names
    ]
    # A known-dead reviewer that came back to life means the exemption is stale.
    # Failing here is what stops this list outliving the outage it describes.
    revived = [
        v for v in verdicts
        if v.reviewed and v.name in known_unavailable
    ]
    return unavailable, unreviewed, revived


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

    unavailable, unreviewed, revived = partition_verdicts(verdicts)
    print()

    for verdict in unavailable:
        print(
            f"[review-coverage] KNOWN-UNAVAILABLE: {verdict.name} -- "
            f"{KNOWN_UNAVAILABLE_REVIEWERS[verdict.name]}"
        )
    if unavailable:
        print(
            "  Not failing on this, because a gate that can never go green "
            "blocks all work. It is reported every run so it cannot be forgotten."
        )
        print()

    if revived:
        names = ", ".join(v.name for v in revived)
        print(
            f"[review-coverage] FAIL: {names} REVIEWED, but is listed in "
            "KNOWN_UNAVAILABLE_REVIEWERS."
        )
        print(
            "  The exemption is stale and is now hiding a real reviewer. "
            "Delete the entry in scripts/review_coverage.py and re-run."
        )
        return 1

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

    covered = len(verdicts) - len(unavailable)
    print(
        f"[review-coverage] PASS: all {covered} AVAILABLE reviewer(s) reviewed"
        + (f"; {len(unavailable)} known-unavailable." if unavailable else ".")
    )
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
