"""Subscription review lane: run Grok or Cursor over a PR diff through the
af-sub-broker and post the findings as review threads.

The configured reviewer chain may try an independent subscription provider when
an earlier lane cannot review. This lane invokes Grok or Cursor through the broker.

ONE LANE, PARAMETERIZED BY PROVIDER. The mechanics are `review_lane.py`,
shared with Sol and Claude and not copied: head pinning, diff fetch, the
generated-data split, anchoring, the reply contract, posting. The prompt and
rubric are `review_prompt.py`, the same text Sol and Claude are asked. What is
here is only what differs: reaching the model through `sb run`, and reading
back what the broker says it ran.

BILLING. `sb run --provider P --model M --access read` bills the named
subscription. The broker refuses a metered model (Cursor's non-house pool)
unless `--allow-metered` is passed, and this lane never passes it. The
RunResult's own `metered` flag is checked as well, and a metered run is
refused before anything is posted: never OpenRouter, never an API key, never
paid extra usage.

Requirements (mini-PRD):
  / Post one review thread per finding, verdict and P-level leading the body.
    [if a finding posts without a P-level or verdict then broken]
  / Refuse a diff over the cap rather than certify part of it.
    [if an oversized diff posts a current-head marker then broken]
  / Skip a head that already carries this lane's review, unless forced.
    [if re-running doubles the threads then broken]
  / A run that could not review exits 3, never 0.
    [if a broker refusal, a timeout or an unparseable reply exits 0 then broken]
  / Never post a metered run.
    [if a RunResult with metered=true reaches post_review then broken]
  / Report the model the broker saw, or say it was only requested.
    [if the marker names a model no record supports, unflagged, then broken]

Usage:
    python -m scripts.subscription_review <grok|cursor> <PR> [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

try:
    from scripts.review_gh import TriageError, _gh, _paginated_json_list
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.subscription_review") from None
    raise
from scripts.review_lane import (
    BADGE_COLORS,
    REPO,
    Finding,
    anchorable_lines,
    parse_findings,
    pinned_head,
    post_review,
    reviewable_diff,
    split_by_anchor,
    unreviewed_note,
    withheld,
)
from scripts.review_prompt import ReviewPromptConfig, build_prompt
from scripts.review_subscription import LANES_BY_SLUG, SubscriptionLane


class CFG:
    """Everything a reader needs to see to predict what a run will do."""

    #: Configured broker CLI.
    SB: str = "sb"
    #: Same cap as the Sol and Claude lanes: over it the run REFUSES, because a
    #: review of part of a diff must not certify the whole push.
    MAX_DIFF_BYTES: int = 320_000
    #: Findings posted per run, same as the other lanes.
    MAX_FINDINGS: int = 12
    #: Broker wall clock for one run, and this process's own (a little longer,
    #: so the broker reports its timeout rather than being killed mid-write).
    RUN_TIMEOUT_S: int = 1800
    PROCESS_TIMEOUT_S: int = 1900
    #: Prefix of a model id that says the broker did not report one (Cursor's
    #: adapter never does). Written into the marker so the gap is visible.
    REQUESTED_ONLY_PREFIX: str = "requested:"


def _tag(lane: SubscriptionLane) -> str:
    return f"[{lane.slug}-review]"


def _existing_review(lane: SubscriptionLane, pr: str, sha: str) -> bool:
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    return any(
        lane.is_artifact((r.get("user") or {}).get("login", ""), r.get("body") or "", sha)
        for r in reviews
    )


# ----- the model ----------------------------------------------------------


def sb_argv(lane: SubscriptionLane, pr: str, prompt_file: Path, cwd: Path) -> list[str]:
    """The broker invocation: read-only, explicit provider and model, attributed.

    `--cwd` is an EMPTY scratch directory, for the reason the Sol lane runs
    from /tmp: the diff arrives in the prompt, and a reviewer reading a working
    tree would be reading a tree that is not the PR's. `--prompt-file` rather
    than stdin because `sb run --prompt-file -` does not read stdin (learning,
    Thu 1 Oct 2026). `--json` returns the RunResult, which names `metered` and
    `models_used`; `--text` would drop both.
    """
    return [
        CFG.SB,
        "run",
        "--provider",
        lane.provider,
        "--model",
        lane.model,
        "--access",
        "read",
        "--cwd",
        str(cwd),
        "--prompt-file",
        str(prompt_file),
        "--timeout-s",
        str(CFG.RUN_TIMEOUT_S),
        "--agent",
        f"{lane.slug}-review",
        "--pr",
        pr,
        "--task-class",
        "review",
        "--json",
    ]


def read_run_result(lane: SubscriptionLane, stdout: str) -> tuple[str, str]:
    """(reply text, model id for the marker) from the broker's RunResult JSON.

    Refuses anything that is not a successful, subscription-billed run with a
    non-empty reply. The model is what the broker REPORTED; when it reports
    none (Cursor), the requested model is written with an explicit
    `requested:` prefix, so the marker never claims a model nobody confirmed.
    """
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise TriageError(f"sb printed no RunResult JSON: {stdout[-400:]!r}") from exc
    if result.get("metered") is not False:
        raise TriageError(
            f"sb reports metered={result.get('metered')!r} for {lane.provider}/{lane.model}; "
            "a subscription review must bill the subscription, so nothing was posted"
        )
    parsed = result.get("parsed") or {}
    reply = str(parsed.get("text") or "")
    if not reply.strip():
        raise TriageError(f"{lane.name} returned an empty reply; nothing was reviewed")
    models = [str(m) for m in parsed.get("models_used") or []]
    model = "+".join(models) if models else f"{CFG.REQUESTED_ONLY_PREFIX}{lane.model}"
    return reply, model


def _failure_reason(proc: subprocess.CompletedProcess[str]) -> str:
    """The broker's own `[ERROR]` line when it printed one, else the stderr tail."""
    errors = [line for line in proc.stderr.splitlines() if line.startswith("[ERROR]")]
    return errors[-1] if errors else (proc.stderr.strip() or proc.stdout.strip())[-600:]


