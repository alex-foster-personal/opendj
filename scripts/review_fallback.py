"""Reviewer fallback: run the Sol lane, then the Claude lane, when Codex cannot review.

WHY THIS EXISTS. Codex, Sol and Claude are ALTERNATIVE reviewers (CLAUDE.md,
REVIEWER COVERAGE), but only Codex runs by itself: the GitHub app answers every
push. When its quota is spent it answers with a usage-limit notice instead of a
review, and the PR sits at coverage MISS until somebody remembers to type
`just sol-review <PR>`. the maintainer, Wed 30 Sep 2026: "We should have automated
fallback tbh when accounts max out." This module is that somebody. It invokes
the existing lanes and changes nothing about them: every size limit, seat
rule and idempotence guard stays in scripts/sol_review.py and
scripts/claude_review.py.

THE RULE, decided by the pure function `decide`:

  COVERED           Codex reviewed the current head (a submitted review at the
                    head, or its summary row marked Completed for the head), or
                    the lane this run invoked exited 0.
  WAIT              Codex has neither reviewed nor posted its notice, and the
                    head was pushed less than CFG.CODEX_GRACE ago.
  RUN_SOL           Codex posted its usage-limit notice AFTER the head was
                    pushed, or stayed silent past the grace window.
  REFUSED_OVERSIZE  RUN_SOL would apply, but the diff is over the lanes' own
                    MAX_DIFF_BYTES, so both lanes would refuse. A human splits
                    the PR; nothing is loosened here.
  RUN_CLAUDE        Sol exited 3 (it could not review, including every seat
                    spent), and Claude has not been tried at this head.
  EXHAUSTED         Claude was already tried at this head, or it just exited 3.
                    Once per head is CLAUDE.md's rule, because a Claude run
                    bills the maintainer's own allowance; this module keeps a local
                    per-head attempt record so a 15-minute sweep cannot loop it.

HOW A NOTICE IS TIED TO A HEAD. Codex's notice is an issue comment and carries
no SHA, so it is dated instead: it counts only when created at or after the
head's push time. The push time is the earliest check suite GitHub created for
the head SHA, which is when GitHub received the push. The commit's own
committer date is NOT used: it predates the push (PR #4538, commit 21:13:33Z,
suites 21:14:16Z, notice 21:14:22Z), and a notice for the previous head that
lands in that gap would otherwise be read as a notice for this one.

Requirements (mini-PRD):
  / A notice posted after the current head was pushed runs Sol.
    [if] the notice sits 6 s after the push and the action is WAIT [then] broken
  / A notice left for an earlier head does not run Sol inside the grace window.
    [if] a notice dated before the push yields RUN_SOL [then] broken
  / Silence past the grace window runs Sol; silence inside it waits.
    [if] 19 min of silence runs Sol, or 21 min waits [then] broken
  / Claude runs only after Sol exits 3, and at most once per head.
    [if] Sol exit 0 runs Claude, or a head Claude already tried runs it again [then] broken
  / An oversize diff is refused with a named reason, never trimmed.
    [if] a diff over the lanes' MAX_DIFF_BYTES yields RUN_SOL [then] broken
  / A gh failure is a failed measurement (exit 3), never "no notice found".
    [if] a gh error yields WAIT or RUN_SOL [then] broken

Usage:
    python -m scripts.review_fallback <PR> [--dry-run]
    python -m scripts.review_fallback --sweep [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

try:
    from scripts.review_gh import TriageError, _body_is_at_head, _gh, _paginated_json_list
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.review_fallback") from None
    raise
from scripts import claude_review, sol_review
from scripts.review_gate_freshness import CHECKOUT_ROOT
from scripts.review_lane import REPO, pinned_head, reviewable_diff

TAG = "[review-fallback]"


class CFG:
    """Everything a reader needs to see to predict what a run will do."""

    #: The Codex GitHub app's login, compared after stripping `[bot]`.
    CODEX_LOGIN: str = "chatgpt-codex-connector"
    #: Codex's own wording, captured verbatim on PR #4538 (Wed 30 Sep 2026).
    USAGE_LIMIT_NOTICE: str = "You have reached your Codex usage limits for code reviews"
    #: How long Codex may stay silent after a push before Sol runs. Codex
    #: answered #4538 in 6 s and reviewed #4515 heads within 4 min.
    CODEX_GRACE: timedelta = timedelta(minutes=20)
    #: The lanes' own ceiling, READ from them rather than restated, so this
    #: module cannot drift from the limit that would actually refuse.
    MAX_DIFF_BYTES: int = min(sol_review.CFG.MAX_DIFF_BYTES, claude_review.CFG.MAX_DIFF_BYTES)
    #: The exit code both lanes use for "could not review" (REVIEW-03).
    LANE_COULD_NOT_REVIEW: int = 3
    #: sol_review's own words when every seat refused (`review_with_codex`).
    SOL_ALL_SEATS_SPENT: str = "no seat accepted this review"
    #: Per-head Claude attempt records. Host-local on purpose: the sweep runs
    #: on one seat-holding host, and a record that must be fetched from
    #: GitHub first is one more call that can fail open.
    STATE_DIR: Path = Path.home() / ".cache" / "opendj" / "review-fallback"
    #: `gh pr list` ceiling. Reaching it means the listing may be truncated,
    #: which is an error, never a complete sweep.
    PR_LIST_LIMIT: int = 500
    #: Stamped on each lane child so a stray one can be attributed.
    SERVICE_ID: str = "com.opendj.review-fallback"


class FallbackError(TriageError):
    """The fallback could not decide or act. Never rendered as a verdict."""


# ----- the decision (pure) --------------------------------------------------


class Action(StrEnum):
    WAIT = "WAIT"
    RUN_SOL = "RUN_SOL"
    RUN_CLAUDE = "RUN_CLAUDE"
    COVERED = "COVERED"
    REFUSED_OVERSIZE = "REFUSED_OVERSIZE"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class CodexArtifact:
    """One thing the Codex app left on the PR, in the shape `decide` reads."""

    kind: Literal["review", "comment"]
    at: datetime
    body: str
    #: The push a submitted review was left against; issue comments have none.
    commit_id: str | None = None


@dataclass(frozen=True)
class HeadState:
    head_sha: str
    pushed_at: datetime
    codex: tuple[CodexArtifact, ...]
    diff_bytes: int


@dataclass(frozen=True)
class LaneResult:
    exit_code: int
    all_seats_spent: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def _is_notice(artifact: CodexArtifact) -> bool:
    return CFG.USAGE_LIMIT_NOTICE.lower() in artifact.body.lower()


def _codex_reviewed(state: HeadState) -> bool:
    """A review left against the head, or the summary row Completed for it."""
    return any(
        not _is_notice(a)
        and (
            a.commit_id == state.head_sha
            if a.kind == "review"
            else _body_is_at_head(a.body, state.head_sha)
        )
        for a in state.codex
    )


def _notice_at_head(state: HeadState) -> CodexArtifact | None:
    """A notice that answered THIS head: by SHA for a review, by date for a comment."""
    return next(
        (
            a
            for a in state.codex
            if _is_notice(a)
            and (a.commit_id == state.head_sha if a.kind == "review" else a.at >= state.pushed_at)
        ),
        None,
    )


def _lane_reviewed(lane: str, result: LaneResult) -> bool:
    if result.exit_code == 0 and not result.all_seats_spent:
        return True
    elif result.exit_code == CFG.LANE_COULD_NOT_REVIEW or result.all_seats_spent:  # noqa: RET505 - outcomes stay explicit.
        return False
    raise FallbackError(f"{lane} exited {result.exit_code}, which is neither 0 nor 3")


def _after_lanes(
    short: str, sol: LaneResult, claude_attempted: bool, claude: LaneResult | None
) -> Decision:
    """What follows a Sol run, and the Claude run after it if there was one."""
    if claude is not None:
        if _lane_reviewed("claude-review", claude):
            return Decision(Action.COVERED, f"Claude reviewed {short}")
        return Decision(
            Action.EXHAUSTED,
            f"Claude could not review {short} (exit {claude.exit_code}); tried once, "
            "not retried for this head",
        )
    if _lane_reviewed("sol-review", sol):
        return Decision(Action.COVERED, f"Sol reviewed {short}")
    why = "every Sol seat spent" if sol.all_seats_spent else f"Sol exited {sol.exit_code}"
    if claude_attempted:
        return Decision(Action.EXHAUSTED, f"{why}, and Claude was already tried at {short}")
    return Decision(Action.RUN_CLAUDE, f"{why}; Claude is the last resort for {short}")


def _before_lanes(state: HeadState, now: datetime) -> Decision:
    """What Codex's own artifacts, the clock and the diff size call for."""
    short = state.head_sha[:10]
    if _codex_reviewed(state):
        return Decision(Action.COVERED, f"Codex reviewed {short}")
    notice = _notice_at_head(state)
    silent_for = now - state.pushed_at
    if notice is not None:
        trigger = f"Codex posted its usage-limit notice at {notice.at:%a %d %b %H:%MZ} for {short}"
    elif silent_for >= CFG.CODEX_GRACE:
        trigger = f"Codex silent {int(silent_for.total_seconds() // 60)} min after {short}"
    else:
        left = int((CFG.CODEX_GRACE - silent_for).total_seconds() // 60)
        return Decision(Action.WAIT, f"Codex has {left} min of grace left on {short}")
    if state.diff_bytes > CFG.MAX_DIFF_BYTES:
        return Decision(
            Action.REFUSED_OVERSIZE,
            f"{trigger}, but the diff is {state.diff_bytes} bytes, over the lanes' "
            f"{CFG.MAX_DIFF_BYTES} limit; split the PR, the limit is not loosened",
        )
    return Decision(Action.RUN_SOL, trigger)


def decide(
    state: HeadState,
    now: datetime,
    *,
    sol: LaneResult | None = None,
    claude_attempted: bool = False,
    claude: LaneResult | None = None,
) -> Decision:
    """What to do next for this head. Pure: every input is an argument.

    `sol` is the Sol run this invocation made, `claude` the Claude run after
    it, and `claude_attempted` whether Claude was already tried at this head
    by any earlier invocation.
    """
    if now.tzinfo is None or state.pushed_at.tzinfo is None:
        raise FallbackError("timestamps must be timezone-aware UTC")
    if state.pushed_at > now:
        raise FallbackError(f"push time {state.pushed_at} is after now {now}; clock skew")
    if sol is not None:
        return _after_lanes(state.head_sha[:10], sol, claude_attempted, claude)
    elif claude is not None:  # noqa: RET505 - inputs stay explicit.
        raise FallbackError("a Claude result without the Sol run before it")
    return _before_lanes(state, now)


# ----- payload parsing (pure) -----------------------------------------------


def _utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        raise FallbackError(f"timestamp {stamp!r} carries no zone")
    return parsed.astimezone(UTC)


def _is_codex(payload: dict) -> bool:
    login = (payload.get("user") or {}).get("login", "")
    return login.removesuffix("[bot]").lower() == CFG.CODEX_LOGIN


def codex_artifacts(reviews: list[dict], comments: list[dict]) -> tuple[CodexArtifact, ...]:
    """Codex's reviews and issue comments, from GitHub's REST payload shapes."""
    found = [
        CodexArtifact("review", _utc(r["submitted_at"]), r.get("body") or "", r["commit_id"])
        for r in reviews
        if _is_codex(r)
    ]
    found += [
        CodexArtifact("comment", _utc(c["created_at"]), c.get("body") or "")
        for c in comments
        if _is_codex(c)
    ]
    return tuple(found)


def pushed_at_from_check_suites(pages: Iterable[dict]) -> datetime:
    """Earliest check suite GitHub created for the SHA: when the push arrived."""
    stamps = [_utc(s["created_at"]) for page in pages for s in page["check_suites"]]
    if not stamps:
        raise FallbackError("no check suite exists for the head, so its push time is unknown")
    return min(stamps)


# ----- measurement (gh) -----------------------------------------------------


def codex_artifacts_on(pr: str) -> tuple[CodexArtifact, ...]:
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    comments = _paginated_json_list(f"repos/{REPO}/issues/{pr}/comments")
    return codex_artifacts(reviews, comments)


def pushed_at(sha: str) -> datetime:
    endpoint = f"repos/{REPO}/commits/{sha}/check-suites"
    return pushed_at_from_check_suites(json.loads(_gh(["api", endpoint, "--paginate", "--slurp"])))


def measure_head(pr: str) -> HeadState:
    sha = pinned_head(pr)
    codex = codex_artifacts_on(pr)
    pushed = pushed_at(sha)
    diff, _ = reviewable_diff(pr)
    _require_head(pr, sha)
    return HeadState(sha, pushed, codex, len(diff.encode()))


def _require_head(pr: str, sha: str) -> None:
    current = pinned_head(pr)
    if current != sha:
        raise FallbackError(f"PR #{pr} moved from {sha} to {current}; re-run on the new head")


def coverage_misses(pr: str) -> bool:
    """Ask scripts/review_coverage.py, never a re-implementation of it."""
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.review_coverage", pr],
        capture_output=True,
        text=True,
        cwd=CHECKOUT_ROOT,
        check=False,
    )
    if proc.returncode == 0:
        return False
    elif proc.returncode == 1:  # noqa: RET505 - coverage exits stay explicit.
        return True
    raise FallbackError(
        f"review_coverage #{pr} exited {proc.returncode}: {(proc.stdout + proc.stderr)[-600:]}"
    )


