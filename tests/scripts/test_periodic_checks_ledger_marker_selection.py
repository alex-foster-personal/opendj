"""Regression guard for periodic-checks ledger state-marker selection (DEVOPS-02).

Runs the shipped `select_state_marker` (the function the window job executes),
not a copy of a jq filter. See review finding r3972781434: without an author
restriction, ANY account able to comment on the ledger issue can post the HTML
state marker and have it accepted as authoritative.
"""

from __future__ import annotations

from scripts.periodic_window import select_state_marker

MARKER_SHA = "a" * 40
FORGED_SHA = "f" * 40
MARKER = f"<!-- periodic-checks state sha={MARKER_SHA} threshold=50 -->"
FORGED_MARKER = f"<!-- periodic-checks state sha={FORGED_SHA} threshold=50 -->"


def _comment(login: str, body: str, created_at: str) -> dict:
    return {"user": {"login": login}, "body": body, "created_at": created_at}


def test_a_forged_marker_from_an_unauthenticated_commenter_is_not_selected() -> None:
    """An account other than the workflow bot posts a marker AFTER the real
    bot's comment, claiming a chosen SHA. Picking the newest matching body
    regardless of author would let any commenter repeatedly reset the merge
    base and created_at, suppressing the periodic checks indefinitely -- the
    selector must ignore the forged comment and keep the bot's own state."""
    comments = [
        _comment("github-actions[bot]", MARKER, "2026-09-01T00:00:00Z"),
        _comment("some-rando", FORGED_MARKER, "2026-09-02T00:00:00Z"),
    ]
    selected = select_state_marker(comments, row=None)
    assert selected is not None, "expected the bot's own marker to be selected"
    assert selected.sha == MARKER_SHA
    assert selected.sha != FORGED_SHA


def test_the_bot_authored_marker_is_still_selected() -> None:
    """OPPOSITE DIRECTION: a legitimate github-actions[bot] comment carrying
    the marker must still be picked -- the author restriction must not
    accidentally exclude the real state comment, which would bootstrap every
    window forever."""
    comments = [_comment("github-actions[bot]", MARKER, "2026-09-01T00:00:00Z")]
    selected = select_state_marker(comments, row=None)
    assert selected is not None, "the bot's own marker must still be selected"
    assert selected.sha == MARKER_SHA


def test_a_body_with_exactly_one_marker_extracts_that_sha() -> None:
    """The ordinary case: one marker, one SHA extracted."""
    selected = select_state_marker(
        [_comment("github-actions[bot]", MARKER, "2026-09-01T00:00:00Z")],
        row=None,
    )
    assert selected is not None
    assert selected.sha == MARKER_SHA


def test_a_body_with_two_markers_is_not_trusted() -> None:
    """OPPOSITE DIRECTION: a body carrying two marker-shaped strings (a reply
    quoting the bot's own comment, say) must not silently hand back a
    multi-line `base` for the git commands downstream to choke on -- treated
    the same as no marker at all, which is what makes 'validate one exact
    marker before using it' from the review finding an explicit check rather
    than an accident of how the later git commands happen to fail."""
    two_markers = f"{MARKER}\n\nsome reply quoting it:\n> {FORGED_MARKER}"
    selected = select_state_marker(
        [_comment("github-actions[bot]", two_markers, "2026-09-01T00:00:00Z")],
        row=None,
    )
    assert selected is None
