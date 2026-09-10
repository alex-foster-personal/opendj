"""AGENT-10 ground-truth probe: it must never answer a question it did not ask.

WHY THIS EXISTS. `scripts/audio_output_probe.py` is the only rung that observes
the actual output, so a wrong verdict from it sends a real outage
investigation down the wrong path. Three ways it could produce one:

  1. A play/pause order the bus accepts but the engine never applies. That is
     the Thu 10 Sep 2026 outage itself: HTTP 200 `succeeded` while `playing`
     stayed false. Both halves of an A/B pair would then be sampled in the SAME
     transport state, after which room noise alone decides between AUDIBLE and
     NOT AUDIBLE.
  2. Reading "AUDIBLE was not established" as "output is absent". With real
     music dynamics one quiet ON window turns 6/6 into 5/6, and NOT AUDIBLE
     there contradicts five pairs that showed signal.
  3. Leaving the deck it moved in the wrong state. It runs against a live app.

NO SIMULATED ENGINE. The decisions are tested directly, as pure functions over
real-shaped published state, rather than through a stand-in HTTP server
manufacturing engine replies: a fabricated `succeeded` is the exact input class
whose mistreatment is under test, so a fake one could only ever confirm this
file's author's belief about it. The control flow that cannot be reduced to a
pure decision is pinned against the production source's own syntax tree.

Regression lines:
  - if a toggle is treated as applied on the command reply alone then broken
  - if an untrusted presentation clock counts as reached then broken
  - if a mixed sign test reports NOT AUDIBLE then broken
  - if a unanimous sign test in EITHER direction reports UNKNOWN then broken
    (the overshoot: a probe that only ever says UNKNOWN answers nothing)
  - if the deck is not restored on the exception path then broken

[if] the probe returns a verdict it did not measure [then] fail, [else stop].
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import audio_output_probe as probe

pytestmark = pytest.mark.requirement("AGENT-10")

SOURCE = Path(probe.__file__).read_text(encoding="utf-8")


def _acoustic(cycles: int, wins: int, median: float = 1.0) -> dict:
    """A result shaped exactly as acoustic_sign_test returns one."""
    return {
        "cycles": cycles,
        "on_louder_in": wins,
        "off_louder_in": cycles - wins,
        "p_audible": 2.0**-cycles if wins == cycles else None,
        "p_silent": 2.0**-cycles if wins == 0 else None,
        "median_delta_db": median,
    }


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is no longer defined in {probe.__file__}")


# ----- the toggle must be observed, not assumed --------------------------


def test_a_reached_state_with_a_trusted_clock_counts() -> None:
    """The check must be able to pass, or the probe can never measure anything."""
    assert probe.transport_reached({"playing": True, "trust": "trusted"}, True) is True
    assert probe.transport_reached({"playing": False, "trust": "trusted"}, False) is True


def test_the_wrong_transport_state_does_not_count() -> None:
    """The outage itself: the bus said succeeded and `playing` stayed false."""
    assert probe.transport_reached({"playing": False, "trust": "trusted"}, True) is False
    assert probe.transport_reached({"playing": True, "trust": "trusted"}, False) is False


def test_an_untrusted_presentation_clock_does_not_count() -> None:
    """`playing` can be true while the schedule the listener gets is a revision
    behind. Sampling there measures the previous state."""
    assert probe.transport_reached({"playing": True, "trust": "untrusted"}, True) is False


def test_an_absent_reading_does_not_count_as_reached() -> None:
    """An unmeasured state must never render as a good one."""
    assert probe.transport_reached({}, True) is False
    assert probe.transport_reached({"playing": None, "trust": None}, True) is False


def test_set_deck_confirms_through_the_published_state() -> None:
    """The command reply alone must not end the wait.

    Pinned on the production syntax tree because the alternative is a stand-in
    engine that fabricates the very reply under test.
    """
    body = ast.unparse(_fn("_set_deck"))
    assert "transport_reached" in body, "the toggle must be confirmed against published state"
    assert "Unmeasurable" in body, "an unconfirmed toggle must refuse the measurement"


# ----- two hypotheses, and UNKNOWN in between ----------------------------


def test_a_unanimous_result_reports_audible() -> None:
    verdict, line = probe.acoustic_verdict(_acoustic(6, 6, 22.0))
    assert verdict == "AUDIBLE"
    assert "6/6" in line
    assert probe.VERDICT_EXIT[verdict] == 0


def test_the_opposite_unanimous_result_reports_not_audible() -> None:
    """NOT AUDIBLE is its own claim with its own unanimous test, not a default."""
    verdict, _ = probe.acoustic_verdict(_acoustic(6, 0, -0.2))
    assert verdict == "NOT AUDIBLE"
    assert probe.VERDICT_EXIT[verdict] == 1


@pytest.mark.parametrize("wins", [1, 2, 3, 4, 5])
def test_a_mixed_result_is_unknown_not_not_audible(wins: int) -> None:
    """Failing to establish AUDIBLE is not evidence of silence."""
    verdict, line = probe.acoustic_verdict(_acoustic(6, wins))
    assert verdict == "UNKNOWN", f"on louder in {wins}/6 establishes neither claim"
    assert probe.VERDICT_EXIT[verdict] == 3
    assert "inconclusive" in line


def test_five_of_six_is_not_reported_as_silence() -> None:
    """The concrete finding: five pairs showed signal, so silence is not the answer."""
    verdict, _ = probe.acoustic_verdict(_acoustic(6, 5, 3.0))
    assert verdict != "NOT AUDIBLE"


def test_a_probe_that_only_ever_says_unknown_would_be_the_overshoot() -> None:
    """The other direction of the same finding, which nothing in it objects to.

    Widening UNKNOWN until it swallows both unanimous cases satisfies "do not
    over-claim" perfectly and leaves a probe that answers nothing.
    """
    assert probe.acoustic_verdict(_acoustic(3, 3))[0] == "AUDIBLE"
    assert probe.acoustic_verdict(_acoustic(3, 0))[0] == "NOT AUDIBLE"


# ----- the deck goes back where it was -----------------------------------


def test_the_deck_is_restored_even_when_a_capture_fails() -> None:
    """A mic failure mid-run must not leave a live deck stopped.

    Structural, on the production tree: the restore has to be in a `finally`,
    because the ordinary success path would put it back either way and only the
    exception path distinguishes the two.
    """
    fn = _fn("acoustic_sign_test")
    tries = [node for node in ast.walk(fn) if isinstance(node, ast.Try) and node.finalbody]
    assert tries, "the A/B loop must sit in a try/finally that restores the deck"
    restored = any("_set_deck" in ast.unparse(stmt) for t in tries for stmt in t.finalbody)
    assert restored, "the finally block must put the deck back with _set_deck"


def test_the_entry_state_is_read_before_the_first_toggle() -> None:
    """Restoring to a state nobody read is restoring to a guess."""
    body = ast.unparse(_fn("acoustic_sign_test"))
    assert body.index("_deck_transport") < body.index("_set_deck"), (
        "the deck's own state must be read before the probe starts moving it"
    )