def review_with_broker(lane: SubscriptionLane, pr: str, prompt: str) -> tuple[str, str]:
    """Run one review through `sb`. Returns (reply, model id). Raises TriageError."""
    with tempfile.TemporaryDirectory(prefix=f"{lane.slug}-review-") as scratch:
        root = Path(scratch)
        prompt_file = root / "prompt.md"
        prompt_file.write_text(prompt)
        cwd = root / "cwd"
        cwd.mkdir()
        try:
            proc = subprocess.run(
                sb_argv(lane, pr, prompt_file, cwd),
                capture_output=True,
                text=True,
                timeout=CFG.PROCESS_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TriageError(f"`{CFG.SB}` is not on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TriageError(
                f"sb did not answer within {CFG.PROCESS_TIMEOUT_S}s; nothing was reviewed"
            ) from exc
    if proc.returncode != 0:
        raise TriageError(
            f"sb run --provider {lane.provider} exited {proc.returncode}: {_failure_reason(proc)}"
        )
    return read_run_result(lane, proc.stdout)


# ----- posting ------------------------------------------------------------


def summary_body(
    lane: SubscriptionLane,
    sha: str,
    model: str,
    findings: list[Finding],
    carried: list[Finding],
    unreviewed: list[str],
) -> str:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in sorted(BADGE_COLORS)}
    tally = ", ".join(f"{s} {n}" for s, n in counts.items() if n)
    noun = "finding" if len(findings) == 1 else "findings"
    head = f"{lane.name} review of `{sha[:10]}`: {len(findings)} {noun}" + (
        f" ({tally})." if tally else "."
    )
    lines = [
        head,
        "",
        f"`{model}` via the af-sub-broker on the {lane.provider} subscription, a step in the "
        "reviewer failover chain (`just review`, scripts/review_chain.py).",
        "",
        "P0/P1 findings are BLOCKING and must be FIXED or REBUTTED before merge. "
        "P2/P3 are NON-BLOCKING. Every thread still needs one of the three terminal "
        "states (see AGENTS.md).",
    ]
    if carried:
        lines += ["", "Findings with no postable line anchor, kept here rather than dropped:"]
        for f in carried:
            where = f"`{f.path}`" + (f":{f.line}" if f.line else "")
            lines += [f"- {f.verdict} {f.severity}: {where} -- {withheld(f.title)}"]
            if f.detail:
                lines += [f"  {withheld(f.detail)}"]
    lines += unreviewed_note(unreviewed)
    lines += ["", lane.marker(sha, model, skipped=frozenset(unreviewed))]
    return "\n".join(lines)


