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
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

from scripts.review_gh import TriageError, _gh, _paginated_json_list
from scripts.review_sol import is_sol_artifact, marker
from scripts.sol_prompt import (
    JSON_CLOSE,
    JSON_OPEN,
    RUN_ID_PLACEHOLDER,
    build_prompt,
)

REPO = "maintainer/music-dj-tools"


class CFG:
    """Everything a reader needs to see to predict what a run will do."""

    #: Seats tried in order under `--seat auto`. "local" is this machine's
    #: `~/.codex`; anything else is an ssh host whose own `~/.codex` is a
    #: SEPARATE subscription seat with a separate quota window. Both are
    #: subscription seats: failing over between them never reaches the API.
    SEATS: tuple[str, ...] = ("local", "nucbox-wsl")
    #: Substrings in codex output that mean THIS seat is spent. Failing over
    #: on these is safe; failing over on any other error would hide a real bug.
    SPENT_MARKERS: tuple[str, ...] = ("hit your usage limit", "usage limit reached")
    #: Diff bytes handed to the model. A truncated diff is stated in the
    #: review body rather than quietly reviewed as if complete.
    MAX_DIFF_BYTES: int = 320_000
    #: Findings posted per run. A reviewer that opens 60 threads is not read.
    MAX_FINDINGS: int = 12
    #: Seconds for one codex run. Generous: a large diff at high reasoning
    #: effort has been observed past ten minutes.
    CODEX_TIMEOUT_S: int = 1200
    #: Seconds to wait for the PR object to catch up with the branch ref.
    HEAD_AGREE_TIMEOUT_S: int = 90


#: Severity to verdict. AGENTS.md defines the tiering; this is the only place
#: it is applied, so the two halves of a finding's marker cannot disagree.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"P0", "P1"})

#: Shields colors matching the Codex bot's own badges, so the two reviewers
#: render identically in a thread list. `review_thread_parse._SEVERITY_BADGE`
#: reads the P-level straight out of this URL.
BADGE_COLORS: dict[str, str] = {"P0": "red", "P1": "orange", "P2": "yellow", "P3": "blue"}

_JSON_BLOCK = re.compile(re.escape(JSON_OPEN) + r"(.*?)" + re.escape(JSON_CLOSE), re.DOTALL)
_MODEL_LINE = re.compile(r"^model:\s*(\S+)", re.MULTILINE)
_TOKENS_LINE = re.compile(r"tokens used[:\s]+([\d,]+)", re.IGNORECASE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
#: codex's own wording, e.g. "try again at Sep 10th, 2026 8:59 PM".
_RESET_AT = re.compile(r"try again at ([^.\n]+)", re.IGNORECASE)

#: Phrases `review_coverage.NOT_REVIEWED_MARKERS` reads as "this reviewer did
#: not really look". They are legitimate English, so model prose may contain
#: one; in the SUMMARY body that would make a real review self-report as a
#: non-review and fail its own coverage check. Withheld there, kept verbatim
#: in the inline thread where nothing parses them.
_COVERAGE_POISON: tuple[str, ...] = (
    "rate limited",
    "review skipped",
    "trial expired",
    "no credits",
    "skipped:",
)


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    """The pattern's first capture, or "unknown". Reporting-only: a missing
    model or token line must never fail a review that already happened."""
    match = pattern.search(text)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    severity: str
    title: str
    detail: str

    @property
    def verdict(self) -> str:
        return "BLOCKING" if self.severity in BLOCKING_SEVERITIES else "NON-BLOCKING"

    def body(self, sha: str, seat: str, model: str) -> str:
        """The thread body, shaped so `review_thread_parse` reads both halves.

        The badge gives `_severity` the P-level. The NEXT line must OPEN with
        the verdict, because `_blocking` anchors its match to the start of the
        headline and markdown images are stripped before that test -- a line
        reading "P1 BLOCKING: ..." parses as `unmarked` and tiers a blocker as
        an ordinary finding.
        """
        color = BADGE_COLORS.get(self.severity, "lightgrey")
        badge = f"![{self.severity} Badge](https://img.shields.io/badge/{self.severity}-{color}?style=flat)"
        return (
            f"{badge}\n"
            f"{self.verdict} {self.severity}: {self.title}\n\n"
            f"{self.detail}\n\n"
            f"{marker(sha, seat, model)}\n"
        )


# ----- head pinning -------------------------------------------------------


def _branch(pr: str) -> str:
    """The PR's head branch, refusing a PR that is not open.

    A merged or closed PR usually has no branch left, and `git ls-remote`
    answers a deleted ref with exit 0 and empty output -- indistinguishable
    from a network problem if the state is not checked first. Observed on
    #1224 (Fri 5 Sep 2026), which merged between being picked as a review
    target and the lane running.
    """
    query = '.state + " " + .headRefName'
    args = ["pr", "view", pr, "--repo", REPO, "--json", "state,headRefName", "-q", query]
    state, branch = _gh(args).split()
    if state != "OPEN":
        raise TriageError(f"PR #{pr} is {state}, not OPEN; there is nothing left to review")
    return branch


def _ref_sha(branch: str) -> str:
    """The branch tip straight from the remote ref, via `git ls-remote`.

    Deliberately not `gh pr view --json headRefOid`: the PR object lags the
    ref, and this repo has already shipped one bug from gating on the lagging
    copy. The remote URL is spelled out rather than using `origin`, so the
    answer does not depend on which checkout the lane happens to run from.
    """
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{REPO}.git", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise TriageError(
            f"git ls-remote found no refs/heads/{branch}: {proc.stderr.strip() or '<empty>'}"
        )
    return proc.stdout.split()[0]


