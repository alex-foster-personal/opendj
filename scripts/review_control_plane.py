"""Control-plane dual review: part of the review-triage verdict (REVIEW-13).

THE RULE (the maintainer, Tue 29 Sep 2026, chosen over human approval): a PR that edits the
control plane needs SUBMITTED reviews at its current head from two different
harnesses among Codex, Sol, Claude, Grok and Cursor, neither of them the PR's
authoring harness. Until this module `just review-triage` passed such a PR on
"1 of 3 AVAILABLE", and #4576 (control plane) merged with only Sol's review.
Putting the rule in review-triage makes every path that reads that verdict
enforce it, instead of one more copy in one more caller.

REVIEW-18 (Thu 1 Oct 2026): Grok and Cursor subscription lanes post submitted
reviews under maintainer with lane markers; they count here when login
and marker both match, like Sol's SOL_LOGINS rule in review_sol.py. Codex has no
marker lane here and is excluded from this ambiguity rule. A body with more than
one lane marker among sol, claude, grok, or cursor counts for no harness in
REVIEW-13's dual-review count; duplicate markers of one lane with differing sha,
model, seat, or skipped values are likewise ambiguous and count for no harness.
Claude's marker regex is imported from review_claude.py (same as Sol from review_sol).

REVIEW-13 DEBT-ONLY CARRY (Thu 1 Oct 2026): a review counts at an earlier head when every
commit since changed only `.planning/debt/<this-pr>.md`, the same rule review_coverage
applies (`review_coverage_carry.is_debt_only_since`, imported, not copied). Without it each
debt-log push reset REVIEW-13 to "(none)" and drew two fresh paid reviews (#4626: Sol
re-raised the same logged P2s 8 times). Independence is unchanged: carried reviews are
still filtered by AUTHOR_EXCLUDES and still need two distinct harnesses. A force-push, any
other path, or another PR's debt file requires fresh reviews; a reviewed SHA that cannot
be fetched is named `carry UNKNOWN` and never counts.
Enforce drops ambiguous submitted reviews before review_coverage collects
evidence, so Sol and Claude are affected too. A Grok or Cursor review whose
skipped= list intersects this PR's control-plane hits does not count.

The path list and the author-trailer rule are #4361's (enqueue gate, OPEN at head
286a755a484a on Thu 1 Oct 2026), so the two cannot disagree once it lands.
Reviewer recognition for Codex, Sol and Claude is review_coverage's own
`_collect_evidence`, passed in by the caller, so who counts is decided in one
place; Grok and Cursor are recognized here from `/pulls/{n}/reviews` markers.

Requirements (mini-PRD):
  / A control-plane PR with fewer than 2 independent submitted reviews at head fails.
    [if] CLAUDE.md edited by a -Claude author with only a Codex review passes [then] broken
    [if] a -Codex author counts Sol as independent [then] broken
  / A PR that touches no control-plane path is untouched by this rule.
    [if] apps/x.py with one reviewer fails here [then] broken
  / An author that cannot be read is a failed measurement, never a pass.
    [if] a commit with no harness trailer yields PASS [then] broken
  / A PR with more commits than the listing's cap is read in full, or not judged at all.
    [if] #3837 (428 commits) reports "may be truncated" [then] broken
    [if] a full listing short of the PR's own commit count yields a verdict [then] broken
  / It runs BEFORE the docs-only exemption: CLAUDE.md and AGENTS.md are *.md files.
    [if] a CLAUDE.md-only PR passes on the docs-only exemption with one reviewer [then] broken
  / REVIEW-18: Grok/Cursor subscription reviews at head count as independent harnesses.
    [if] Sol plus a valid Grok marker at head on a Claude control-plane PR fails [then] broken
    [if] a Grok author counts its own Grok review [then] broken
    [if] sol+grok in one body still counts as Sol via enforce [then] broken
  / REVIEW-13 carries two independent reviews across a debt-only push.
    [if] two reviews at R and a debt-only commit for this PR's file fail [then] broken
    [if] another path, another PR's debt file or a force-push still carries [then] broken
    [if] a carried review of the author's own family counts [then] broken
"""

from __future__ import annotations

