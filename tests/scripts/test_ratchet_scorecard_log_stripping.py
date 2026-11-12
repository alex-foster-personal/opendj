"""scripts/ratchet_scorecard.py: gate keys, classes, fix kinds, scoring and verdicts.

The quality line and the pytest summary line are copied from real ci.yml job logs
(quality job 116355559982 and a pytest fast lane shard, Thu 15 Oct 2026).
"""

from __future__ import annotations

import pytest

from scripts import ratchet_scorecard as rs


def test_a_c1_st_terminated_string_does_not_swallow_the_failed_line() -> None:
    """Codex P1 at e81d76eb1f4, `strip_terminal_controls`: `_C1_INTRODUCERS` mapped the 8-bit
    introducers but not C1 ST (`\x9c`), so a string opened with one and closed with the other
    was unterminated for `_CONTROL_STRING` and ran to the newline. Measured on that sha:
    `strip_terminal_controls("\\x9d0;title\\x9cFAILED tests/a.py::t") -> ''`, so the FAILED line
    vanished from every parser that follows. PR-controlled log bytes could hide a gate failure.
    """
    for introducer in ("\x90", "\x98", "\x9d", "\x9e", "\x9f"):  # DCS, SOS, OSC, PM, APC
        swallowed = f"{introducer}0;title\x9cFAILED tests/a.py::t - boom\n"
        assert rs.strip_terminal_controls(swallowed) == "FAILED tests/a.py::t - boom\n"
        assert rs.pytest_keys(rs.failed_identities(rs.strip_terminal_controls(swallowed))) == ["pytest:tests/a.py"]
    # Controls: the ESC-ST form and plain text are unchanged, and a C1 ST on its own is a
    # control byte, not content.
    assert rs.strip_terminal_controls("\x9d0;title\x1b\\FAILED tests/a.py::t") == "FAILED tests/a.py::t"
    assert rs.strip_terminal_controls("plain FAILED tests/c.py::t") == "plain FAILED tests/c.py::t"
    assert rs.strip_terminal_controls("FAILED tests/c.py::t\x9c") == "FAILED tests/c.py::t"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # THREAD 1 (PRRT_kwDOSEvNd86uubcP): CAN/SUB cancel the sequence in progress and the
        # terminal returns to ground, so the text AFTER them is visible. The pattern stripper
        # treated the string as unterminated and ran it to the newline, deleting the whole
        # line -- `strip_terminal_controls` returned `""`, so a PR-controlled byte could hide
        # a `[quality] REGRESSION` marker from the scorecard that exists to surface it.
        ("\x1b]0;title\x18[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b]0;title\x1a[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        # THREAD 2 (PRRT_kwDOSEvNd86uubcb): a C0 control inside a CSI is EXECUTED and the
        # sequence continues to its final byte, so `ESC [ 31 BEL m` is SGR-red and the residue
        # `31m` must not survive as visible text (it parsed as `31m REGRESSION`, a dead key).
        ("[quality] \x1b[31\x07m REGRESSION: ruff.style", "[quality]  REGRESSION: ruff.style"),
        # The same class, unnamed by either thread: CAN cancels a CSI mid-parameters, and the
        # siblings the scanner covers for the same reason -- a CAN inside DCS, inside a
        # two-byte ESC sequence, and a SUB inside an OSC that also carries a BEL.
        ("\x1b[31\x18[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1bP1;2q\x18[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b7\x1a[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b]8;;https://evil.example\x18[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x9d0;title\x1a[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        # A C0 executed inside a CSI does not end it: BEL, TAB and a CR-ordered SGR all still
        # consume their whole sequence, and the visible text before and after is untouched.
        ("[quality] \x1b[38;5;196\x07m REGRESSION: ruff.style", "[quality]  REGRESSION: ruff.style"),
        ("[quality] \x1b[31\x09m REGRESSION: ruff.style", "[quality]  REGRESSION: ruff.style"),
        ("\x1b[31\x07m[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
    ],
)
def test_can_sub_cancel_and_c0_inside_a_sequence_is_executed(raw: str, expected: str) -> None:
    """Codex P2/BLOCKING at b018b06e2a, two threads: `_CONTROL_STRING` had no CAN/SUB
    cancellation, so `\\x1b]0;title\\x18[quality] REGRESSION: ruff.style` stripped to `''` --
    the regression marker gone, not merely the sequence -- and `_CSI` let a C0 byte inside it
    abort the sequence, leaving `31m` as visible text. Both are the same defect in the
    opposite direction from the one this PR fixes: a PR-controlled log byte that hides a red.
    """
    line = rs.strip_terminal_controls(raw)
    assert line == expected
    # Through the scorecard's own gate-key parser, because the defect was that it MISSED the
    # regression, not merely that the strip was untidy.
    assert rs.quality_keys([line]) == ["quality:ruff.style"]


def test_an_esc_inside_a_control_string_aborts_to_escape_state() -> None:
    """Codex P2/BLOCKING at 0de9f05689 (PRRT_kwDOSEvNd86uvJdz), `_scan_string_esc`: an ESC
    inside an OSC/DCS payload was payload unless the next byte was a backslash, so the string
    kept running to the newline and swallowed the line. xterm's parser instead leaves the
    string on ESC (`case OSC_STRING: if (c == ESC) state = ESCAPE`) and processes the next
    byte as an escape sequence, so the text after it is visible. Measured on that sha:
    `strip_terminal_controls("\\\\x1b]0;title\\\\x1b[31m[quality] REGRESSION: ruff.style") -> ''`,
    `quality_keys(...) -> []` -- a PR-controlled byte hiding the regression the scorecard
    exists to surface. Codex named the CSI row; the ESC 7 and DCS-opening-an-OSC rows below
    are the same transition and were not named by any thread.
    """
    for raw in (
        # ESC ends the OSC, and `[31m` then opens and closes a CSI.
        "\x1b]0;title\x1b[31m[quality] REGRESSION: ruff.style",
        # ESC ends the OSC and `7` is DECSC's final byte, so nothing of it is visible.
        "\x1b]0;title\x1b7[quality] REGRESSION: ruff.style",
        # A DCS is aborted by ESC, and the `]` that follows opens a real OSC, which BEL ends.
        "\x1bP1;2;q\x1b]0;t\x07[quality] REGRESSION: ruff.style",
        # ESC ESC: the second ESC restarts escape state, then `[31m` is a CSI.
        "\x1b]0;t\x1b\x1b[31m[quality] REGRESSION: ruff.style",
    ):
        line = rs.strip_terminal_controls(raw)
        assert line == "[quality] REGRESSION: ruff.style"
        assert rs.quality_keys([line]) == ["quality:ruff.style"]
    # ST (`ESC \\`) still ends the string rather than aborting it: the old path is unchanged.
    assert rs.strip_terminal_controls("\x1b]0;t\x1b\\[quality] REGRESSION: ruff.style") == (
        "[quality] REGRESSION: ruff.style"
    )


def test_the_stripper_keeps_text_the_terminal_would_show() -> None:
    """Controls for the scanner: cancelling restores ground rather than eating the rest of the
    line, and the sequences that already worked still terminate where they should."""
    # CAN after a bare ESC leaves the text after it visible.
    assert rs.strip_terminal_controls("\x1b\x18[quality] REGRESSION: ruff.style") == "[quality] REGRESSION: ruff.style"
    # A CAN *inside* an OSC payload is a cancellation too: the payload stops there, and the
    # bytes after the CAN -- which the terminal shows -- are kept.
    assert rs.strip_terminal_controls("\x1b]0;ti\x18tle\x07FAILED tests/a.py") == "tleFAILED tests/a.py"
    # An unterminated sequence still stops at the newline, and the newline survives.
    assert rs.strip_terminal_controls("\x1b]0;title\x18 no newline needed") == " no newline needed"
    assert rs.strip_terminal_controls("\x1b]0;unterminated\x07\nFAILED tests/a.py") == "\nFAILED tests/a.py"
    # BEL still ends only OSC: inside a DCS it is payload, so the tail inside stays hidden.
    assert rs.strip_terminal_controls("\x1bPpayload\x07FAILED tests/a.py\x1b\\") == ""
    # A C0 inside a CSI is executed, not a terminator, and does not leak its own byte.
    assert rs.strip_terminal_controls("\x1b[31\x07m") == ""
    assert rs.strip_terminal_controls("\x1b[31\x07mFAILED tests/a.py") == "FAILED tests/a.py"