def _pr_head(pr: str) -> str:
    args = ["pr", "view", pr, "--repo", REPO, "--json", "headRefOid", "-q", ".headRefOid"]
    return _gh(args).strip()


def _pinned_head(pr: str) -> str:
    """The head SHA, taken from the REF and confirmed against the PR object.

    Two readings of the same fact, and they disagree for a real reason: the
    PR object's `headRefOid` lags the branch ref for seconds to minutes after
    a push. Reviewing the ref's SHA while coverage measures the PR object's
    would post evidence at a SHA the gate is not asking about, so the review
    would be invisible to the very gate it exists to satisfy. Wait for them to
    agree, then pin; never average them, never pick one.
    """
    branch = _branch(pr)
    deadline = time.monotonic() + CFG.HEAD_AGREE_TIMEOUT_S
    while True:
        ref, obj = _ref_sha(branch), _pr_head(pr)
        if ref == obj:
            return ref
        if time.monotonic() >= deadline:
            raise TriageError(
                f"branch {branch} is at {ref} but PR #{pr} still reports {obj} after "
                f"{CFG.HEAD_AGREE_TIMEOUT_S}s. The PR is mid-push; re-run once it settles."
            )
        time.sleep(5)


def _existing_sol_review(pr: str, sha: str) -> bool:
    reviews = _paginated_json_list(f"repos/{REPO}/pulls/{pr}/reviews")
    return any(
        is_sol_artifact((r.get("user") or {}).get("login", ""), r.get("body") or "", sha)
        for r in reviews
    )


# ----- the diff -----------------------------------------------------------


def _diff(pr: str) -> str:
    return _gh(["api", f"repos/{REPO}/pulls/{pr}", "-H", "Accept: application/vnd.github.v3.diff"])


def anchorable_lines(diff: str) -> dict[str, set[int]]:
    """RIGHT-side line numbers a review comment may be attached to.

    GitHub rejects the whole review with a 422 if ANY comment names a line
    outside the diff, so one bad anchor loses every finding in the batch. This
    is the pure half of that defense; `_split_by_anchor` is what acts on it.
    Added and context lines both count; removed lines have no right-side
    number and are deliberately absent.
    """
    lines: dict[str, set[int]] = {}
    path, cursor = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path, cursor = raw[6:], 0
            lines.setdefault(path, set())
        elif (hunk := _HUNK.match(raw)) and path:
            cursor = int(hunk.group(1))
        elif path and cursor:
            if raw.startswith(("+", " ")):
                lines[path].add(cursor)
                cursor += 1
            elif raw.startswith("-"):
                continue
            else:
                cursor = 0
    return lines


