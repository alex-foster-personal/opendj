"""Sol review lane: run GPT-5.6 over a PR diff and post the findings as
review threads, when the Codex GitHub app cannot.

WHY THIS EXISTS. `chatgpt-codex-connector[bot]` is the only reviewer
`review_coverage.EXPECTED_REVIEWERS` names, and at about 18:30Z on
Thu 4 Sep 2026 it ran out of quota and started posting a usage-limit notice
in place of every review (issue #1211). The coverage gate did its job and
reported MISS on every PR from that moment; PRs merged un-reviewed anyway.
The gate was never the problem. The missing piece was a SECOND ROUTE to the
same review, one whose quota is a different bucket.

Sol is that route: the same class of model reached through the Codex CLI on a
ChatGPT subscription seat instead of through the GitHub app. It bills the
subscription, never the OpenAI API -- every invocation strips
`OPENAI_API_KEY` from the child environment (see `_codex_argv`), for the
reason set out in the af-delegate-to-codex-subscription-not-api skill: the
precedence between an exported API key and `~/.codex/auth.json` is not
authoritatively documented, so it is guarded rather than trusted.

WHAT MAKES THIS A REVIEW AND NOT A COMMENT-SPRAYER:

  Pinned head. The SHA is taken from `git ls-remote`, not from the PR object,
  because `gh pr view`'s `headRefOid` lags the branch ref and a review posted
  against a stale head certifies a push nobody looked at. The two must AGREE
  before anything is posted (`_pinned_head`), so an in-flight push aborts the
  run rather than producing a review of the wrong tree.

  Idempotent per head. A Sol review already present for this exact SHA means
  the work is done; re-running posts nothing. Re-review after a push is a new
  SHA and therefore a new run, which is the same per-push cadence Codex has.

  Severity drives the verdict, mechanically. The model chooses P0-P3; this
  module derives BLOCKING/NON-BLOCKING from it, per AGENTS.md's review output
  convention (P0/P1 blocking, P2/P3 not). Letting the model emit both halves
  independently means the day it emits "P1 NON-BLOCKING" the merge gate tiers
  a blocker as debt-loggable.

  Findings survive a bad anchor. A finding whose line is not in the diff
  cannot be posted inline, so it is carried in the summary review instead of
  being dropped. Silently losing a P0 because of an off-by-one line number is
  the worst outcome available here.

Requirements (mini-PRD):
  / Post one review thread per finding, verdict and P-level leading the body.
    [if a finding posts without a P-level or verdict then broken]
  / Derive the verdict from the severity, never from the model's own wording.
    [if a P1 can post as NON-BLOCKING then broken]
  / Refuse to post against a head that `git ls-remote` and the PR disagree on.
    [if a review posts while the branch is mid-push then broken]
  / Skip a head that already carries a Sol review, unless forced.
    [if re-running doubles the threads then broken]
  / Never let a finding with an unpostable anchor vanish.
    [if a finding with a bad line number is dropped silently then broken]
  / Never bill the OpenAI API.
    [if OPENAI_API_KEY reaches the codex child environment then broken]

Usage:
    python -m scripts.sol_review <PR> [--seat auto|local|<ssh-host>]
                                      [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import uuid
from typing import ClassVar

try:
    from scripts.review_gh import TriageError, _gh, _paginated_json_list
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.sol_review") from None
    raise
from scripts.codex_home import CodexHomeUnavailable, select_codex_home
from scripts.review_lane import (
    BADGE_COLORS,
    REPO,
    Finding,
    anchorable_lines,
    first_group,
    parse_findings,
    pinned_head,
    post_review,
    reviewable_diff,
    split_by_anchor,
    unreviewed_note,
    withheld,
)
from scripts.review_prompt import ReviewPromptConfig, SOL_FENCE, build_prompt
from scripts.review_sol import is_sol_artifact, marker


class CFG:
    """Everything a reader needs to see to predict what a run will do."""

    #: Seats tried in order under `--seat auto`. "local" is this machine's
    #: `~/.codex`; anything else is an ssh host whose own `~/.codex` is a
    #: SEPARATE subscription seat with a separate quota window. Both are
    #: subscription seats: failing over between them never reaches the API.
    SEATS: tuple[str, ...] = ("local", "nucbox-wsl")
    #: Hostnames on which a named seat executes directly. This inspectable
    #: registry avoids treating a remote nucbox seat as local on another host.
    LOCAL_SEAT_HOSTS: ClassVar[dict[str, frozenset[str]]] = {
        "nucbox-wsl": frozenset({"NucBoxEVO-X2"}),
    }
    #: Substrings in codex output that mean THIS seat is spent. Failing over
    #: on these is safe; failing over on any other error would hide a real bug.
    SPENT_MARKERS: tuple[str, ...] = ("hit your usage limit", "usage limit reached")
    #: Diff bytes handed to the model; over this the run refuses (see `run`).
    #: Measured Wed 30 Sep 2026: PR #4240's 519,631-byte reviewable diff took
    #: 159,969 tokens and 69 s on gpt-5.6 through a Pro seat, about 3.3 bytes
    #: per token, so 640 KB stays near 200K tokens. The old 320 KB cap refused
    #: every PR carrying a large recorded fixture or register migration, which
    #: left the Codex GitHub app as the only reviewer for them.
    MAX_DIFF_BYTES: int = 640_000
    #: Findings posted per run. A reviewer that opens 60 threads is not read.
    MAX_FINDINGS: int = 12
    #: Seconds for one codex run. Generous: a large diff at high reasoning
    #: effort has been observed past ten minutes.
    CODEX_TIMEOUT_S: int = 1200


_MODEL_LINE = re.compile(r"^model:\s*(\S+)", re.MULTILINE)
_TOKENS_LINE = re.compile(r"tokens used[:\s]+([\d,]+)", re.IGNORECASE)
#: codex's own wording, e.g. "try again at Sep 10th, 2026 8:59 PM".
_RESET_AT = re.compile(r"try again at ([^.\n]+)", re.IGNORECASE)


def _existing_sol_review(pr: str, sha: str) -> bool:
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    return any(
        is_sol_artifact((r.get("user") or {}).get("login", ""), r.get("body") or "", sha)
        for r in reviews
    )


# ----- the model ----------------------------------------------------------


def _codex_argv() -> list[str]:
    """The codex invocation, read-only and with no route to the API.

    `--sandbox read-only` because a reviewer has no business editing the tree.
    `--skip-git-repo-check` because it runs from a scratch directory, not a
    checkout: the diff arrives in the prompt, so a repo would only tempt the
    model into reading a working tree that is not the PR's.
    """
    return [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
    ]


def _is_local_seat(seat: str, hostname: str | None = None) -> bool:
    """Whether `seat` executes directly on the current configured host."""
    if seat == "local":
        return True
    elif seat in CFG.LOCAL_SEAT_HOSTS:  # noqa: RET505 - seat kinds stay explicit.
        return (hostname or socket.gethostname()) in CFG.LOCAL_SEAT_HOSTS[seat]
    return False


def _seat_argv(seat: str, hostname: str | None = None) -> list[str]:
    """The direct or SSH argv selected by the inspectable seat registry."""
    if _is_local_seat(seat, hostname):
        return _codex_argv()
    elif seat:  # noqa: RET505 - local and remote seat kinds stay explicit.
        remote_prefix = 'export PATH="$HOME/.local/bin:$PATH"; cd /tmp && env -u OPENAI_API_KEY '
        return [
            "ssh",
            "-o",
            "ConnectTimeout=15",
            seat,
            remote_prefix + " ".join(_codex_argv()),
        ]
    raise TriageError("seat must name a configured local alias or an SSH host")


def _run_seat(seat: str, prompt: str) -> subprocess.CompletedProcess[str]:
    if _is_local_seat(seat):
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        # CODEX_HOME is set HERE, deterministically, rather than trusted from
        # whatever the caller's shell happened to export. A stale ambient
        # CODEX_HOME pointing at an expired seat used to fail silently into a
        # 401; this fails explicitly (coverage MISS) or picks a live seat.
        try:
            codex_home, weekly_pct = select_codex_home()
        except CodexHomeUnavailable as exc:
            raise TriageError(f"coverage MISS: {exc}") from exc
        env["CODEX_HOME"] = codex_home
        print(f"[sol-review] local seat: CODEX_HOME={codex_home} ({weekly_pct}% weekly used)")
        return subprocess.run(
            _seat_argv(seat),
            input=prompt,
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=env,
            timeout=CFG.CODEX_TIMEOUT_S,
            check=False,
        )
    elif seat:  # noqa: RET505 - local and remote seat kinds stay explicit.
        return subprocess.run(
            _seat_argv(seat),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CFG.CODEX_TIMEOUT_S,
            check=False,
        )
    raise TriageError("seat must name a configured local alias or an SSH host")


def _seat_error_location(seat: str, returncode: int) -> str:
    """Describe an execution failure without misattributing a Codex error to SSH."""
    if _is_local_seat(seat):
        return f"local seat {seat}"
    elif returncode == 255:  # noqa: RET505 - failure kinds stay explicit.
        return f"remote seat {seat}; configure SSH for host {seat} before retrying"
    elif seat:
        return f"remote seat {seat}"
    raise TriageError("seat must name a configured local alias or an SSH host")


def _seat_is_spent(output: str) -> bool:
    """A wall is codex's own `ERROR:` line, never text the prompt echoes back.

    codex echoes the whole prompt to stderr, diff included. Scanning all of it
    read a diff context line quoting `SPENT_MARKERS` as a refusal and threw
    away every review of PR #4544, a run that exited 0 with a parsed result
    (Thu 1 Oct 2026). Diff lines always start with `+`, `-`, ` ` or `@@`, so
    a line-start `ERROR:` is codex speaking. Same class as the Claude lane's
    #1394 fix, which never reached this lane."""
    lowered = _codex_errors(output).lower()
    return any(m in lowered for m in CFG.SPENT_MARKERS)


