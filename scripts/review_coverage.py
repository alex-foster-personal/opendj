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

Policy change, issue #1016 (Thu 3 Sep 2026): CodeRabbit is rate-limited on
every PR and Devin's trial expired at #530, so both are gone from
EXPECTED_REVIEWERS -- see the same issue's `.coderabbit.yaml` for CodeRabbit's
disable and CLAUDE.md's "BOT REVIEW THREADS" clause for the matching policy
text. Codex (`chatgpt-codex-connector`) replaces them as the sole expected
reviewer. Codex posts NO check-run at all -- confirmed live on #1049 and #1051
(Thu 3 Sep 2026): `gh pr checks` lists no "Codex" row even though
`pulls/<n>/reviews` carries a real `COMMENTED` review from
`chatgpt-codex-connector[bot]`. So Codex has no status line to be a weaker
instrument than; its evidence artifact IS the only instrument, not a
cross-check on one. See CHECKLESS_REVIEWERS below.

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
  / An artifact left against an earlier push does not certify the current one.
    [if] a review's own `commit_id`, or a summary comment's own embedded SHA,
    predates the PR's current head and still counts toward coverage
    [then broken]

Policy change, issue #1016 P1 BLOCKING (PR #1053, thread r3927136609, Thu 3
Sep 2026): evidence used to count from ANY push, not just the current one.
Codex reviews are per-push and routinely skip a head after a fix-push --
documented live on #1009: Codex `Completed` on `884ca47`, then `aeea7de3`
pushed, no new round, and this gate would have reported coverage PASS on
`aeea7de3` off the stale `884ca47` round. `_evidence` now filters every
artifact to the PR's `headRefOid` before it ever reaches a classifier, so a
stale round reads as NO evidence rather than as coverage.

Follow-up findings, issue #1016, PR #1053, same Codex round(s) that reviewed
the head-tie fix above -- each row's own docstring carries the full story:
  / Status Queued/In progress/Failed on the CURRENT head's row must not count
    even though the Commit column already shows that SHA (thread r3927558602,
    P1 BLOCKING): see `_body_is_at_head`.
  / Every review-artifact endpoint must be paginated, not just page 1 (thread
    r3927558605, P2 BLOCKING): see `scripts.review_gh._paginated_json_list`.
  / Evidence sampled against one head is void if the PR moved while the
    network calls ran (thread r3927877681, P1 BLOCKING): see
    `_require_head_unchanged`.
  / This module's own tests must exercise `_gh`'s real subprocess path, never
    a lambda standing in for it (thread r3927877691, P1 BLOCKING): see
    scripts/review_gh.py's docstring and tests/scripts/test_review_coverage_pagination.py.

gh plumbing (`TriageError`, `_gh`, `_checks`, `_head_sha`, `_flatten_pages`,
`_paginated_json_list`) lives in scripts/review_gh.py, split out so this
module's own domain logic can grow the fixes above without crossing this
repo's 600-line file-size ratchet -- see that module's docstring for why.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from scripts.review_gh import TriageError, _checks, _head_sha, _paginated_json_list

REPO = "maintainer/music-dj-tools"

#: Reviewers the merge gate expects. A name absent from a PR's checks (or, for
#: a CHECKLESS_REVIEWERS entry, absent from evidence) is NOT a pass; it is an
#: unmeasured reviewer, which is the whole point of this tool. CodeRabbit and
#: Devin were removed here by issue #1016 (Thu 3 Sep 2026): CodeRabbit is
#: rate-limited on every PR and Devin's trial expired at #530, so neither
#: produces a real review to require.
EXPECTED_REVIEWERS: tuple[str, ...] = ("Codex",)

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
    "Codex": ("chatgpt-codex-connector",),
}

