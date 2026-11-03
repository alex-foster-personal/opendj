"""Control-plane review note: advisory since REVIEW-8220 (REVIEW-13 retired as a gate).

REVIEW-8220 (the maintainer, 19:30Z Mon 2 Nov 2026): "lax the 2 review requirement entirely to be soft -
replace with a reminder to try to include a review where appropriate (just one is fine) and no
strict requirement at all". `enforce` still measures who reviewed, and prints either the `ok`
line or a `[review-coverage] NOTE:` line; it never returns a failure, and an author it cannot
read (no harness trailer, #8214) is part of the note, not a failed measurement. Everything below
describes the measurement, which is unchanged; read "fails" as "prints the advisory note".

THE RULE (the maintainer, Tue 29 Sep 2026, chosen over human approval): a PR that edits the
control plane needs SUBMITTED reviews at its current head from two different
harnesses among Codex, Sol, Claude, Grok and Cursor, neither of them the PR's
authoring harness. Until this module `just review-triage` passed such a PR on
"1 of 3 AVAILABLE", and #4576 (control plane) merged with only Sol's review.
Putting the rule in review-triage makes every path that reads that verdict
enforce it, instead of one more copy in one more caller.

THE AUTHOR SHELLS (Wed 21 Oct 2026, #7421): an author trailer names the harness
that WROTE the commit. Hermes is a builder shell, not one of the five reviewer
lanes, so a -Hermes commit is a measured author that excludes no reviewer: it can
never be UNKNOWN for want of a known name, and it can never exempt a reviewer the
way -Claude exempts Claude. Before this, one -Hermes commit in a PR that also
touched the control plane read "no author trailer" and left REVIEW-13
unmeasurable for that head.

REVIEW-18 (Thu 1 Oct 2026): Grok and Cursor subscription lanes post submitted
reviews under maintainer with lane markers; they count here when login
and marker both match, like Sol's SOL_LOGINS rule in review_sol.py. Codex has no
marker lane here and is excluded from this ambiguity rule. A body with more than
one lane marker among sol, claude, grok, or cursor counts for no harness in
REVIEW-13's dual-review count; duplicate markers of one lane with differing sha,
model, seat, or skipped values are likewise ambiguous and count for no harness.
Claude's marker regex is imported from review_claude.py (same as Sol from review_sol).

REVIEW-13 DEBT-ONLY CARRY (Thu 1 Oct 2026): a review counts at an earlier head when every
commit since changed only `(private path)/<this-pr>.md`, the same rule review_coverage
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
  / A control-plane PR with fewer than 2 independent harness reviews at head fails.
    [if] CLAUDE.md edited by a -Claude author with only a Codex review passes [then] broken
    [if] a -Codex author counts Sol as independent [then] broken
  / A PR that touches no control-plane path is untouched by this rule.
    [if] apps/x.py with one reviewer fails here [then] broken
  / An author that cannot be read is a failed measurement, never a pass.
    [if] a commit with no harness trailer yields PASS [then] broken
  / A missing author trailer names who rewrites it, never a bare note.
    [if] the advisory note for a missing trailer leaves no owner path [then] broken
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
  / Codex issue-comment evidence requires a completed Code Review table row at head.
    [if] Security Review completed at head with Code Review queued still passes [then] broken
"""

from __future__ import annotations