def open_ready_prs() -> list[str]:
    fields = ["--state", "open", "--json", "number,isDraft", "--limit", str(CFG.PR_LIST_LIMIT)]
    prs = json.loads(_gh(["pr", "list", "--repo", REPO, *fields]))
    if len(prs) >= CFG.PR_LIST_LIMIT:
        raise FallbackError(f"gh pr list hit its {CFG.PR_LIST_LIMIT} limit; listing may be cut")
    return [str(p["number"]) for p in prs if not p["isDraft"]]


# ----- acting ---------------------------------------------------------------


def _run_lane(module: str, pr: str) -> tuple[int, str]:
    """Run a lane exactly as its just recipe does, streaming its output."""
    proc = subprocess.Popen(
        [sys.executable, "-m", module, pr],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=CHECKOUT_ROOT,
        env={**os.environ, "AF_SERVICE_ID": CFG.SERVICE_ID},
    )
    if proc.stdout is None:
        raise FallbackError(f"{module} started with no stdout pipe")
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return proc.wait(), "".join(lines)


def _claude_record(pr: str, sha: str) -> Path:
    return CFG.STATE_DIR / f"pr{pr}-{sha}.claude-attempted"


def claim_claude_attempt(record: Path) -> bool:
    """Create `record` exclusively; False when another sweep already holds it.

    O_EXCL makes check and create one atomic step, so two concurrent sweeps at
    the same head cannot both pass an existence check and both spend a Claude
    review. The claim is taken BEFORE the run: a crash mid-run still counts as
    the one try."""
    record.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(record, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{datetime.now(UTC).isoformat()}\n")
    return True


def _report(pr: str, decision: Decision) -> Decision:
    print(f"{TAG} #{pr} {decision.action}: {decision.reason}", flush=True)
    return decision


def process_pr(pr: str, dry_run: bool) -> Decision:
    if not coverage_misses(pr):
        return _report(pr, Decision(Action.COVERED, "review_coverage passes at head"))
    state = measure_head(pr)
    now = datetime.now(UTC)
    decision = _report(pr, decide(state, now))
    if decision.action != Action.RUN_SOL or dry_run:
        return decision
    _require_head(pr, state.head_sha)
    code, output = _run_lane("scripts.sol_review", pr)
    sol = LaneResult(code, CFG.SOL_ALL_SEATS_SPENT in output)
    record = _claude_record(pr, state.head_sha)
    decision = _report(pr, decide(state, now, sol=sol, claude_attempted=record.exists()))
    if decision.action != Action.RUN_CLAUDE:
        return decision
    _require_head(pr, state.head_sha)
    if not claim_claude_attempt(record):
        return _report(pr, decide(state, now, sol=sol, claude_attempted=True))
    code, _ = _run_lane("scripts.claude_review", pr)
    claude = LaneResult(code)
    return _report(pr, decide(state, now, sol=sol, claude_attempted=True, claude=claude))


# ----- CLI ------------------------------------------------------------------

#: Exit per final action: 0 nothing more to do now, 1 the head stays uncovered
#: and needs a human. 3 (could not measure) comes from a raised error, never
#: from this table.
EXIT_FOR: dict[Action, int] = {
    Action.COVERED: 0,
    Action.WAIT: 0,
    Action.RUN_SOL: 0,
    Action.RUN_CLAUDE: 0,
    Action.REFUSED_OVERSIZE: 1,
    Action.EXHAUSTED: 1,
}


def exit_code(decision: Decision) -> int:
    return EXIT_FOR[decision.action]


def sweep(dry_run: bool) -> int:
    worst = 0
    prs = open_ready_prs()
    print(f"{TAG} sweep: {len(prs)} open non-draft PR(s)", flush=True)
    for pr in prs:
        try:
            worst = max(worst, exit_code(process_pr(pr, dry_run)))
        except TriageError as exc:
            # One PR's failed measurement must not hide the rest of the sweep,
            # and it is never folded into a verdict: the sweep exits 3.
            print(f"{TAG} #{pr} COULD NOT MEASURE: {exc}", file=sys.stderr, flush=True)
            worst = 3
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review-fallback", description=__doc__)
    parser.add_argument("pr", nargs="?")
    parser.add_argument("--sweep", action="store_true", help="every open non-draft PR")
    parser.add_argument("--dry-run", action="store_true", help="decide and print, run no lane")
    args = parser.parse_args(argv)
    if bool(args.pr) == args.sweep:
        parser.error("give exactly one of a PR number or --sweep")
    try:
        if args.sweep:
            return sweep(args.dry_run)
        return exit_code(process_pr(args.pr, args.dry_run))
    except TriageError as exc:
        print(f"{TAG} COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