def _split_by_anchor(
    findings: list[Finding], anchors: dict[str, set[int]]
) -> tuple[list[Finding], list[Finding]]:
    """(postable inline, carried in the summary instead)."""
    inline: list[Finding] = []
    carried: list[Finding] = []
    for finding in findings:
        ok = finding.line is not None and finding.line in anchors.get(finding.path, set())
        (inline if ok else carried).append(finding)
    return inline, carried


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


def _run_seat(seat: str, prompt: str) -> subprocess.CompletedProcess[str]:
    if seat == "local":
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        return subprocess.run(
            _codex_argv(),
            input=prompt,
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=env,
            timeout=CFG.CODEX_TIMEOUT_S,
            check=False,
        )
    remote = 'export PATH="$HOME/.local/bin:$PATH"; cd /tmp && env -u OPENAI_API_KEY ' + " ".join(
        _codex_argv()
    )
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", seat, remote],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CFG.CODEX_TIMEOUT_S,
        check=False,
    )


def _seat_is_spent(output: str) -> bool:
    lowered = output.lower()
    return any(m in lowered for m in CFG.SPENT_MARKERS)


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
    when = _first_group(_RESET_AT, output).strip().strip('".')
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
            raise TriageError(
                f"codex on seat {candidate} exited {proc.returncode}: {output[-800:]}"
            )
        return output, candidate, _first_group(_MODEL_LINE, output)
    raise TriageError(
        "no seat accepted this review. "
        + " ".join(spent)
        + " Nothing here is a reason to use an OpenAI API key."
    )


def parse_findings(output: str, run_id: str) -> list[Finding]:
    """Pull THIS RUN's fenced JSON out of a codex transcript.

    Every block is considered and the last one CARRYING THIS RUN'S ID wins.
    Both halves matter. Last, because a model may restate the shape while
    reasoning. This run's id, because the transcript also contains blocks the
    model never wrote: `codex exec` echoes the prompt, so the example block is
    always in there, and a diff that touches this file or its tests carries
    more. Accepting the last block unconditionally means a model reply with no
    block at all silently degrades into posting the EXAMPLE as findings -- a
    fabricated review, which is strictly worse than no review.
    """
    blocks = [b for b in _JSON_BLOCK.findall(output) if RUN_ID_PLACEHOLDER not in b]
    mine = [b for b in blocks if run_id in b]
    if not mine:
        raise TriageError(
            f"no {JSON_OPEN} block carrying run id {run_id} in the codex output; the "
            f"model did not follow the reply contract. Tail:\n{output[-800:]}"
        )
    payload = json.loads(mine[-1].strip())
    if payload.get("run") != run_id:
        raise TriageError(f"reply block names run {payload.get('run')!r}, not {run_id!r}")
    # EVERY contract violation below used to read as "clean". A missing
    # `findings` key defaulted to an empty list, and a list longer than the cap
    # was silently sliced. Both discard real findings while still posting a
    # current-head marker that satisfies coverage, which is the exact shape of
    # a check that cannot fail. Sol's own review of this file, P1 (Fri 5 Sep
    # 2026): "Reject malformed findings instead of treating them as clean".
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise TriageError(f"reply carries no findings list (got {type(raw_findings).__name__})")
    if len(raw_findings) > CFG.MAX_FINDINGS:
        raise TriageError(
            f"reply carries {len(raw_findings)} findings, over the {CFG.MAX_FINDINGS} cap; "
            "truncating here would drop findings the model actually made"
        )
    findings = []
    for raw in raw_findings:
        severity = str(raw.get("severity", "")).upper()
        if severity not in BADGE_COLORS:
            raise TriageError(f"finding carries an unknown severity {severity!r}: {raw}")
        for field in ("path", "title"):
            if not str(raw.get(field, "")).strip():
                raise TriageError(f"finding is missing a {field}: {raw}")
        line = raw.get("line")
        findings.append(
            Finding(
                path=str(raw["path"]),
                line=int(line) if isinstance(line, int) else None,
                severity=severity,
                title=str(raw["title"]).strip(),
                detail=str(raw.get("detail", "")).strip(),
            )
        )
    return findings


