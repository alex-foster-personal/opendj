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
  / A gate older than main's own gate code renders no verdict (PR #1720).
    [if] a branch cut before a gate fix on main prints PASS [then broken]
    -- see scripts/review_gate_freshness.py
  / Debt-only pushes carry coverage when only this PR's debt file changed since
    the reviewed head (issues #2907, #2871, ADR-0049, REVIEW-08; see
    scripts/review_coverage_carry.py). When carry applies, triage prints both
    SHAs and the local ``git diff --name-only`` path list.
  / Base merges carry coverage when main changed no path the PR touches and
    the PR's net diff is byte-identical at both heads (REVIEW-16; see
    scripts/review_coverage_base_merge.py). Unmeasurable reads carry UNKNOWN.

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
`_body_is_at_head` joined it there Sun 6 Sep 2026 for the same reason (#T8).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

try:
    from scripts.review_gh import (
        TriageError,
        _body_is_at_head,
        _checks,
        _head_sha,
        _paginated_json_list,
        _paginated_json_pages,  # noqa: F401  (review_control_plane's fetch seam)
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.review_coverage") from None
    raise
from scripts.review_claude import CLAUDE, is_claude_artifact
from scripts.review_control_plane import enforce as enforce_control_plane
from scripts.review_coverage_carry import (
    ReviewCarryInputs,
    print_carry_proofs,
    verdicts_with_carry,
)
from scripts.review_docs_only import is_docs_only, render_docs_only_pass
from scripts.review_gate_freshness import CHECKOUT_ROOT, require_gate_current_with_main
from scripts.review_sol import SOL, is_sol_artifact, substitute_alternatives
from scripts.review_subscription import CURSOR, GROK, LANES_BY_NAME

REPO = "maintainer/music-dj-tools"

#: Reviewers the merge gate expects. A name absent from a PR's checks (or, for
#: a CHECKLESS_REVIEWERS entry, absent from evidence) is NOT a pass; it is an
#: unmeasured reviewer, which is the whole point of this tool. CodeRabbit and
#: Devin were removed here by issue #1016 (Thu 3 Sep 2026): CodeRabbit is
#: rate-limited on every PR and Devin's trial expired at #530, so neither
#: produces a real review to require.
#: Sol joined Thu 4 Sep 2026 (issue #1211): the Codex GitHub app ran out of
#: quota at ~18:30Z and has posted a usage-limit notice instead of a review
#: ever since. The three are ALTERNATIVES, not additional requirements -- see
#: `review_sol.substitute_alternatives`, applied in `triage` below.
#: Claude joined Sun 6 Sep 2026, authorized by the maintainer: by then BOTH ChatGPT
#: subscription seats Sol can reach were walled too, with a stated reset of
#: Thu 11 Sep, so every named reviewer was down at once and no new PR head
#: could be covered by anything.
#: Grok and Cursor joined Thu 1 Oct 2026 (the maintainer's "Auto failover chain"
#: decision): subscription lanes via the af-sub-broker, tried by
#: `just review <PR>` in exactly this order, Claude last because it bills
#: the maintainer's own allowance. See scripts/review_chain.py.
EXPECTED_REVIEWERS: tuple[str, ...] = ("Codex", SOL, GROK, CURSOR, CLAUDE)

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

#: Reviewers recognized by login PLUS an embedded marker rather than by a bot
#: login alone, each with the matcher that owns that pair. A CLI lane posts
#: through `gh` as the maintainer, so neither signal is sufficient by itself; the
#: matchers live beside the markers they read (scripts/review_sol.py,
#: scripts/review_claude.py) so a writer and its reader cannot drift apart.
_MARKER_REVIEWERS: dict[str, Callable[[str, str, str], bool]] = {
    SOL: is_sol_artifact,
    CLAUDE: is_claude_artifact,
    **{name: lane.is_artifact for name, lane in LANES_BY_NAME.items()},
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
#: Sol and Claude post no check either: each is a CLI run whose only trace on
#: the PR is the review it submits, so evidence is their sole instrument too.
CHECKLESS_REVIEWERS: frozenset[str] = frozenset({"Codex", SOL, CLAUDE, *LANES_BY_NAME})

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
# `_paginated_json_list`, `_body_is_at_head`) lives in scripts/review_gh.py;
# `TriageError`, `_checks` and `_head_sha` are re-exported here (via the
# import above) for this module's own use and for
# scripts/review_thread_triage.py's `review_coverage.TriageError` reference.


# ----- review artifacts ---------------------------------------------------


def _changed_files(pr: str) -> list[str]:
    """Every path the diff touches, rename sources included (REVIEW-13), across all pages. A `gh`
    failure raises `TriageError`: a failed listing is never read as an empty, non-docs-only PR."""
    entries = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/files")
    return [p for e in entries for p in (e["filename"], e.get("previous_filename")) if p]


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

    A submitted review's `commit_id` is the push it was left against, so it
    filters on plain equality. An inline comment's `commit_id` is NOT: GitHub
    carries it forward to every newer push its diff position still applies
    to, so it filters on `original_commit_id`, and a comment without one
    counts for nothing (PR #1717, Thu 10 Sep 2026: three older Codex comments
    certified head f8f0cad79 while Codex's own review of it had failed).
    Issue comments carry no SHA field, so they fall back to `_body_is_at_head`.
    """
    def wrote(payload: dict) -> bool:
        """Did `name` write this artifact? Codex is known by its bot login;
        the CLI lanes have no bot account and are known by login PLUS marker
        (see scripts/review_sol.py for why neither half suffices alone)."""
        login = (payload.get("user") or {}).get("login", "")
        if matcher := _MARKER_REVIEWERS.get(name):
            return matcher(login, payload.get("body") or "", head_sha)
        return _matches(login, name)

    bodies: list[str] = []
    submitted = 0
    for review in reviews:
        if not wrote(review):
            continue
        if review.get("commit_id") != head_sha:
            continue
        submitted += 1
        if review.get("body"):
            bodies.append(review["body"])
    comments = sum(1 for c in inline if wrote(c) and c.get("original_commit_id") == head_sha)
    for comment in issue_comments:
        if not wrote(comment):
            continue
        body = comment.get("body") or ""
        # A lane marker already carries the head it reviewed, so `wrote` has
        # done the head-tie `_body_is_at_head` does for Codex's summary table.
        if name in _MARKER_REVIEWERS or _body_is_at_head(body, head_sha):
            bodies.append(body)
    return ReviewerEvidence(submitted, comments, tuple(bodies))


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
    # The alternative reviewer that covered for this one, when this reviewer
    # left nothing. Not folded into `reason`: the board prints `sub` off this
    # field, so a covered row never reads as "ok" and claims a review that did
    # not happen. See `review_sol.substitute_alternatives`.
    substituted_by: str = ""
    carried_from: str = ""  # full debt-only carry source SHA (issues #2907, #2871)
    carried_paths: frozenset[str] = frozenset()  # git diff paths for carry proof
    carry_proof: tuple[str, ...] = ()  # base-merge carry proof lines (REVIEW-16)
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
    gate_commit = require_gate_current_with_main()
    head_sha = _head_sha(pr)
    changed_files = _changed_files(pr)
    if control_plane_rc := enforce_control_plane(pr, head_sha, changed_files):
        return control_plane_rc  # REVIEW-13, before the docs-only exemption: CLAUDE.md is *.md
    if is_docs_only(changed_files):
        # Coverage is the only thing this exemption waives. Re-sample the
        # head before printing, the same race guard the normal path applies
        # after ITS network calls (`_require_head_unchanged` below): a push
        # landing between the head sample and the files listing must void
        # this verdict rather than certify a head nobody measured.
        _require_head_unchanged(head_sha, _head_sha(pr))
        print(render_docs_only_pass(pr, head_sha, gate_commit, changed_files))
        return 0

    checks = _checks(pr)
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    inline = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/comments")
    issue = _paginated_json_list(f"repos/{REPO}/issues/{pr}/comments")
    evidence = {
        name: _collect_evidence(name, reviews, inline, issue, head_sha)
        for name in EXPECTED_REVIEWERS
    }
    _require_head_unchanged(head_sha, _head_sha(pr))

    verdicts = substitute_alternatives(
        verdicts_with_carry(
            pr,
            head_sha,
            ReviewCarryInputs(
                checks=checks,
                evidence=evidence,
                reviews=reviews,
                inline=inline,
                issue_comments=issue,
                repo_root=CHECKOUT_ROOT,
                expected_reviewers=EXPECTED_REVIEWERS,
                classify_reviewer=classify_reviewer,
            ),
        ),
        EXPECTED_REVIEWERS,
    )

    print(f"[review-coverage] PR #{pr} @ head {head_sha} (gate code has main's {gate_commit[:9]})")
    print()
    print("  reviewer coverage")
    for verdict in verdicts:
        mark = "MISS" if not verdict.reviewed else ("sub " if verdict.substituted_by else "ok  ")
        found = evidence.get(verdict.name)
        artifacts = f" [{found.artifact_count} artifact(s)]" if found else ""
        print(f"    {mark} {verdict.name}: {verdict.reason}{artifacts}")

    print_carry_proofs(verdicts, head_sha, CHECKOUT_ROOT)

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
    # Substitutions are counted OUT of "reviewed" rather than folded into it:
    # "all 2 reviewed" on a PR only Sol looked at is the same class of lie the
    # artifact requirement exists to stop.
    subbed = sum(1 for v in verdicts if v.substituted_by)
    print(
        f"[review-coverage] PASS: {covered - subbed} of {covered} AVAILABLE reviewer(s) reviewed"
        + (f", {subbed} covered by an alternative" if subbed else "")
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