import fnmatch
import functools
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from scripts.review_claude import CLAUDE_MARKER
from scripts.review_control_plane_carry import (
    _NO_CARRY,
    SUBMITTED_REVIEW_STATES,
    CarriedReview,
    Carry,
    debt_only_carry,
    print_carry_proofs,
)
from scripts.review_control_plane_codex import (
    codex_head_tied_issue_comment as _codex_head_tied_issue_comment,
    harness_reviewed_at_head as _harness_reviewed_at_head,
)
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
# REVIEW-24 (ADR-0124, Wed 28 Oct 2026): the root import surface (any root *.py, *.so,
# a root package's __init__, and the stdlib-shadowing dirs) is no longer control plane.
# A merged root module that shadows the stdlib DOES run inside later gate runs; the maintainer
# accepted that risk for one-review merges, and the ADR records the trade-off.
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
    "(private path)*",
    "(private path)/*",
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
    "scripts/worktree_tools.py",
    "tests/scripts/test_enqueue_gate*.py",
    "tests/scripts/enqueue_gate_*.py",
    "tests/scripts/test_review_submitted_state.py",
    "scripts/interpreter_contract.py",
    "scripts/provenance_*.py",
    "(private path)",
    "(private path)",
    "apps/sync/usb/pioneer/fixture_resolver.py",
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
    "(private path)",
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
    "(private path)",
)
#: REVIEW-24: a control-plane file whose diff changes ONLY these declaration lines does
#: not make the PR control plane. A PR registering its own tests in `pytest_scope` needs
#: one reviewer; any other justfile line (the gate recipes) still needs two.
#: The whole line must match: a quoted list of plain test paths, so no just backtick or
#: shell text can ride in on a waived line.
DECLARATION_ONLY_LINES: Mapping[str, re.Pattern[str]] = {
    "justfile": re.compile(r'pytest_scope := "[A-Za-z0-9_./ -]*"'),
}
DUAL_REVIEW_MIN = 2
#: GET /pulls/{n}/commits returns at most 250 commits, and so does GraphQL's
#: `pullRequest.commits` (measured on #3837 Thu 1 Oct 2026: totalCount 428, 250 paged out).
COMMITS_API_CAP = 250
COMPARE_PAGE_SIZE = 100  # GET /compare/{base}...{head} pages its commits, 100 at most per page
#: Authoring harness (commit trailer) -> reviewer harnesses it may not stand in for.
#: Sol is GPT driven through the Codex CLI, so a -Codex author excludes Sol too.
#: Hermes is a builder shell, not a reviewer lane: it posts no review marker, so it stands in
#: for no reviewer and excludes none. A -Hermes commit is measurable rather than UNKNOWN;
#: without this, one Hermes-authored commit in a PR that also touches the control plane made
#: REVIEW-13 read "no author trailer" and blocked a measurement it could have made (#7421 at
#: 820daec9c, Wed 21 Oct 2026).
AUTHOR_EXCLUDES: Mapping[str, frozenset[str]] = {
    "Claude": frozenset({"Claude"}),
    "Codex": frozenset({"Codex", "Sol"}),
    "Cursor": frozenset({"Cursor"}),
    "Grok": frozenset({"Grok"}),
    #: Hermes on bifrost-1 runs DeepSeek ((private path)), which backs no
    #: reviewer lane, so it excludes none; it still needs two independent reviews like any author.
    "Hermes": frozenset(),
}
AUTHOR_MARKER = re.compile(r"-(Claude|Codex|Cursor|Grok|Hermes)[ \t]*")
GIT_TRAILER_LINE = re.compile(r"[A-Za-z][A-Za-z0-9-]*: \S.*")
#: The note `git cherry-pick -x` writes between a message and its trailers. A paragraph of only
#: these is passed over when looking for markers; it attributes nothing itself (#5731, 13a22177a).
GIT_CHERRY_PICK_NOTE = re.compile(r"\(cherry picked from commit [0-9a-f]{40}\)")
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


def verdict_line(ok: bool, head_sha: str, detail: str) -> str:
    """REVIEW-13's verdict line. It names the head because on a FAIL triage exits
    before its own `PR #N @ head` header, and review_chain reads the head from here."""
    outcome = "ok" if ok else "FAIL"
    return f"[review-coverage] {outcome}: control-plane dual review (REVIEW-13) @ head {head_sha}: {detail}"


def advisory_line(head_sha: str, detail: str) -> str:
    """REVIEW-8220's note where REVIEW-13 used to FAIL. It must never start with the FAIL prefix
    or carry the could-not-measure text: the nucbox lander holds a PR on either."""
    return (
        f"[review-coverage] NOTE: control-plane review is advisory (REVIEW-8220) @ head {head_sha}: "
        f"{detail}; consider one review where it helps, one is enough; nothing is held"
    )