# ----- posting ------------------------------------------------------------


def _withheld(text: str) -> str:
    """Blank a title that would make the summary self-report as a non-review.

    See `_COVERAGE_POISON`. The finding itself is untouched in its own thread;
    only its appearance in the coverage-scanned summary is replaced.
    """
    lowered = text.lower()
    if any(p in lowered for p in _COVERAGE_POISON):
        return "(title withheld here; it contains a coverage-marker phrase. See the thread.)"
    return text


def summary_body(
    sha: str,
    seat: str,
    model: str,
    findings: list[Finding],
    carried: list[Finding],
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
            lines += [f"- {f.verdict} {f.severity}: {where} -- {_withheld(f.title)}"]
            if f.detail:
                lines += [f"  {_withheld(f.detail)}"]
    lines += ["", marker(sha, seat, model)]
    return "\n".join(lines)


def post_review(
    pr: str,
    sha: str,
    seat: str,
    model: str,
    findings: list[Finding],
) -> str:
    # The head was pinned before a model run that can last twenty minutes, and
    # was never re-read. A branch that advanced in that window would get a
    # review of the tree BEFORE the push, marked as covering the tree after
    # it -- the stale-round failure issue #1016 already fixed on the reading
    # side. Sol's own review of this file, P2 (Fri 5 Sep 2026).
    if (now := _pinned_head(pr)) != sha:
        raise TriageError(
            f"PR #{pr} moved from {sha} to {now} while the review ran; nothing posted. "
            "Re-run against the new head."
        )
    anchors = anchorable_lines(_diff(pr))
    inline, carried = _split_by_anchor(findings, anchors)
    payload = {
        "commit_id": sha,
        "event": "COMMENT",
        "body": summary_body(sha, seat, model, findings, carried),
        "comments": [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body(sha, seat, model)}
            for f in inline
        ],
    }
    endpoint = f"repos/{REPO}/pulls/{pr}/reviews"
    out = _gh(["api", endpoint, "--input", "-", "-q", ".html_url"], payload)
    print(f"[sol-review] posted {len(inline)} inline thread(s), {len(carried)} carried in summary")
    return out.strip()


# ----- CLI ----------------------------------------------------------------


def run(pr: str, seat: str, dry_run: bool, force: bool) -> int:
    sha = _pinned_head(pr)
    print(f"[sol-review] PR #{pr} pinned at {sha}")
    if not force and _existing_sol_review(pr, sha):
        print(
            f"[sol-review] a Sol review already exists for {sha}; nothing to do (--force to redo)"
        )
        return 0
    title = _gh(["pr", "view", pr, "--repo", REPO, "--json", "title", "-q", ".title"]).strip()
    diff = _diff(pr)
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
    prompt, _ = build_prompt(pr, sha, title, diff, run_id, CFG.MAX_DIFF_BYTES, CFG.MAX_FINDINGS)
    output, used, model = review_with_codex(prompt, seat)
    findings = parse_findings(output, run_id)
    tokens = _first_group(_TOKENS_LINE, output)
    print(f"[sol-review] seat={used} model={model} tokens={tokens} findings={len(findings)}")
    for finding in findings:
        anchor = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"  {finding.verdict} {finding.severity} {anchor} -- {finding.title}")
    if dry_run:
        print("[sol-review] dry run; nothing posted")
        return 0
    print(f"[sol-review] {post_review(pr, sha, used, model, findings)}")
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