import fnmatch
import functools
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.review_claude import CLAUDE_MARKER
from scripts.review_control_plane_carry import (
    _NO_CARRY,
    SUBMITTED_REVIEW_STATES,
    CarriedReview,
    Carry,
    debt_only_carry,
    print_carry_proofs,
)
from scripts.review_gh import TriageError
from scripts.review_sol import SOL_MARKER

_SOL_MARKER_GROUPS = 4
_CLAUDE_MARKER_GROUPS = 3


def _require_marker_group_count(
    marker_name: str,
    marker: re.Pattern[str],
    expected: int,
    fields: str,
) -> None:
    if marker.groups != expected:
        raise RuntimeError(
            f"{marker_name} shape changed: expected {expected} capture groups ({fields}), "
            f"got {marker.groups}"
        )


_require_marker_group_count(
    "SOL_MARKER", SOL_MARKER, _SOL_MARKER_GROUPS, "sha, seat, model, skipped"
)
_require_marker_group_count(
    "CLAUDE_MARKER", CLAUDE_MARKER, _CLAUDE_MARKER_GROUPS, "sha, model, skipped"
)

# fnmatch semantics: `*` also crosses `/`, so `.github/workflows/*` covers the tree.
ROOT_IMPORT_PATHS: tuple[str, ...] = ("__pycache__/*", "_winapi/*", "msvcrt/*", "nt/*", "org/*")
CONTROL_PLANE_PATHS: tuple[str, ...] = (
    ".github/workflows/*",
    ".github/zizmor.yml",
    ".trunk/*",
    ".mergify.yml",
    ".coderabbit.yaml",
    "scripts/review_*.py",
    "scripts/sol_review.py",
    "scripts/claude_review.py",
    "scripts/codex_home.py",
    "ops/fleet/kpi.sh",
    "scripts/oss_tip_rules.py",
    ".claude/settings*",
    ".claude/rules/*",
    ".mcp.json",
    "AGENTS.md",
    "CLAUDE.md",
    "justfile",
    "ci/trunk-merge-queue.json",
    "scripts/__init__.py",
    "scripts/enqueue_gate*.py",
    "scripts/ci_health_core.py",
    "scripts/ci_pr_scope.py",
    "scripts/ci_required_gate.py",
    "scripts/ci_clean_untracked.sh",
    "scripts/ci_runner_preflight.sh",
    "scripts/ci_stale_install_guard.py",
    "scripts/ci_wait_workflows.py",
    "scripts/debt_index.py",
    "scripts/gh_version_guard.py",
    "scripts/pr_scope_check.py",
    "scripts/worker_worktree_guard.py",
    "scripts/worktree_census.py",
    "scripts/worktree_lifecycle.py",
    "tests/scripts/test_enqueue_gate*.py",
    "tests/scripts/enqueue_gate_*.py",
    "tests/scripts/test_review_submitted_state.py",
    "scripts/interpreter_contract.py",
    "scripts/provenance_*.py",
    "tests/scripts/provenance_fixtures.py",
    "tests/scripts/test_review_coverage_head_tie.py",
    "tests/fixtures/_resolver.py",
    "tests/testclient_host_allowlist.py",
    "tests/standalone/__init__.py",
    "tests/standalone/guard.py",
    "tests/support/__init__.py",
    "tests/support/thread_leaks.py",
    "apps/analysis/__init__.py",
    "apps/analysis/pcm_fingerprint.py",
    "apps/shared/__init__.py",
    "apps/shared/ffmpeg.py",
    "apps/shared/hashing.py",
    "apps/shared/rekordbox_writeback.py",
    "tests/fixtures/github/enqueue_gate_*",
    "tests/fixtures/github/review_pending_real.*",
    "conftest.py",
    "tests/conftest.py",
    "tests/scripts/conftest.py",
    "tests/__init__.py",
    "tests/scripts/__init__.py",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".python-versions",
    "uv.toml",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "tests/scripts/test_review_control_plane.py",
    *ROOT_IMPORT_PATHS,
)
CONTROL_PLANE_ROOT_FILES: tuple[str, ...] = ("*.py", "*.pyc", "*.so", "*.pyd")
DUAL_REVIEW_MIN = 2
#: GET /pulls/{n}/commits returns at most 250 commits, and so does GraphQL's
#: `pullRequest.commits` (measured on #3837 Thu 1 Oct 2026: totalCount 428, 250 paged out).
COMMITS_API_CAP = 250
COMPARE_PAGE_SIZE = 100  # GET /compare/{base}...{head} pages its commits, 100 at most per page
#: Authoring harness (commit trailer) -> reviewer harnesses it may not stand in for.
#: Sol is GPT driven through the Codex CLI, so a -Codex author excludes Sol too.
AUTHOR_EXCLUDES: Mapping[str, frozenset[str]] = {
    "Claude": frozenset({"Claude"}),
    "Codex": frozenset({"Codex", "Sol"}),
    "Cursor": frozenset({"Cursor"}),
    "Grok": frozenset({"Grok"}),
}
AUTHOR_MARKER = re.compile(r"-(Claude|Codex|Cursor|Grok)[ \t]*")
GIT_TRAILER_LINE = re.compile(r"[A-Za-z][A-Za-z0-9-]*: \S.*")
#: Same trusted-login rule as Sol's SOL_LOGINS in scripts/review_sol.py; login AND marker required.
SUBSCRIPTION_REVIEW_LOGINS: frozenset[str] = frozenset({"maintainer"})
GROK_REVIEW_MARKER = re.compile(
    r"<!--\s*grok-review\s+v1\s+sha=(?P<sha>[0-9a-f]{40})\s+model=(?P<model>\S+)"
    r"(?:\s+skipped=(?P<skipped>[\w.,/-]+))?\s*-->",
    re.IGNORECASE,
)
CURSOR_REVIEW_MARKER = re.compile(
    r"<!--\s*cursor-review\s+v1\s+sha=(?P<sha>[0-9a-f]{40})\s+model=(?P<model>\S+)"
    r"(?:\s+skipped=(?P<skipped>[\w.,/-]+))?\s*-->",
    re.IGNORECASE,
)
#: All marker lanes used for ambiguity detection (Codex has no marker here).
_AMBIGUITY_LANE_MARKERS: Mapping[str, re.Pattern[str]] = {
    "Sol": SOL_MARKER,
    "Claude": CLAUDE_MARKER,
    "Grok": GROK_REVIEW_MARKER,
    "Cursor": CURSOR_REVIEW_MARKER,
}


