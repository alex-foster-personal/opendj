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
and marker both match, like Sol's SOL_LOGINS rule in review_sol.py.

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
  / It runs BEFORE the docs-only exemption: CLAUDE.md and AGENTS.md are *.md files.
    [if] a CLAUDE.md-only PR passes on the docs-only exemption with one reviewer [then] broken
  / REVIEW-18: Grok/Cursor subscription reviews at head count as independent harnesses.
    [if] Sol plus a valid Grok marker at head on a Claude control-plane PR fails [then] broken
    [if] a Grok author counts its own Grok review [then] broken
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from scripts.review_gh import TriageError

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
COMMITS_API_CAP = 250  # GET /pulls/{n}/commits returns at most 250 commits
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
#: Pull-request review states that count as submitted (excludes PENDING and DISMISSED).
SUBMITTED_REVIEW_STATES: frozenset[str] = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})
#: Same trusted-login rule as Sol's SOL_LOGINS in scripts/review_sol.py; login AND marker required.
SUBSCRIPTION_REVIEW_LOGINS: frozenset[str] = frozenset({"maintainer"})
GROK_REVIEW_MARKER = re.compile(
    r"<!--\s*grok-review\s+v1\s+sha=([0-9a-f]{40})\s+model=(\S+)"
    r"(?:\s+skipped=[\w.,/-]+)?\s*-->",
    re.IGNORECASE,
)
CURSOR_REVIEW_MARKER = re.compile(
    r"<!--\s*cursor-review\s+v1\s+sha=([0-9a-f]{40})\s+model=(\S+)"
    r"(?:\s+skipped=[\w.,/-]+)?\s*-->",
    re.IGNORECASE,
)


#: The last `enforce` FAIL's detail, for review_blocker's one-line BLOCKER; empty when the
#: last run did not fail. Module state on purpose: triage hands back only an exit code.
_LAST_FAILURE: list[str] = []


def last_failure() -> str | None:
    return _LAST_FAILURE[-1] if _LAST_FAILURE else None


@dataclass(frozen=True)
class DualReview:
    ok: bool | None  # None = could not measure
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


def _subscription_lane_at_head(
    reviews: Sequence[Mapping],
    head_sha: str,
    marker: re.Pattern[str],
    model_ok: Callable[[str], bool],
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
        match = marker.search(body)
        if not match or match.group(1).lower() != want:
            continue
        if model_ok(match.group(2)):
            return True
    return False


def subscription_reviewed_at_head(reviews: Sequence[dict], head_sha: str) -> dict[str, bool]:
    """Grok and Cursor submitted reviews at head (login + exact-sha marker + model family)."""
    seq: Sequence[Mapping] = reviews
    return {
        "Grok": _subscription_lane_at_head(seq, head_sha, GROK_REVIEW_MARKER, _grok_model_counts),
        "Cursor": _subscription_lane_at_head(seq, head_sha, CURSOR_REVIEW_MARKER, _cursor_model_counts),
    }


def author_harnesses(commits: Sequence[Mapping]) -> tuple[frozenset[str] | None, str]:
    """Union of every commit's trailer harnesses (REST /pulls/{n}/commits shape).

    None when any non-merge commit carries no trailer: an unknown author cannot be excluded.
    """
    if len(commits) >= COMMITS_API_CAP:
        return None, f"{len(commits)} commits listed, the API cap; the list may be truncated"
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


def dual_review(
    hits: Sequence[str],
    authors: frozenset[str] | None,
    author_detail: str,
    reviewed_at_head: Mapping[str, bool],
) -> DualReview:
    """Pure verdict. `reviewed_at_head[name]`: that harness left a SUBMITTED review at head."""
    if not hits:
        return DualReview(True, "no control-plane path touched")
    shown = ", ".join(hits[:5]) + (f" (+{len(hits) - 5} more)" if len(hits) > 5 else "")
    if authors is None:
        return DualReview(None, f"control plane ({shown}): {author_detail}")
    excluded = frozenset().union(*(AUTHOR_EXCLUDES[a] for a in authors))
    counted = sorted(n for n, ok in reviewed_at_head.items() if ok and n not in excluded)
    detail = (
        f"control plane ({shown}); {author_detail}; independent submitted reviews at head: "
        f"{', '.join(counted) or '(none)'} (need {DUAL_REVIEW_MIN}; excluded as author: "
        f"{', '.join(sorted(excluded)) or '(none)'})"
    )
    return DualReview(len(counted) >= DUAL_REVIEW_MIN, detail)


def measure(
    changed_files: Sequence[str],
    commits: Callable[[], list[dict]],
    reviewed_at_head: Callable[[], Mapping[str, bool]],
) -> DualReview:
    """Read only what a control-plane PR needs; a PR outside it costs no extra call."""
    hits = control_plane_hits(changed_files)
    if not hits:
        return dual_review(hits, frozenset(), "", {})
    authors, author_detail = author_harnesses(commits())
    return dual_review(hits, authors, author_detail, reviewed_at_head())


def enforce(pr: str, head_sha: str, changed_files: Sequence[str]) -> int:
    """review_coverage.triage's hook: 0 to continue, 1 after printing the FAIL line.

    Raises TriageError when it cannot measure. review_coverage is imported here, not
    at the top, because it imports this module; reading `_paginated_json_list`
    through it keeps the one fetch seam its tests already use.
    """
    from scripts import review_coverage as rc

    _LAST_FAILURE.clear()

    def reviewed_at_head() -> dict[str, bool]:
        reviews = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/reviews")
        inline = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/comments")
        issue = rc._paginated_json_list(f"repos/{rc.REPO}/issues/{pr}/comments")
        found = {}
        for name in rc.EXPECTED_REVIEWERS:
            evidence = rc._collect_evidence(name, reviews, inline, issue, head_sha)
            reviewed = rc._classify_from_evidence(name, evidence).reviewed
            found[name] = reviewed and evidence.submitted_reviews > 0
        found.update(subscription_reviewed_at_head(reviews, head_sha))
        return found

    verdict = measure(
        changed_files,
        commits=lambda: rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/commits"),
        reviewed_at_head=reviewed_at_head,
    )
    if verdict.ok is None:
        raise TriageError(f"control-plane dual review (REVIEW-13) could not measure: {verdict.detail}")
    if not control_plane_hits(changed_files):
        return 0
    rc._require_head_unchanged(head_sha, rc._head_sha(pr))
    if verdict.ok:
        print(f"[review-coverage] control-plane dual review (REVIEW-13) ok: {verdict.detail}")
        return 0
    _LAST_FAILURE.append(verdict.detail)
    print(f"[review-coverage] FAIL: control-plane dual review (REVIEW-13): {verdict.detail}")
    return 1