#: Reviewers that post NO check-run at all, ever -- they review by submitting a
#: PR review (and/or inline comments) directly, so there is no status line to
#: require or cross-check against. Codex is the confirmed case (see the module
#: docstring): looking it up in `_checks()` always returns None, which the
#: check-based path below reads as "no check reported", indistinguishable from
#: a reviewer that never ran. For a name listed here, evidence is not a
#: cross-check on a weaker status signal, it is the ONLY signal, so
#: `classify_reviewer` skips the check lookup entirely rather than misreading
#: its permanent absence as a failure.
CHECKLESS_REVIEWERS: frozenset[str] = frozenset({"Codex"})

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
#:
#: As of issue #1016 (Thu 3 Sep 2026) this dict is structurally empty, not
#: just currently empty: `outage` is only ever set on the check-based path in
#: `classify_reviewer`, and the sole entry in EXPECTED_REVIEWERS (Codex) is a
#: CHECKLESS_REVIEWERS entry that never takes that path. It stays wired up for
#: whichever future reviewer posts a check again.
KNOWN_UNAVAILABLE_REVIEWERS: dict[str, str] = {}


# gh plumbing (`TriageError`, `_gh`, `_checks`, `_head_sha`, `_flatten_pages`,
# `_paginated_json_list`) lives in scripts/review_gh.py; `TriageError`,
# `_checks` and `_head_sha` are re-exported here (via the import above) for
# this module's own use and for scripts/review_thread_triage.py's
# `review_coverage.TriageError` reference.


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
    """Exact match on the normalized login, never a substring test.

    issue #1016 P1 BLOCKING, thread r3929765931 (PR #1053, Thu 3 Sep 2026): a
    substring check accepted `chatgpt-codex-connector-attacker` as Codex,
    because `"chatgpt-codex-connector" in login.lower()` is true for any
    login merely CONTAINING the trusted stem. On a public repo any commenter
    could post a current-head `Completed` line under that name and pass
    coverage without a real Codex review. Normalize the `[bot]` suffix and
    require full equality, the same rule `review_thread_parse._is_bot` already
    applies to its own bot-identity check.
    """
    return login.removesuffix("[bot]").lower() in REVIEWER_LOGINS.get(name, ())


#: A commit SHA GitHub renders in backtick-quoted code, e.g. the summary
#: table's `` `7cbe749` `` Commit column or a review body's `` **Reviewed
#: commit:** `7cbe7496d2` ``. GitHub abbreviates to 7+ hex chars, never fewer,
#: so the floor here matches GitHub's own minimum rather than inventing one.
_SHA_IN_BACKTICKS = re.compile(r"`([0-9a-f]{7,40})`", re.IGNORECASE)

#: The summary table's Status cell reads e.g. `` ✅ **Completed** ``,
#: `` ⏳ **Queued** ``, `` 🔄 **In progress** `` or `` ❌ **Failed** ``. Only
#: the first of those is a claim that Codex finished looking at this head.
_STATUS_COMPLETED = re.compile(r"completed", re.IGNORECASE)


def _body_is_at_head(body: str, head_sha: str) -> bool:
    """Does this comment's OWN embedded commit reference match the PR head,
    AND does that same line say the review of it is done?

    Issue comments (as opposed to submitted reviews or inline review
    comments) carry no `commit_id` field at all -- GitHub does not tie them to
    any specific push -- so a bot's summary comment is otherwise untethered
    from any particular head. Codex's summary comment is edited in place each
    round and embeds the SHA it reviewed, so require that embedded prefix to
    match the CURRENT head. That alone is not enough (issue #1016 P1
    BLOCKING, thread r3927558602): the summary's Commit column takes on the
    new head's SHA the moment a round STARTS, before Codex has looked at
    anything, so a row read as `Queued`/`In progress`/`Failed` for the current
    head is a promise, not a review. Require both the SHA and a `completed`
    marker on the SAME table row -- table rows are one line each in Codex's
    rendered markdown, so line-scoping ties the status to the SHA it actually
    describes rather than to any other row of a multi-row table. A body with
    no such row is treated as NOT evidence for this push, the same
    fail-closed direction as zero artifacts.
    """
    for line in body.splitlines():
        shas = _SHA_IN_BACKTICKS.findall(line)
        if not shas:
            continue
        if not any(head_sha.lower().startswith(sha.lower()) for sha in shas):
            continue
        if _STATUS_COMPLETED.search(line):
            return True
    return False