# ----- CLI ----------------------------------------------------------------


def run(lane: SubscriptionLane, pr: str, dry_run: bool, force: bool) -> int:
    tag = _tag(lane)
    sha = pinned_head(pr)
    print(f"{tag} PR #{pr} pinned at {sha}")
    if not force and _existing_review(lane, pr, sha):
        print(f"{tag} a {lane.name} review already exists for {sha}; nothing to do (--force to redo)")
        return 0
    title = _gh(["pr", "view", pr, "--repo", REPO, "--json", "title", "-q", ".title"]).strip()
    diff, unreviewed = reviewable_diff(pr)
    if unreviewed:
        print(f"{tag} not reviewed, generated data: {', '.join(unreviewed)}")
    if len(diff.encode()) > CFG.MAX_DIFF_BYTES:
        raise TriageError(
            f"PR #{pr}'s diff is {len(diff.encode())} bytes, over the "
            f"{CFG.MAX_DIFF_BYTES} limit. A partial review must not certify the whole "
            "push, so nothing was run. Split the PR or review it another way."
        )
    run_id = uuid.uuid4().hex[:12]
    prompt, _ = build_prompt(
        pr,
        sha,
        title,
        diff,
        ReviewPromptConfig(
            run_id=run_id,
            max_diff_bytes=CFG.MAX_DIFF_BYTES,
            max_findings=CFG.MAX_FINDINGS,
            fence=lane.fence,
            withheld=unreviewed,
        ),
    )
    reply, model = review_with_broker(lane, pr, prompt)
    findings = parse_findings(reply, run_id, lane.fence, CFG.MAX_FINDINGS)
    print(f"{tag} provider={lane.provider} model={model} findings={len(findings)}")
    for finding in findings:
        anchor = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"  {finding.verdict} {finding.severity} {anchor} -- {finding.title}")
    if dry_run:
        print(f"{tag} dry run; nothing posted")
        return 0
    inline, carried = split_by_anchor(findings, anchorable_lines(diff))
    lane_marker = lane.marker(sha, model, skipped=frozenset(unreviewed))
    summary = summary_body(lane, sha, model, findings, carried, unreviewed)
    print(f"{tag} {len(carried)} finding(s) carried in the summary, none dropped")
    print(f"{tag} {post_review(pr, sha, summary, inline, lane_marker, tag)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subscription-review", description=__doc__)
    parser.add_argument("lane", choices=sorted(LANES_BY_SLUG))
    parser.add_argument("pr")
    parser.add_argument("--dry-run", action="store_true", help="review and print, post nothing")
    parser.add_argument("--force", action="store_true", help="re-review a head already covered")
    args = parser.parse_args(argv)
    lane = LANES_BY_SLUG[args.lane]
    try:
        return run(lane, args.pr, args.dry_run, args.force)
    except TriageError as exc:
        # Exit 3, never 1 and never 0: "I could not review" must not be
        # readable as "I reviewed and found nothing".
        print(f"{_tag(lane)} FAILED: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