@dataclass(frozen=True)
class DualReview:
    ok: bool | None  # None = could not measure
    detail: str
    carried: tuple[CarriedReview, ...] = ()
    #: Harnesses with a review recognized AT HEAD, the author's own included.
    recognized: tuple[str, ...] = ()
    #: Harnesses that count as independent (at head or carried; the author's excluded).
    counted: tuple[str, ...] = ()


class Recognition(NamedTuple):
    """`recognition_line`, read back: who was recognized at head, who counts, how many are needed."""

    recognized: frozenset[str]
    counted: frozenset[str]
    need: int


_RECOGNITION = re.compile(
    r"^\[review-coverage\] REVIEW-13 reviewers: recognized at head (?P<recognized>[^;]*); "
    r"counted as independent (?P<counted>[^;]*); need (?P<need>\d+)$",
    re.M,
)
_NOBODY = "(none)"


def read_recognition(output: str) -> Recognition | None:
    """The last `recognition_line` on review-coverage's output; None when it printed none."""
    found = list(_RECOGNITION.finditer(output))
    if not found:
        return None

    def names(group: str) -> frozenset[str]:
        text = found[-1].group(group).strip()
        return frozenset() if text == _NOBODY else frozenset(n.strip() for n in text.split(","))

    return Recognition(names("recognized"), names("counted"), int(found[-1].group("need")))


@dataclass(frozen=True)
class CommitList:
    commits: Sequence[Mapping] | None  # None = not provably every commit of the PR
    detail: str


def control_plane_hits(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(p for p in set(paths) if any(fnmatch.fnmatchcase(p, g) for g in CONTROL_PLANE_PATHS)))


def declaration_only(entry: Mapping) -> bool:
    """Pure: whether one `pulls/{n}/files` entry changes only REVIEW-24 declaration lines.

    Fails closed: a rename, a file GitHub sent no patch for (too large), or any other
    changed line keeps the file control plane.
    """
    pattern = DECLARATION_ONLY_LINES.get(entry.get("filename", ""))
    patch = entry.get("patch")
    if pattern is None or not patch or entry.get("previous_filename"):
        return False
    # GitHub's `patch` field has no `---`/`+++` file headers, so every +/- line is a change.
    changed = [line[1:] for line in patch.splitlines() if line[:1] in "+-"]
    return bool(changed) and all(pattern.fullmatch(line) for line in changed)


def without_declaration_only(paths: Sequence[str], entries: Callable[[], Sequence[Mapping]]) -> list[str]:
    """`paths` minus files whose diff changes only declaration lines; reads `entries` only when needed."""
    if not set(paths) & set(DECLARATION_ONLY_LINES):
        return list(paths)
    waived = {e["filename"] for e in entries() if declaration_only(e)}
    return [p for p in paths if p not in waived]


def append_author_trailer_owner_path(detail: str) -> str:
    """One-line owner handoff when a commit lacks a readable harness trailer.

    It names the PR author by role, with no GitHub read: under REVIEW-8220 this note is advisory,
    and a lookup that could fail would turn it back into a held, unmeasured verdict."""
    if "has no author trailer" not in detail:
        return detail
    return (
        f"{detail}; owner: the PR author recovers the trailer without a force-push: "
        "cherry-pick the commits onto a fresh branch from main, give that commit a harness "
        "trailer (`-Claude`/`-Codex`/`-Cursor`/`-Grok` on its own line in the final block, real "
        "newlines, never a literal `\\n`) and open a replacement PR, or ask CORE for a "
        "force-push exception (see (private path))"
    )


