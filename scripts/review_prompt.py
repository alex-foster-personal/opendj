"""What a CLI reviewer is asked to look for, and how it must reply.

Split from `scripts/sol_review.py` (which owns the mechanics: seats, head
pinning, anchors, posting) so the REVIEW CRITERIA can be edited by someone
tuning what counts as a finding here, without reading a line of subprocess or
GitHub-API code. The reply contract lives here too rather than with the
parser, because the prompt and the fence are one agreement: change the fence
in one place and the other half stops reading it.

SHARED BY BOTH LANES since the Claude lane landed (Sun 6 Sep 2026), and it
was renamed from `sol_prompt.py` in the same change. The review CRITERIA are
a property of THIS REPOSITORY, not of which model reads them, so a second
copy would mean a criterion tightened for one reviewer and not the other --
the two lanes would then disagree about what is blocking here, which is worse
than either standard alone. What differs per lane is the FENCE, so that is a
parameter rather than a module constant.

Requirements (mini-PRD):
  / The prompt states this repo's own blocking criteria, not generic ones.
    [if it asks for style nits then broken]
  / The reply must prove it came from THIS run.
    [if an echoed example block can be read as findings then broken]
  / A diff too large to send is truncated and SAID to be truncated.
    [if a partial review reports as a whole one then broken]
  / The repo-specific blocking rules (OSSPUB-01 real identities, a weekday that
    contradicts its date, a diff that does not do what the PR claims) are stated
    as BLOCKING and may never be waved through as nits (REVIEW-20).
    [if the rubric omits any of the three then broken]
    [if the rubric lets a blocking-rule hit be downgraded to a nit then broken]
  / The PR body reaches the reviewer, so the intent check has the claims to test.
    [if the body is dropped from the prompt then broken]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


#: The model's answer is fenced by these so it survives the CLI's own
#: logging, which interleaves hook lines and MCP warnings with the reply. One
#: pair per lane, because a transcript that quotes the other lane's source or
#: tests then cannot be mistaken for this lane's answer.
class Fence(NamedTuple):
    """The pair of tokens one lane's reply is wrapped in."""

    open: str
    close: str


SOL_FENCE = Fence("<<<SOL_JSON", "SOL_JSON>>>")
CLAUDE_FENCE = Fence("<<<CLAUDE_JSON", "CLAUDE_JSON>>>")
GROK_FENCE = Fence("<<<GROK_JSON", "GROK_JSON>>>")
CURSOR_FENCE = Fence("<<<CURSOR_JSON", "CURSOR_JSON>>>")
#: Every lane's fence. The parser's unfenced fallback must skip what sits inside
#: ANY of them: a block fenced for another lane is never this lane's answer.
ALL_FENCES = (SOL_FENCE, CLAUDE_FENCE, GROK_FENCE, CURSOR_FENCE)

#: The placeholder the example block below carries where a real reply must
#: carry this run's id. `codex exec` echoes the prompt into its own stdout, so
#: the transcript the parser reads always contains this example verbatim -- and
#: on a PR that touches these files, the diff carries more blocks still. Found
#: by running the lane against its own PR (#1227, Fri 5 Sep 2026): a model
#: reply that omitted the block would have left the ECHOED EXAMPLE as the last
#: block in the transcript, and the lane would have posted
#: "path/to/file.py:42 one line, imperative" as a P1 finding on a real PR. A
#: fabricated review is worse than no review.
RUN_ID_PLACEHOLDER = "REPLACE_WITH_RUN_ID"

