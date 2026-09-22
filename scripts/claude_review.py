"""Claude review lane: run Claude over a PR diff and post the findings as
review threads, when neither Codex nor Sol can.

This is an independent configured review lane when another reviewer cannot run.
It preserves the same coverage contract and refuses unavailable reviews.

The mechanics are `review_lane.py`, shared with the Sol lane and not copied:
head pinning, diff fetch, anchoring, the reply contract, posting. What is
here is only what differs -- reaching Claude, reading what it charged, and
the wording of the summary this reviewer signs.

WHAT MAKES THIS A REVIEW AND NOT A COMMENT-SPRAYER (each rule enforced in
`review_lane`, restated here because a reader of this file needs them):

  Pinned head, taken from `git ls-remote` and confirmed against the PR object,
  and re-read after the model run so a push mid-review cannot be certified.

  Idempotent per head. A Claude review already present for this exact SHA
  means the work is done; re-running posts nothing. Run it ONCE per head.

  Severity drives the verdict, mechanically. The model chooses P0-P3;
  `review_lane.Finding` derives BLOCKING/NON-BLOCKING from it. Letting the
  model emit both halves independently means the day it writes "P1
  NON-BLOCKING" the merge gate tiers a blocker as debt-loggable.

  A partial review may not certify a whole push, so an oversized diff is a
  refusal, not a truncation.

EXIT CODES ARE THE POINT OF THIS LANE'S FAILURE PATH. 0 means a review
happened, including a review that found nothing. 3 means the review could NOT
be performed -- quota, CLI error, oversized diff, a reply that broke the
contract -- and it is deliberately distinct so a caller can never read "I
could not look" as "I looked and found no problems".

Requirements (mini-PRD):
  / Post one review thread per finding, verdict and P-level leading the body.
    [if a finding posts without a P-level or verdict then broken]
  / Derive the verdict from the severity, never from the model's own wording.
    [if a P1 can post as NON-BLOCKING then broken]
  / Skip a head that already carries a Claude review, unless forced.
    [if re-running doubles the threads then broken]
  / A run that could not review exits 3, never 0.
    [if a quota wall or a CLI error exits 0 then broken]
  / Report the model that actually answered, never the alias asked for.
    [if the marker names `opus` rather than the resolved model id then broken]
  / Authenticate the child via an env token or a keychain login, never
    neither, and never invent one when both are missing.
    [if a Mac with only a keychain login refuses before the CLI then broken]
    [if both an env token and a keychain login are absent then a run must
    refuse before the CLI, naming both]

Usage:
    python -m scripts.claude_review <PR> [--model opus] [--effort high]
                                         [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid

try:
    from scripts.review_gh import TriageError, _gh, _paginated_json_list
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.claude_review") from None
    raise
from scripts.review_claude import is_claude_artifact, marker
from scripts.review_debt_match import add_suppressed_summary
from scripts.review_lane import (
    BADGE_COLORS,
    REPO,
    Finding,
    anchorable_lines,
    debt_logged_findings,
    parse_findings,
    pinned_head,
    post_review,
    reviewable_diff,
    split_by_anchor,
    unreviewed_note,
    withheld,
)
from scripts.review_ledger import LedgerReadError, debt_text_at
from scripts.review_prompt import CLAUDE_FENCE, ReviewPromptConfig, build_prompt

TAG = "[claude-review]"


class CFG:
    """Everything a reader needs to see to predict what a run will do."""

    #: Model alias handed to the CLI. An ALIAS, not a pinned id, because the
    #: CLI resolves it against the account's current entitlements; the id that
    #: actually answered is read back out of the run and is what the marker
    #: records. Opus because a reviewer that misses a P0 costs more than the
    #: tokens a cheaper tier saves.
    MODEL: str = "opus"
    #: Bounded reasoning effort for the configured diff-reading task.
    EFFORT: str = "high"
    #: Diff bytes handed to the model. Over this the run REFUSES rather than
    #: truncating, because a review of part of a diff must not certify the
    #: whole push.
    MAX_DIFF_BYTES: int = 320_000
    #: Findings posted per run. A reviewer that opens 60 threads is not read.
    MAX_FINDINGS: int = 12
    #: Finite timeout for one review, including a large diff at high effort.
    CLAUDE_TIMEOUT_S: int = 1800
    #: Usage-limit refusal markers. An unavailable review exits 3, never clean 0.
    SPENT_MARKERS: tuple[str, ...] = (
        "usage limit reached",
        "hit your usage limit",
        "exceeded your account's rate limit",
        "insufficient credit",
    )
    #: Strip alternate API billing credentials and gateways from the child.
    #: This lane uses its configured subscription authentication; parent-shell
    #: exports must not select another billing route implicitly.
    STRIPPED_ENV: tuple[str, ...] = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    )
    #: Strip only session-scoped plumbing from the parent Claude process.
    #: Keep the credential itself: CLAUDE_CODE_OAUTH_TOKEN is not session state.
    #: The explicit denylist avoids treating every CLAUDE_CODE_ name as plumbing.
    STRIPPED_ENV_NAMES: tuple[str, ...] = (
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_PID",
    )
    #: Prefixes of session-scoped variables stripped from the child: the
    #: session id and the messaging socket/token, never anything else under
    #: `CLAUDE_CODE_` -- in particular never `CLAUDE_CODE_OAUTH_TOKEN`.
    STRIPPED_ENV_PREFIXES: tuple[str, ...] = (
        "CLAUDE_CODE_SESSION",
        "CLAUDE_CODE_MESSAGING_",
    )
    #: Preferred env credential. When absent, an explicitly verified keychain
    #: login may authenticate the child; absence of both refuses before review.
    REQUIRED_ENV: str = "CLAUDE_CODE_OAUTH_TOKEN"


def _existing_claude_review(pr: str, sha: str) -> bool:
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    return any(
        is_claude_artifact((r.get("user") or {}).get("login", ""), r.get("body") or "", sha)
        for r in reviews
    )


# ----- the model ----------------------------------------------------------


def _claude_argv(model: str, effort: str) -> list[str]:
    """The `claude` invocation: one non-interactive turn, no tools, no state.

    `--restricted` removes command/code tools and ignores user, project and
    local settings, so the reviewer cannot edit a tree or invoke host hooks.
    `--strict-mcp-config` and `--disable-slash-commands` exclude other external
    configuration. The PR diff arrives in the prompt, not from a working tree.
    `--permission-mode dontAsk` denies requests that would prompt, rather than
    waiting without an operator. The installed-CLI argv contract test checks
    emitted flags and documented values so incompatible CLI changes fail closed.
    """
    return [
        "claude",
        "--print",
        "--model",
        model,
        "--effort",
        effort,
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        # Stream JSON carries assistant records naming the answering model.
        # Result modelUsage can include auxiliary calls, so it cannot identify
        # the reviewer by its first key. --verbose emits assistant records.
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def _child_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in CFG.STRIPPED_ENV
        and k not in CFG.STRIPPED_ENV_NAMES
        and not k.startswith(CFG.STRIPPED_ENV_PREFIXES)
    }


def _records(stdout: str) -> list[dict]:
    """The JSON Lines the CLI streamed, skipping any non-JSON noise around them."""
    records = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _spent(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in CFG.SPENT_MARKERS)


def _read_result(stdout: str) -> tuple[str, str, dict]:
    """Unpack the streamed transcript, refusing incomplete or error results.

    Error results, non-success subtypes and empty replies do not establish a
    review. Models come from assistant records, not result modelUsage, which
    may include auxiliary calls. Report every distinct answering model so a
    model switch is explicit. No answering model means coverage is refused.
    """
    records = _records(stdout)
    finals = [r for r in records if r.get("type") == "result"]
    if not finals:
        raise TriageError(f"claude printed no result record: {stdout[-800:]}")
    payload = finals[-1]
    if payload.get("is_error") or payload.get("subtype") != "success":
        raise TriageError(
            f"claude ended {payload.get('subtype')!r} (api_error_status="
            f"{payload.get('api_error_status')!r}); nothing was reviewed"
        )
    reply = str(payload.get("result") or "")
    if not reply.strip():
        raise TriageError("claude returned an empty reply; nothing was reviewed")
    models = []
    for record in records:
        model = (record.get("message") or {}).get("model")
        if record.get("type") == "assistant" and model and model not in models:
            models.append(model)
    if not models:
        # A review whose author cannot be named must not certify a head: the
        # marker would then claim a model no record supports.
        raise TriageError(
            "the transcript names no model for any assistant turn, so this run cannot "
            "say which model reviewed; nothing was posted"
        )
    usage = payload.get("usage") or {}
    record = {
        "model": "+".join(models),
        "cost_usd": payload.get("total_cost_usd"),
        "duration_s": round((payload.get("duration_ms") or 0) / 1000, 1),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_tokens": usage.get("cache_creation_input_tokens"),
    }
    return reply, record["model"], record


def _failure_text(stdout: str, stderr: str) -> str:
    """Text that may explain a failed run, excluding model reply text.

    Review prose can legitimately quote quota markers and must not be read
    as CLI failure evidence. Keep stderr and non-JSON CLI noise; add only a
    failed result record. This also excludes assistant text from interrupted
    streams with no final result, so incomplete runs cannot poison coverage.
    """
    noise = [raw for raw in stdout.splitlines() if not raw.strip().startswith("{")]
    finals = [r for r in _records(stdout) if r.get("type") == "result"]
    if not finals:
        # No result record: a killed or timed-out run. Everything except the
        # assistant records is the CLI's own, and those are already excluded
        # because only non-JSON noise is kept here.
        return "\n".join([stderr, *noise])
    final = finals[-1]
    if final.get("is_error") or final.get("subtype") != "success":
        return "\n".join([stderr, *noise, json.dumps(final)])
    return stderr


def wall_report(output: str) -> str:
    """Report an unavailable review and retain terminal CLI evidence.

    Usage refusal cannot certify the PR: the run exits3 rather than clean0.
    Any reset information reported by the CLI remains in its terminal excerpt.
    """
    return (
        "The configured Claude review refused the request with a usage limit. "
        "Nothing was posted and the PR is NOT "
        f"covered. The CLI said: {output.strip()[-400:]}"
    )


def _run_claude_auth_status(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run `claude auth status --json` under the review child environment.

    The child may lack credentials stripped from the parent, so parent auth
    cannot establish child authentication. Pass the identical child environment
    and explicitly request JSON rather than depending on a CLI default.
    """
    return subprocess.run(
        ["claude", "auth", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _keychain_login_ok(env: dict[str, str]) -> tuple[bool, str]:
    """(True, "") when `claude auth status`, run under `env`, reports an
    active keychain login, else (False, <what was checked and what it said>).

    `env` is threaded through from `_auth_source` rather than read from the
    parent process here, so the probe and the review invocation it is
    gating always see the same environment.

    Every way this can fail to confirm a login -- the command missing or
    timing out, a non-zero exit, unparseable stdout, `loggedIn` absent or
    false -- returns False rather than raising, so `_auth_source` is the one
    place that decides whether the overall precondition is met and the one
    place that writes the error message. The detail string travels with the
    False so that message names the actual failure instead of a generic "no
    keychain login".
    """
    try:
        proc = _run_claude_auth_status(env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"`claude auth status` could not be run: {exc}"
    if proc.returncode != 0:
        return False, (
            f"`claude auth status` exited {proc.returncode}: "
            f"{(proc.stdout + proc.stderr).strip()[-400:]}"
        )
    try:
        status = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, f"`claude auth status` printed non-JSON output: {proc.stdout.strip()[-400:]}"
    if status.get("loggedIn") is not True:
        return False, f"`claude auth status` reports loggedIn={status.get('loggedIn')!r}"
    return True, ""


def _auth_source(child_env: dict[str, str]) -> str:
    """Return env-token or keychain only when the child can authenticate.

    A non-empty configured token is checked first. Without it, verify the
    child keychain login; never infer auth from absence of a token. If neither
    option holds, refuse before review and name both options and probe detail.
    """
    if child_env.get(CFG.REQUIRED_ENV):
        return "env-token"
    ok, detail = _keychain_login_ok(child_env)
    if ok:
        return "keychain"
    raise TriageError(
        f"neither {CFG.REQUIRED_ENV} is set (or empty) in the environment handed to the "
        f"child, nor is there an active keychain login ({detail}). Nothing was run. "
        "Provide the configured credential or run `claude auth login` on this host."
    )


def review_with_claude(prompt: str, model: str, effort: str) -> tuple[str, str, dict]:
    """Run the review; return reply text, resolved model and usage record.

    A good result establishes review before failure text is inspected for
    usage refusal. Otherwise findings quoting quota vocabulary could be read
    as CLI failure. Resolve the answering model from actual assistant records,
    never assume the requested alias identifies the model that answered.
    """
    child_env = _child_env()
    auth_source = _auth_source(child_env)
    try:
        proc = subprocess.run(
            _claude_argv(model, effort),
            input=prompt,
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=child_env,
            timeout=CFG.CLAUDE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TriageError(
            f"claude did not answer within {CFG.CLAUDE_TIMEOUT_S}s; nothing was reviewed"
        ) from exc
    try:
        reply, resolved_model, usage = _read_result(proc.stdout)
    except TriageError as failure:
        blob = _failure_text(proc.stdout, proc.stderr)
        if _spent(blob):
            raise TriageError(wall_report(blob)) from failure
        if proc.returncode != 0:
            raise TriageError(
                f"claude exited {proc.returncode}: {(proc.stdout + proc.stderr)[-800:]}"
            ) from failure
        raise
    # Preserve the verified authentication source in the usage record so
    # operators can distinguish an env token from keychain authentication.
    usage["auth_source"] = auth_source
    return reply, resolved_model, usage


# ----- posting ------------------------------------------------------------


def summary_body(
    sha: str,
    model: str,
    findings: list[Finding],
    carried: list[Finding],
    unreviewed: list[str],
) -> str:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in sorted(BADGE_COLORS)}
    tally = ", ".join(f"{s} {n}" for s, n in counts.items() if n)
    noun = "finding" if len(findings) == 1 else "findings"
    head = f"Claude review of `{sha[:10]}`: {len(findings)} {noun}" + (
        f" ({tally})." if tally else "."
    )
    lines = [
        head,
        "",
        # Report the actual answering model ID rather than the requested alias.
        f"Claude CLI model `{model}` answered this configured review lane.",
        "",
        "P0/P1 findings are BLOCKING and must be FIXED or REBUTTED before merge. "
        "P2/P3 are NON-BLOCKING. Every thread still needs one of the three terminal "
        "states (see AGENTS.md).",
    ]
    if carried:
        lines += ["", "Findings with no postable line anchor, kept here rather than dropped:"]
        for f in carried:
            where = f"`{f.path}`" + (f":{f.line}" if f.line else "")
            # The DETAIL travels with them. Carrying only the title drops the
            # failing input and the remediation, which for a P0 is most of the
            # finding.
            lines += [f"- {f.verdict} {f.severity}: {where} -- {withheld(f.title)}"]
            if f.detail:
                lines += [f"  {withheld(f.detail)}"]
    lines += unreviewed_note(unreviewed)
    lines += ["", marker(sha, model, skipped=frozenset(unreviewed))]
    return "\n".join(lines)


# ----- CLI ----------------------------------------------------------------


def run(pr: str, model: str, effort: str, dry_run: bool, force: bool) -> int:
    sha = pinned_head(pr)
    print(f"{TAG} PR #{pr} pinned at {sha}")
    if not force and _existing_claude_review(pr, sha):
        print(f"{TAG} a Claude review already exists for {sha}; nothing to do (--force to redo)")
        return 0
    title = _gh(["pr", "view", pr, "--repo", REPO, "--json", "title", "-q", ".title"]).strip()
    diff, unreviewed = reviewable_diff(pr)
    skipped = frozenset(unreviewed)
    if unreviewed:
        print(f"{TAG} not reviewed, generated data: {', '.join(unreviewed)}")
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
            fence=CLAUDE_FENCE,
            withheld=unreviewed,
        ),
    )
    reply, resolved, usage = review_with_claude(prompt, model, effort)
    findings = parse_findings(reply, run_id, CLAUDE_FENCE, CFG.MAX_FINDINGS)
    try:
        findings, suppressed = debt_logged_findings(findings, debt_text_at(int(pr), sha))
    except LedgerReadError as exc:
        suppressed = []
        print(f"{TAG} debt ledger unreadable; all findings remain open: {exc}")
    print(
        f"{TAG} model={resolved} auth_source={usage['auth_source']} "
        f"cost_usd={usage['cost_usd']} duration_s={usage['duration_s']} "
        f"in={usage['input_tokens']} out={usage['output_tokens']} "
        f"cache_read={usage['cache_read_tokens']} cache_write={usage['cache_write_tokens']} "
        f"findings={len(findings)}"
    )
    for finding in findings:
        anchor = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"  {finding.verdict} {finding.severity} {anchor} -- {finding.title}")
    if dry_run:
        print(f"{TAG} dry run; nothing posted")
        return 0
    inline, carried = split_by_anchor(findings, anchorable_lines(diff))
    lane_marker = marker(sha, resolved, skipped=skipped)
    summary = add_suppressed_summary(
        summary_body(sha, resolved, findings, carried, unreviewed),
        suppressed,
        lane_marker,
    )
    print(f"{TAG} {len(carried)} finding(s) carried in the summary, none dropped")
    print(f"{TAG} {post_review(pr, sha, summary, inline, lane_marker, TAG)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-review", description=__doc__)
    parser.add_argument("pr")
    parser.add_argument("--model", default=CFG.MODEL, help="model alias or id for the CLI")
    parser.add_argument("--effort", default=CFG.EFFORT, help="low | medium | high | xhigh | max")
    parser.add_argument("--dry-run", action="store_true", help="review and print, post nothing")
    parser.add_argument("--force", action="store_true", help="re-review a head already covered")
    args = parser.parse_args(argv)
    try:
        return run(args.pr, args.model, args.effort, args.dry_run, args.force)
    except TriageError as exc:
        # Exit 3, never 1 and never 0: "I could not review" must not be
        # readable as "I reviewed and found nothing".
        print(f"{TAG} FAILED: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