def _collect_evidence(
    name: str,
    reviews: list[dict],
    inline: list[dict],
    issue_comments: list[dict],
    head_sha: str,
) -> ReviewerEvidence:
    """Turn raw API-shaped payloads into evidence tied to the PR's CURRENT
    head. Split out from `_evidence` as a pure step so the head-tie itself is
    directly testable against real captured payload shapes, no network call
    or mock required.

    Submitted reviews and inline review comments both carry `commit_id`, the
    push each was left against, so those filter on plain equality. Issue
    comments carry no such field, so they fall back to `_body_is_at_head`.
    """
    bodies: list[str] = []
    submitted = 0
    for review in reviews:
        if not _matches((review.get("user") or {}).get("login", ""), name):
            continue
        if review.get("commit_id") != head_sha:
            continue
        submitted += 1
        if review.get("body"):
            bodies.append(review["body"])
    comments = sum(
        1
        for c in inline
        if _matches((c.get("user") or {}).get("login", ""), name)
        and c.get("commit_id") == head_sha
    )
    for comment in issue_comments:
        if not _matches((comment.get("user") or {}).get("login", ""), name):
            continue
        body = comment.get("body") or ""
        if _body_is_at_head(body, head_sha):
            bodies.append(body)
    return ReviewerEvidence(submitted, comments, tuple(bodies))


def _evidence(pr: str, head_sha: str) -> dict[str, ReviewerEvidence]:
    """Collect per-reviewer artifacts from the three places bots post, each
    filtered to the PR's current head SHA before it reaches a classifier."""
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    inline = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/comments")
    issue = _paginated_json_list(f"repos/{REPO}/issues/{pr}/comments")
    return {
        name: _collect_evidence(name, reviews, inline, issue, head_sha)
        for name in EXPECTED_REVIEWERS
    }


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


def _classify_from_evidence(name: str, evidence: ReviewerEvidence | None) -> ReviewerVerdict:
    """Classify a CHECKLESS_REVIEWERS entry: evidence is the only instrument.

    There is no status line here to be the weaker signal, so this is not the
    two-instrument cross-check `classify_reviewer` runs below -- zero
    artifacts means nobody looked, full stop. It still scans bodies for the
    NOT_REVIEWED_MARKERS vocabulary, on the same reasoning CodeRabbit's rate
    limit lives in a comment rather than a status: a checkless reviewer that
    ever reports its own skip in a comment body must not read as a pass.
    """
    if evidence is None or evidence.artifact_count == 0:
        return ReviewerVerdict(
            name,
            False,
            "left NO review artifact (0 submitted reviews, 0 inline comments, "
            "0 summary comments); this reviewer posts no status check to fall "
            "back on",
        )
    for body in evidence.bodies:
        lowered_body = body.lower()
        for marker in NOT_REVIEWED_MARKERS:
            if marker in lowered_body:
                return ReviewerVerdict(name, False, marker)
    return ReviewerVerdict(name, True, f"{evidence.artifact_count} artifact(s) found")


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

    A name in CHECKLESS_REVIEWERS never has a status to begin with, so it skips
    straight to the evidence-only path: reading its permanent check-absence as
    the failure "no check reported" would be indistinguishable from a reviewer
    that genuinely never ran, and it isn't the same thing.
    """
    if name in CHECKLESS_REVIEWERS:
        return _classify_from_evidence(name, evidence)

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


def _require_head_unchanged(sampled: str, current: str) -> None:
    """Evidence gathered against `sampled` is void if the PR moved to
    `current` while the three network calls ran (issue #1016 P1 BLOCKING,
    thread r3927877681): `head_sha` was sampled once, before those
    round-trips, with no re-check after. Raising here keeps this a failed
    MEASUREMENT, never a rendered verdict, for a race no retry can undo.
    """
    if sampled != current:
        raise TriageError(
            f"PR head moved from {sampled} to {current} while collecting "
            "review evidence; re-run against the new head"
        )


def triage(pr: str) -> int:
    checks = _checks(pr)
    head_sha = _head_sha(pr)
    evidence = _evidence(pr, head_sha)
    _require_head_unchanged(head_sha, _head_sha(pr))
    verdicts = [
        classify_reviewer(name, checks, evidence.get(name))
        for name in EXPECTED_REVIEWERS
    ]

    print(f"[review-coverage] PR #{pr} @ head {head_sha}")
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