def _codex_errors(output: str) -> str:
    """codex's own `ERROR:` lines: the only text that may describe a refusal."""
    return "\n".join(line for line in output.splitlines() if line.startswith("ERROR:"))


def reported_tokens(output: str) -> str:
    """codex's own token count. It prints it AFTER echoing the prompt, so a diff
    quoting a count matches first; the last match is codex's."""
    return (_TOKENS_LINE.findall(output) or ["unknown"])[-1]


def seat_wall_report(seat: str, output: str) -> str:
    """What a usage-limit refusal from `seat` actually tells us.

    IT DOES NOT TELL US THE SEAT IS EMPTY, and calling it that sends a merge
    lane away to declare a PR permanently blocked. Measured on nucbox-wsl,
    Fri 5 Sep 2026, all on one seat:

        07:00Z  full review of #1226, 43,182 tokens        accepted
        08:05Z  full review of #1227, 67,825-byte prompt   usage limit
        09:20Z  the SAME 67,825-byte prompt, 47,725 tokens accepted
        09:55Z  full review of #1227                       usage limit
        10:05Z  one-line prompt                            accepted

    The identical request was refused and then accepted 75 minutes later, so
    the wall is INTERMITTENT, not a property of the request. The likely reason
    is contention: nucbox's own merge residents run Codex on this same seat
    every tick, so the window's remainder moves under the lane. A retry is
    therefore the right next move, and "the seat is out of quota" is the wrong
    conclusion to hand a merge lane. The reset time, when codex names one, is
    passed through verbatim rather than paraphrased.
    """
    when = first_group(_RESET_AT, _codex_errors(output)).strip().strip('".')
    reset = f" Codex names a reset at {when}." if when != "unknown" else ""
    return (
        f"seat {seat} refused this request with a usage limit.{reset} That refusal is "
        "intermittent, not proof the seat is empty: the identical request has been "
        "accepted on this seat after being refused, because other agents share the "
        "window. Retry before calling the PR blocked."
    )