def author_markers(message: str) -> set[str]:
    """Harness markers from the message's trailer block only (#4361 r4137687862).

    A paragraph made only of exact `git cherry-pick -x` notes is skipped, so a marker above the
    note is still read. An exact note inside a trailer paragraph is also accepted: when the message
    already ends in trailers, git appends the note to that block with no blank line (#7087's
    aae38d8c7). Any other paragraph that is not markers, trailers and notes ends the walk."""
    markers: set[str] = set()
    for paragraph in reversed(re.split(r"\n[ \t]*\n", message.strip())):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if lines and all(GIT_CHERRY_PICK_NOTE.fullmatch(x) for x in lines):
            continue
        if not lines or not all(
            AUTHOR_MARKER.fullmatch(x) or GIT_TRAILER_LINE.fullmatch(x) or GIT_CHERRY_PICK_NOTE.fullmatch(x)
            for x in lines
        ):
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
    """Pure verdict. `reviewed_at_head[name]`: that harness left a review at head.

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
        f"{', '.join(counted) or '(none)'} (REVIEW-13 asked for {DUAL_REVIEW_MIN}; excluded as author: "
        f"{', '.join(sorted(excluded)) or '(none)'})"
    )
    if carry.unknown and not ok:
        detail += f"; carry UNKNOWN ({'; '.join(carry.unknown)}), so a review at head is required"
    used = tuple(carried[n] for n in counted_names if n in carried)
    recognized = tuple(sorted(n for n, reviewed in reviewed_at_head.items() if reviewed))
    return DualReview(ok, detail, used, recognized, tuple(counted_names))


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


def enforce(pr: str, head_sha: str, changed_files: Sequence[str]) -> bool:
    """review_coverage.triage's hook: prints the `ok` line or an advisory NOTE, and never holds.

    Returns whether REVIEW-13's old bar (two independent harness reviews) was met, for tests and
    callers that report it; no caller may hold a PR on False.

    REVIEW-8220: fewer than two independent reviews, or authors that cannot be read (no
    harness trailer), is a NOTE, not a FAIL and not a failed measurement. A failed GitHub
    read still raises TriageError. review_coverage is imported here, not at the top, because
    it imports this module; reading `_paginated_json_list` through it keeps the one fetch
    seam its tests already use.
    """
    from scripts import review_coverage as rc

    def files_at_head() -> list[dict]:
        # The files API describes the LIVE head: fail closed if it is not the pinned one.
        entries = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/files")
        rc._require_head_unchanged(head_sha, rc._head_sha(pr))
        return entries

    changed_files = without_declaration_only(changed_files, files_at_head)
    cp_hits = control_plane_hits(changed_files)
    repo_root: Path = rc.CHECKOUT_ROOT

    @functools.cache
    def payloads() -> tuple[list[dict], list[dict], list[dict]]:
        reviews = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/reviews")
        inline = rc._paginated_json_list(f"repos/{rc.REPO}/pulls/{pr}/comments")
        issue = rc._paginated_json_list(f"repos/{rc.REPO}/issues/{pr}/comments")
        # SUBMITTED reviews count at head and when carried; Codex may also count a clean-pass
        # issue comment tied to head via review_coverage._collect_evidence. A DISMISSED or
        # PENDING Codex, Sol or Claude review at an earlier head must not carry (#4876).
        unambiguous_reviews = [
            review
            for review in reviews
            if str(review["state"]).upper() in SUBMITTED_REVIEW_STATES
            and not _subscription_review_body_ambiguous(_review_body_text(review))
        ]
        return unambiguous_reviews, inline, issue

    def reviewed_at(sha: str) -> dict[str, bool]:
        unambiguous_reviews, inline, issue = payloads()
        found = {}
        for name in rc.EXPECTED_REVIEWERS:
            evidence = rc._collect_evidence(name, unambiguous_reviews, inline, issue, sha)
            reviewed = rc._classify_from_evidence(name, evidence).reviewed
            found[name] = _harness_reviewed_at_head(name, evidence, reviewed, issue, sha)
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
    if not control_plane_hits(changed_files):
        return True
    rc._require_head_unchanged(head_sha, rc._head_sha(pr))
    if verdict.ok:
        print(verdict_line(True, head_sha, verdict.detail))
        print_carry_proofs(verdict.carried, head_sha)
        return True
    print(advisory_line(head_sha, append_author_trailer_owner_path(verdict.detail)))
    print_carry_proofs(verdict.carried, head_sha)
    return False
