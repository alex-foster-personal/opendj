"""Regression guard for the periodic-checks ledger state-marker selection in
``.github/workflows/periodic-checks.yml`` (DEVOPS-05).

Runs the workflow's OWN jq filter (extracted from the file, not a copy) so a
future edit that drifts from what actually executes cannot pass silently.
See review finding r3972781434: without an author restriction, ANY account
able to comment on the ledger issue can post the HTML state marker and have
it accepted as authoritative, because the original filter only checked
whether a comment's body contained the marker string.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "periodic-checks.yml"

MARKER_SHA = "a" * 40
FORGED_SHA = "f" * 40
MARKER = f"<!-- periodic-checks state sha={MARKER_SHA} threshold=50 -->"
FORGED_MARKER = f"<!-- periodic-checks state sha={FORGED_SHA} threshold=50 -->"


def _extract_jq_filter() -> str:
    """Pull the exact jq filter string out of the `Compute the window` step's
    `run:` script, between the single-quotes that bound the `--jq` argument.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"--jq '(\[\.\[\]\[\].*?empty end)'", text, re.DOTALL)
    assert match, "could not locate the state-marker jq filter in periodic-checks.yml"
    return match.group(1)


def _comment(login: str, body: str, created_at: str) -> dict:
    return {"user": {"login": login}, "body": body, "created_at": created_at}


def _select(pages: list[list[dict]]) -> tuple[str, str] | None:
    """Run the workflow's real jq filter against a synthetic `gh api
    --paginate --slurp` payload (a list of pages, each a list of comment
    objects) and decode the result the same way the bash step does."""
    proc = subprocess.run(
        ["jq", "-r", _extract_jq_filter()],
        input=json.dumps(pages),
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout.strip()
    if not out:
        return None
    created_at, body_b64 = out.split(" ", 1)
    return created_at, base64.b64decode(body_b64).decode("utf-8")


def test_a_forged_marker_from_an_unauthenticated_commenter_is_not_selected() -> None:
    """An account other than the workflow bot posts a marker AFTER the real
    bot's comment, claiming a chosen SHA. Picking the newest matching body
    regardless of author would let any commenter repeatedly reset the merge
    base and created_at, suppressing the periodic checks indefinitely -- the
    selector must ignore the forged comment and keep the bot's own state."""
    pages = [
        [
            _comment("github-actions[bot]", MARKER, "2026-09-01T00:00:00Z"),
            _comment("some-rando", FORGED_MARKER, "2026-09-02T00:00:00Z"),
        ]
    ]
    selected = _select(pages)
    assert selected is not None, "expected the bot's own marker to be selected"
    _created_at, body = selected
    assert MARKER_SHA in body
    assert FORGED_SHA not in body


def test_the_bot_authored_marker_is_still_selected() -> None:
    """OPPOSITE DIRECTION: a legitimate github-actions[bot] comment carrying
    the marker must still be picked -- the author restriction must not
    accidentally exclude the real state comment, which would bootstrap every
    window forever."""
    pages = [[_comment("github-actions[bot]", MARKER, "2026-09-01T00:00:00Z")]]
    selected = _select(pages)
    assert selected is not None, "the bot's own marker must still be selected"
    _created_at, body = selected
    assert MARKER_SHA in body


def _extract_base(base_body: str) -> str:
    """Run the workflow's own marker-extraction snippet (from the
    `base_hits="$(...)"` assignment through the single-hit validation right
    after the `combined` split) against a synthetic comment body, isolated
    from the surrounding git/gh calls so it can run without a real ledger
    issue."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r'(base_hits="\$\(printf.*?\n            fi\n)', text, re.DOTALL)
    assert match, "could not locate the marker-extraction snippet in periodic-checks.yml"
    script = f'base=""\nbase_body="$1"\n{match.group(1)}\nprintf "%s" "$base"\n'
    proc = subprocess.run(
        ["bash", "-c", script, "bash", base_body],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_a_body_with_exactly_one_marker_extracts_that_sha() -> None:
    """The ordinary case: one marker, one SHA extracted."""
    assert _extract_base(MARKER) == MARKER_SHA


def test_a_body_with_two_markers_is_not_trusted() -> None:
    """OPPOSITE DIRECTION: a body carrying two marker-shaped strings (a reply
    quoting the bot's own comment, say) must not silently hand back a
    multi-line `base` for the git commands downstream to choke on -- treated
    the same as no marker at all, which is what makes 'validate one exact
    marker before using it' from the review finding an explicit check rather
    than an accident of how the later git commands happen to fail."""
    two_markers = f"{MARKER}\n\nsome reply quoting it:\n> {FORGED_MARKER}"
    assert _extract_base(two_markers) == ""
