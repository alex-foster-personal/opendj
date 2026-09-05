"""What the Sol reviewer is asked to look for, and how it must reply.

Split from `scripts/sol_review.py` (which owns the mechanics: seats, head
pinning, anchors, posting) so the REVIEW CRITERIA can be edited by someone
tuning what counts as a finding here, without reading a line of subprocess or
GitHub-API code. The reply contract lives here too rather than with the
parser, because the prompt and the fence are one agreement: change the fence
in one place and the other half stops reading it.

Requirements (mini-PRD):
  / The prompt states this repo's own blocking criteria, not generic ones.
    [if it asks for style nits then broken]
  / The reply must prove it came from THIS run.
    [if an echoed example block can be read as findings then broken]
  / A diff too large to send is truncated and SAID to be truncated.
    [if a partial review reports as a whole one then broken]
"""

from __future__ import annotations

#: The model's answer is fenced by these so it survives codex's own logging,
#: which interleaves hook lines and MCP warnings with the reply.
JSON_OPEN, JSON_CLOSE = "<<<SOL_JSON", "SOL_JSON>>>"

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
Style-only nits, matters of taste, and speculation about code you cannot see
are NOT blocking and usually not worth a finding at all.

Severity, and be strict about it: P0 breaks production or loses data. P1 is a
correctness, security or contract defect that must not merge. P2 is a real but
survivable defect. P3 is a small correctness or clarity improvement. Do not
inflate; a review whose findings are all P1 gets ignored, and so does one that
tiers a data-loss bug as P2.

Report at most {max_findings} findings, best first. Report ZERO findings if the
diff is clean; an empty list is a legitimate and useful review.

Every finding must name a file path exactly as it appears in the diff, and a
line number on the NEW side of the diff (a line the diff shows as added or as
context). If you cannot tie a finding to a line, set "line" to null.

PR #{pr}, head {sha}
Title: {title}

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

{diff}
"""


def build_prompt(
    pr: str,
    sha: str,
    title: str,
    diff: str,
    run_id: str,
    max_diff_bytes: int,
    max_findings: int,
) -> tuple[str, bool]:
    """The prompt, and whether the diff had to be truncated to fit.

    Truncation is returned rather than swallowed so the posted review can SAY
    it reviewed part of the diff. A partial review that reports as a whole one
    is the same failure as a coverage figure quoted against the wrong
    denominator.
    """
    truncated = len(diff.encode()) > max_diff_bytes
    body = diff.encode()[:max_diff_bytes].decode(errors="ignore") if truncated else diff
    note = (
        f"NOTE: the diff is truncated at {max_diff_bytes} bytes. Review what is here."
        if truncated
        else ""
    )
    return (
        PROMPT.format(
            max_findings=max_findings,
            pr=pr,
            sha=sha,
            title=title,
            open=JSON_OPEN,
            close=JSON_CLOSE,
            run_id=run_id,
            placeholder=RUN_ID_PLACEHOLDER,
            truncation=note,
            diff=body,
        ),
        truncated,
    )