#: The last `enforce` FAIL's detail, for review_blocker's one-line BLOCKER; empty when the
#: last run did not fail. Module state on purpose: triage hands back only an exit code.
_LAST_FAILURE: list[str] = []


def last_failure() -> str | None:
    return _LAST_FAILURE[-1] if _LAST_FAILURE else None


@dataclass(frozen=True)
class DualReview:
    ok: bool | None  # None = could not measure
    detail: str
    carried: tuple[CarriedReview, ...] = ()


@dataclass(frozen=True)
class CommitList:
    commits: Sequence[Mapping] | None  # None = not provably every commit of the PR
    detail: str


def _is_root_import_surface(path: str) -> bool:
    """`python -m` puts the repository root ahead of the standard library."""
    parts = path.split("/")
    if len(parts) == 1:
        name = parts[0]
    elif len(parts) == 2 and parts[1].split(".")[0] == "__init__":
        name = parts[1]
    else:
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in CONTROL_PLANE_ROOT_FILES)


def control_plane_hits(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            p
            for p in set(paths)
            if any(fnmatch.fnmatchcase(p, g) for g in CONTROL_PLANE_PATHS) or _is_root_import_surface(p)
        )
    )


def author_markers(message: str) -> set[str]:
    """Harness markers from the message's trailer block only (#4361 r4137687862)."""
    markers: set[str] = set()
    for paragraph in reversed(re.split(r"\n[ \t]*\n", message.strip())):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines or not all(AUTHOR_MARKER.fullmatch(x) or GIT_TRAILER_LINE.fullmatch(x) for x in lines):
            break
        markers |= {m.group(1) for x in lines if (m := AUTHOR_MARKER.fullmatch(x))}
    return markers


def _normalize_login(login: str) -> str:
    return login.removesuffix("[bot]").lower()


def _grok_model_counts(model: str) -> bool:
    lowered = model.lower()
    if lowered == "unknown" or lowered.startswith("requested:"):
        return False
    return lowered.startswith("grok-")