PROMPT = """You are reviewing one pull request diff for the music-dj-tools repository.

Repository conventions that make a finding BLOCKING here:
- A test that cannot fail (asserts nothing real, or its subject is mocked).
- Mocked data, hidden defaults, silent exception handling, or a fallback that
  masks a failure. This repo is deliberately brittle and fail-fast.
- A coverage, match-rate or accuracy number quoted against a denominator that
  is not the one being measured.
- Contract drift: an API response, generated type, or ledger shape changed on
  one side only.
- Data loss, a destructive operation without a bound on what being wrong
  costs, or a security hole.
- A timestamp labeled UTC that is not UTC.
- A real identity in the tracked tree. This repository is public. Two classes:
  (a) OSSPUB-01: a real home directory path, a personal mailbox, a tailnet
  hostname or a CGNAT address; (b) a real artist, track or album name in a
  fixture, example or test, where invented names are the rule (issue #5734).
  A provenance note, comment, changelog or test name that QUOTES the real name
  it replaced reintroduces it just as surely. Out of scope: the maintainer's
  first name in an attribution or dated decision, the fleet's machine
  nicknames, and the repository's documented placeholder home paths.
- A date whose weekday contradicts it, for example "Wed 8 Oct 2026" when
  8 Oct 2026 is a Thursday. Dates here always carry a weekday as a checksum,
  so work out the weekday for every dated line the diff adds before you
  accept it.
- The diff does not do what the PR title and body say it does: a claimed fix
  that leaves the defect, a claimed scrub or rename that leaves instances
  behind, or a stated scope the diff exceeds or falls short of. Read the title
  and body below and check the diff against each claim.

A hit on any rule above is a finding at P1 or higher, however small the edit
or however cosmetic it looks. It is never "just a nit", never "incomplete but
not a correctness bug", and never "style". If you notice one and are tempted
to downgrade it, report it instead.

Style-only nits, matters of taste, and speculation about code you cannot see
are NOT blocking and usually not worth a finding at all.

Severity, and be strict about it: P0 breaks production or loses data. P1 is a
correctness, security or contract defect that must not merge. P2 is a real but
survivable defect. P3 is a small correctness or clarity improvement. Do not
inflate; a review whose findings are all P1 gets ignored, and so does one that
tiers a data-loss bug as P2.

Report at most {max_findings} findings, best first. Report ZERO findings if the
diff is clean; an empty list is a legitimate and useful review.

Use no tools for this review: do not run commands, read files or search. The
diff below is the whole subject, and a tool call ends the run with no review
at all. Work out weekdays and every other check in your own reasoning.

Every finding must name a file path exactly as it appears in the diff, and a
line number on the NEW side of the diff (a line the diff shows as added or as
context). If you cannot tie a finding to a line, set "line" to null.

PR #{pr}, head {sha}
Title: {title}
Body (what the author claims this diff does):
{body}

Reply with NOTHING but this block, no prose before or after. Set "run" to
exactly {run_id} -- that literal string, not the placeholder shown here:
{open}
{{"run": "{placeholder}",
  "findings": [{{"path": "path/to/file.py", "line": 42, "severity": "P1",
  "title": "one line, imperative, under 100 chars",
  "detail": "2-6 sentences: what breaks, under what input, and what to change."}}]}}
{close}

The diff follows.
{truncation}
{withheld}

{diff}
"""


def withheld_prompt_line(dropped: list[str]) -> str:
    if not dropped:
        return ""
    return f"generated data withheld: {', '.join(dropped)}"


@dataclass(frozen=True)
class ReviewPromptConfig:
    run_id: str
    max_diff_bytes: int
    max_findings: int
    fence: Fence
    withheld: list[str]


def build_prompt(
    pr: str,
    sha: str,
    title: str,
    diff: str,
    config: ReviewPromptConfig,
    *,
    body: str,
) -> tuple[str, bool]:
    """The prompt, and whether the diff had to be truncated to fit.

    Truncation is returned rather than swallowed so the posted review can SAY
    it reviewed part of the diff. A partial review that reports as a whole one
    is the same failure as a coverage figure quoted against the wrong
    denominator.

    `fence` has no default on purpose. A default would make one lane's fence
    the silent answer for a caller that forgot to say which lane it is, and
    this repository's rule against hidden defaults exists for exactly that
    shape: the failure would be a Claude review whose reply is fenced as
    Sol's, parsed by neither.
    """
    truncated = len(diff.encode()) > config.max_diff_bytes
    sent_diff = (
        diff.encode()[: config.max_diff_bytes].decode(errors="ignore")
        if truncated
        else diff
    )
    note = (
        f"NOTE: the diff is truncated at {config.max_diff_bytes} bytes. Review what is here."
        if truncated
        else ""
    )
    withheld_line = withheld_prompt_line(config.withheld)
    return (
        PROMPT.format(
            max_findings=config.max_findings,
            pr=pr,
            sha=sha,
            title=title,
            body=body.strip() or "(the PR body is empty)",
            open=config.fence.open,
            close=config.fence.close,
            run_id=config.run_id,
            placeholder=RUN_ID_PLACEHOLDER,
            truncation=note,
            withheld=withheld_line,
            diff=sent_diff,
        ),
        truncated,
    )