def review_with_codex(prompt: str, seat: str) -> tuple[str, str, str]:
    """Run the review. Returns (output, seat used, model reported).

    Under `--seat auto` a spent seat fails over to the next SUBSCRIPTION seat,
    which is a different quota bucket on the same billing route. It never
    falls back to an API key: the child environment has none, by construction.
    Any error that is not a quota wall is raised, because failing over on it
    would run the same bug twice and hide it.
    """
    seats = CFG.SEATS if seat == "auto" else (seat,)
    spent: list[str] = []
    for candidate in seats:
        proc = _run_seat(candidate, prompt)
        output = proc.stdout + proc.stderr
        if _seat_is_spent(output):
            spent.append(seat_wall_report(candidate, output))
            print(f"[sol-review] {spent[-1]}")
            continue
        if proc.returncode != 0:
            location = _seat_error_location(candidate, proc.returncode)
            raise TriageError(
                f"codex on {location} exited {proc.returncode}: {output[-800:]}"
            )
        return output, candidate, first_group(_MODEL_LINE, output)
    raise TriageError(
        "no seat accepted this review. "
        + " ".join(spent)
        + " Nothing here is a reason to use an OpenAI API key."
    )


# ----- posting ------------------------------------------------------------


def summary_body(
    sha: str,
    seat: str,
    model: str,
    findings: list[Finding],
    carried: list[Finding],
    unreviewed: list[str],
) -> str:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in sorted(BADGE_COLORS)}
    tally = ", ".join(f"{s} {n}" for s, n in counts.items() if n)
    noun = "finding" if len(findings) == 1 else "findings"
    head = f"Sol review of `{sha[:10]}`: {len(findings)} {noun}" + (
        f" ({tally})." if tally else "."
    )
    lines = [
        head,
        "",
        # The model is REPORTED, never asserted. The lane does not pass `-m`,
        # so the seat's own default decides which model ran; printing a family
        # name here would claim a model this run cannot verify. Sol's own
        # review of this file, P1 (Fri 5 Sep 2026).
        f"Codex CLI model `{model}` on the `{seat}` ChatGPT subscription seat, standing in "
        "for the Codex GitHub app while its quota window is exhausted (issue #1211).",
        "",
        "P0/P1 findings are BLOCKING and must be FIXED or REBUTTED before merge. "
        "P2/P3 are NON-BLOCKING. Every thread still needs one of the three terminal "
        "states (see AGENTS.md).",
    ]
    if carried:
        lines += ["", "Findings with no postable line anchor, kept here rather than dropped:"]
        for f in carried:
            where = f"`{f.path}`" + (f":{f.line}" if f.line else "")
            # The DETAIL travels with them. Carrying only the title dropped the
            # failing input and the remediation, which for a P0 is most of the
            # finding. Sol's own review of this file, P2 (Fri 5 Sep 2026).
            lines += [f"- {f.verdict} {f.severity}: {where} -- {withheld(f.title)}"]
            if f.detail:
                lines += [f"  {withheld(f.detail)}"]
    lines += unreviewed_note(unreviewed)
    lines += ["", marker(sha, seat, model, skipped=frozenset(unreviewed))]
    return "\n".join(lines)