def _cursor_model_counts(model: str) -> bool:
    lowered = model.lower()
    if lowered == "unknown" or lowered.startswith("requested:"):
        return False
    return lowered.startswith(("composer-", "cursor-"))


def _subscription_marker_signature(lane: str, match: re.Match[str]) -> tuple[str, str, str, str | None]:
    if lane == "Sol":
        sha = match.group(1)
        seat = match.group(2)
        model = match.group(3)
        skipped = match.group(4)
        return (sha.lower(), seat.lower(), model.lower(), skipped)
    if lane == "Claude":
        sha = match.group(1)
        model = match.group(2)
        skipped = match.group(3)
        return (sha.lower(), "", model.lower(), skipped)
    if lane in ("Grok", "Cursor"):
        sha = match.group("sha")
        model = match.group("model")
        skipped = match.groupdict().get("skipped")
        seat_group = match.groupdict().get("seat")
        seat = "" if seat_group is None else seat_group.lower()
        return (sha.lower(), seat, model.lower(), skipped)
    raise ValueError(f"unknown marker lane: {lane}")


def _review_body_text(review: Mapping) -> str:
    body = review["body"]
    if body is None:
        return ""
    return str(body)


def _subscription_lanes_with_markers(body: str) -> frozenset[str]:
    present: set[str] = set()
    for lane, pattern in _AMBIGUITY_LANE_MARKERS.items():
        if pattern.search(body):
            present.add(lane)
    return frozenset(present)


def _subscription_review_body_ambiguous(body: str) -> bool:
    if len(_subscription_lanes_with_markers(body)) > 1:
        return True
    for lane, pattern in _AMBIGUITY_LANE_MARKERS.items():
        matches = list(pattern.finditer(body))
        if len(matches) <= 1:
            continue
        sigs = {_subscription_marker_signature(lane, m) for m in matches}
        if len(sigs) > 1:
            return True
    return False


def _normalize_skipped_token(raw: str) -> str:
    return raw.strip().removeprefix("./").rstrip("/")


def _skipped_token_intersects_hits(token: str, control_plane_hit_paths: Collection[str]) -> bool:
    return any(hit == token or hit.startswith(f"{token}/") for hit in control_plane_hit_paths)


def _skipped_intersects_control_plane_hits(
    skipped_raw: str | None,
    control_plane_hit_paths: Collection[str],
) -> bool:
    if not skipped_raw:
        return False
    for part in skipped_raw.split(","):
        token = _normalize_skipped_token(part)
        if not token:
            continue
        if _skipped_token_intersects_hits(token, control_plane_hit_paths):
            return True
    return False


def _subscription_lane_at_head(
    reviews: Sequence[Mapping],
    head_sha: str,
    marker: re.Pattern[str],
    model_ok: Callable[[str], bool],
    control_plane_hit_paths: Collection[str],
) -> bool:
    want = head_sha.lower()
    for review in reviews:
        state = str(review["state"]).upper()
        if state not in SUBMITTED_REVIEW_STATES:
            continue
        login = str(review["user"]["login"])
        if _normalize_login(login) not in SUBSCRIPTION_REVIEW_LOGINS:
            continue
        if str(review["commit_id"]).lower() != want:
            continue
        body = "" if review["body"] is None else str(review["body"])
        if _subscription_review_body_ambiguous(body):
            continue
        match = marker.search(body)
        if not match or match.group("sha").lower() != want:
            continue
        if _skipped_intersects_control_plane_hits(match.groupdict().get("skipped"), control_plane_hit_paths):
            continue
        if model_ok(match.group("model")):
            return True
    return False


def subscription_reviewed_at_head(
    reviews: Sequence[dict],
    head_sha: str,
    control_plane_hit_paths: Collection[str],
) -> dict[str, bool]:
    """Grok and Cursor submitted reviews at head (login + exact-sha marker + model family)."""
    seq: Sequence[Mapping] = reviews
    return {
        "Grok": _subscription_lane_at_head(
            seq, head_sha, GROK_REVIEW_MARKER, _grok_model_counts, control_plane_hit_paths
        ),
        "Cursor": _subscription_lane_at_head(
            seq, head_sha, CURSOR_REVIEW_MARKER, _cursor_model_counts, control_plane_hit_paths
        ),
    }