# ----- CLI ----------------------------------------------------------------


def run(pr: str, seat: str, dry_run: bool, force: bool) -> int:
    sha = pinned_head(pr)
    print(f"[sol-review] PR #{pr} pinned at {sha}")
    if not force and _existing_sol_review(pr, sha):
        print(
            f"[sol-review] a Sol review already exists for {sha}; nothing to do (--force to redo)"
        )
        return 0
    title = _gh(["pr", "view", pr, "--repo", REPO, "--json", "title", "-q", ".title"]).strip()
    diff, unreviewed = reviewable_diff(pr)
    skipped = frozenset(unreviewed)
    if unreviewed:
        print(f"[sol-review] not reviewed, generated data: {', '.join(unreviewed)}")
    # A REVIEW OF PART OF A DIFF MAY NOT CERTIFY THE WHOLE PUSH. Truncating and
    # saying so in prose still posts a current-head marker, and coverage reads
    # the marker, not the prose -- the same "honest denominator" failure this
    # repo already has a rule about. So an oversized diff is a refusal, and the
    # PR goes to a human or to a smaller split. Sol's own review of this file,
    # P1 (Fri 5 Sep 2026).
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
            fence=SOL_FENCE,
            withheld=unreviewed,
        ),
    )
    output, used, model = review_with_codex(prompt, seat)
    findings = parse_findings(output, run_id, SOL_FENCE, CFG.MAX_FINDINGS)
    tokens = reported_tokens(output)
    print(f"[sol-review] seat={used} model={model} tokens={tokens} findings={len(findings)}")
    for finding in findings:
        anchor = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"  {finding.verdict} {finding.severity} {anchor} -- {finding.title}")
    if dry_run:
        print("[sol-review] dry run; nothing posted")
        return 0
    inline, carried = split_by_anchor(findings, anchorable_lines(diff))
    lane_marker = marker(sha, used, model, skipped=skipped)
    summary = summary_body(sha, used, model, findings, carried, unreviewed)
    print(f"[sol-review] {len(carried)} finding(s) carried in the summary, none dropped")
    url = post_review(pr, sha, summary, inline, lane_marker, "[sol-review]")
    print(f"[sol-review] {url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sol-review", description=__doc__)
    parser.add_argument("pr")
    parser.add_argument(
        "--seat", default="auto", help="auto | local | an ssh host with a codex seat"
    )
    parser.add_argument("--dry-run", action="store_true", help="review and print, post nothing")
    parser.add_argument("--force", action="store_true", help="re-review a head already covered")
    args = parser.parse_args(argv)
    try:
        return run(args.pr, args.seat, args.dry_run, args.force)
    except TriageError as exc:
        print(f"[sol-review] FAILED: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