def author_harnesses(commits: Sequence[Mapping]) -> tuple[frozenset[str] | None, str]:
    """Union of every commit's trailer harnesses (REST /pulls/{n}/commits shape).

    None when any non-merge commit carries no trailer: an unknown author cannot be excluded.
    `commits` must be the PR's EVERY commit: `complete_commits` is what proves that.
    """
    authored = [c for c in commits if len(c.get("parents") or []) <= 1]
    if not authored:
        return None, "no non-merge commit to read an author trailer from"
    found: set[str] = set()
    for commit in commits:
        marks = author_markers(str((commit.get("commit") or {}).get("message", "")))
        if not marks and len(commit.get("parents") or []) <= 1:
            return None, f"commit {str(commit.get('sha', ''))[:9]} has no author trailer"
        found |= marks
    return frozenset(found), f"author {'+'.join(sorted(found))} (commit trailers)"


def complete_commits(
    capped: Sequence[Mapping],
    uncapped: Callable[[], tuple[Sequence[Mapping], int]],
    head_sha: str,
) -> CommitList:
    """The PR's every commit, or the reason that could not be proven.

    Under the cap the capped listing is the whole list and `uncapped` is never called. At
    the cap it may be truncated, so `uncapped` supplies a paged listing AND the commit count
    the PR itself reports; anything short of that count, repeated, missing a commit the
    capped listing holds, or not ending at the head under review is unmeasured.
    """
    if len(capped) < COMMITS_API_CAP:
        return CommitList(capped, "")
    listed, reported = uncapped()
    shas = [str(c.get("sha", "")) for c in listed]
    if len(set(shas)) != reported or len(shas) != reported:
        return CommitList(
            None, f"{len(set(shas))} distinct commits listed, the PR reports {reported}; the list is incomplete"
        )
    if absent := {str(c.get("sha", "")) for c in capped} - set(shas):
        return CommitList(None, f"commit {sorted(absent)[0][:9]} is absent from the full listing")
    if shas[-1] != head_sha:
        return CommitList(None, f"the full listing ends at {shas[-1][:9]}; it does not end at head {head_sha[:9]}")
    return CommitList(listed, "")


def dual_review(
    hits: Sequence[str],
    authors: frozenset[str] | None,
    author_detail: str,
    reviewed_at_head: Mapping[str, bool],
    carry: Carry = _NO_CARRY,
) -> DualReview:
    """Pure verdict. `reviewed_at_head[name]`: that harness left a SUBMITTED review at head.

    `carry` adds reviewers whose submitted review sits at an earlier, debt-only-equivalent head.
    """
    if not hits:
        return DualReview(True, "no control-plane path touched")
    shown = ", ".join(hits[:5]) + (f" (+{len(hits) - 5} more)" if len(hits) > 5 else "")
    if authors is None:
        return DualReview(None, f"control plane ({shown}): {author_detail}")
    excluded = frozenset().union(*(AUTHOR_EXCLUDES[a] for a in authors))
    carried = {c.reviewer: c for c in carry.reviews if not reviewed_at_head.get(c.reviewer)}
    counted_names = sorted(
        n for n in {*reviewed_at_head, *carried} if (reviewed_at_head.get(n) or n in carried) and n not in excluded
    )
    counted = [carried[n].phrase if n in carried else n for n in counted_names]
    ok = len(counted) >= DUAL_REVIEW_MIN
    detail = (
        f"control plane ({shown}); {author_detail}; independent submitted reviews at head: "
        f"{', '.join(counted) or '(none)'} (need {DUAL_REVIEW_MIN}; excluded as author: "
        f"{', '.join(sorted(excluded)) or '(none)'})"
    )
    if carry.unknown and not ok:
        detail += f"; carry UNKNOWN ({'; '.join(carry.unknown)}), so a review at head is required"
    used = tuple(carried[n] for n in counted_names if n in carried)
    return DualReview(ok, detail, used)


def measure(
    changed_files: Sequence[str],
    commits: Callable[[], CommitList],
    reviewed_at_head: Callable[[], Mapping[str, bool]],
    carried: Callable[[Mapping[str, bool]], Carry] = lambda _at_head: _NO_CARRY,
) -> DualReview:
    """Read only what a control-plane PR needs; a PR outside it costs no extra call.

    Carry is read only when the head alone fails, so a PR with two fresh reviews pays no git call.
    """
    hits = control_plane_hits(changed_files)
    if not hits:
        return dual_review(hits, frozenset(), "", {})
    listing = commits()
    if listing.commits is None:
        return dual_review(hits, None, listing.detail, {})
    authors, author_detail = author_harnesses(listing.commits)
    if authors is None:
        return dual_review(hits, None, author_detail, {})  # unmeasured already: spend no review reads
    at_head = reviewed_at_head()
    verdict = dual_review(hits, authors, author_detail, at_head)
    if verdict.ok is not False:
        return verdict
    return dual_review(hits, authors, author_detail, at_head, carried(at_head))


def enforce(pr: str, head_sha: str, changed_files: Sequence[str]) -> int:
    """review_coverage.triage's hook: 0 to continue, 1 after printing the FAIL line.

    Raises TriageError when it cannot measure. review_coverage is imported here, not
    at the top, because it imports this module; reading `_paginated_json_list`
    through it keeps the one fetch seam its tests already use.
    """
    from scripts import review_coverage as rc

    _LAST_FAILURE.clear()

    cp_hits = control_plane_hits(changed_files)
    repo_root: Path = rc.CHECKOUT_ROOT

    @functools.cache
    def payloads() -> tuple[list[dict], list[dict], list[dict]]:
        reviews = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/reviews")
        inline = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/comments")
        issue = rc._paginated_json_list(f"repos/{rc.REPO}/issues/{pr}/comments")
        unambiguous_reviews = [
            review
            for review in reviews
            if not _subscription_review_body_ambiguous(_review_body_text(review))
        ]
        return unambiguous_reviews, inline, issue

    def reviewed_at(sha: str) -> dict[str, bool]:
        unambiguous_reviews, inline, issue = payloads()
        found = {}
        for name in rc.EXPECTED_REVIEWERS:
            evidence = rc._collect_evidence(name, unambiguous_reviews, inline, issue, sha)
            reviewed = rc._classify_from_evidence(name, evidence).reviewed
            found[name] = reviewed and evidence.submitted_reviews > 0
        found.update(subscription_reviewed_at_head(unambiguous_reviews, sha, cp_hits))
        return found

    def carried(at_head: Mapping[str, bool]) -> Carry:
        return debt_only_carry(pr, head_sha, repo_root, payloads()[0], reviewed_at, at_head)

    def uncapped() -> tuple[list[dict], int]:
        (pull,) = rc._paginated_json_pages(f"repos/{rc.REPO}/pulls/{pr}")
        # One snapshot: the count is this head's, and both compare ends are SHAs, so a base
        # branch that moves between pages cannot change which commits are listed.
        rc._require_head_unchanged(head_sha, pull["head"]["sha"])
        compare = f"repos/{rc.REPO}/compare/{pull['base']['sha']}...{head_sha}?per_page={COMPARE_PAGE_SIZE}"
        return [c for page in rc._paginated_json_pages(compare) for c in page["commits"]], pull["commits"]

    verdict = measure(
        changed_files,
        commits=lambda: complete_commits(
            rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/commits"), uncapped, head_sha
        ),
        reviewed_at_head=lambda: reviewed_at(head_sha),
        carried=carried,
    )
    if verdict.ok is None:
        raise TriageError(f"control-plane dual review (REVIEW-13) could not measure: {verdict.detail}")
    if not control_plane_hits(changed_files):
        return 0
    rc._require_head_unchanged(head_sha, rc._head_sha(pr))
    if verdict.ok:
        print(f"[review-coverage] control-plane dual review (REVIEW-13) ok: {verdict.detail}")
        print_carry_proofs(verdict.carried, head_sha)
        return 0
    _LAST_FAILURE.append(verdict.detail)
    print(f"[review-coverage] FAIL: control-plane dual review (REVIEW-13): {verdict.detail}")
    print_carry_proofs(verdict.carried, head_sha)
    return 1
