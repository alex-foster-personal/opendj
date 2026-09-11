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
import re
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
#
# Shapes below are what `/api/v1/state/ui-mirror` publishes per deck:
# `deckTransportClock` (audio-engine.svelte.ts:593) spreads `source`,
# `desired_revision` and `presented_revision` into `presentation_clock`.


def _published(playing: bool, source: str, desired: int = 7, presented: int = 7) -> dict:
    return {
        "playing": playing,
        "source": source,
        "desired_revision": desired,
        "presented_revision": presented,
    }


def test_a_playing_deck_presenting_output_counts() -> None:
    """The check must be able to pass, or the probe can never measure anything."""
    assert probe.transport_reached(_published(True, "audio_output"), True) is True


def test_a_paused_deck_counts_as_paused() -> None:
    """THE PAUSE HALF, which the mirror's own `trust` label cannot express.

    Every acoustic cycle pauses first. `deckTransportClock` publishes
    `source: 'paused_cursor'` whenever transport is inactive, and
    `ui-mirror.ts:64` therefore labels EVERY paused deck `untrusted`, so a
    confirmation keyed on `trust` waits out its timeout before the first OFF
    sample and the probe can never measure anything at all.
    """
    assert probe.transport_reached(_published(False, "paused_cursor"), False) is True


def test_the_wrong_transport_state_does_not_count() -> None:
    """The outage itself: the bus said succeeded and `playing` stayed false."""
    assert probe.transport_reached(_published(False, "paused_cursor"), True) is False
    assert probe.transport_reached(_published(True, "audio_output"), False) is False


def test_a_lagging_presented_revision_does_not_count() -> None:
    """`playing` can be true while the schedule the listener gets is a revision
    behind, so the deck is still sounding the PREVIOUS state."""
    assert probe.transport_reached(_published(True, "audio_output", 8, 7), True) is False
    assert probe.transport_reached(_published(False, "paused_cursor", 8, 7), False) is False


def test_a_clock_source_that_contradicts_the_direction_does_not_count() -> None:
    """Each direction has its OWN affirmative evidence, and neither may borrow
    the other's: a play confirmed by a paused cursor is not sounding."""
    assert probe.transport_reached(_published(True, "paused_cursor"), True) is False
    assert probe.transport_reached(_published(False, "audio_output"), False) is False


def test_an_absent_reading_does_not_count_as_reached() -> None:
    """An unmeasured state must never render as a good one."""
    assert probe.transport_reached({}, True) is False
    assert probe.transport_reached({"playing": None, "source": None}, True) is False
    assert probe.transport_reached(_published(True, "audio_output", None, None), True) is False


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


# ----- the shape is the PRODUCER's, not this file's ----------------------
#
# The pure-decision tests above take dicts written here. That is legitimate for
# a pure function (the dict IS the argument, not a stand-in for a dependency),
# but on its own it leaves one thing unverified and it is the thing that breaks
# in practice: whether those field names are the ones the app actually
# publishes. Rename `presented_revision` in the engine and every test above
# still passes while the probe reads `None` forever and can never confirm a
# toggle again.
#
# So the shape is pinned to the PRODUCTION SOURCES on both sides, and the
# assertions below are derived from them rather than restated here. Either side
# renaming a field turns this red.

_FRONTEND = Path(probe.__file__).resolve().parents[1] / "apps/webui/frontend/src/lib/rb"
_ENGINE_TS = _FRONTEND / "audio-engine.svelte.ts"
_MIRROR_TS = _FRONTEND / "ui-mirror.ts"


def _deck_transport_clock_fields() -> set[str]:
    """Field names `deckTransportClock` publishes, read out of the real source."""
    source = _ENGINE_TS.read_text(encoding="utf-8")
    start = source.index("export function deckTransportClock")
    body = source[start : source.index("\n}\n", start)]
    return set(re.findall(r"^\t\t(\w+):", body, flags=re.MULTILINE))


def _probe_clock_reads() -> set[str]:
    """Keys `_deck_transport` pulls out of `presentation_clock`.

    `ast.unparse` normalizes to single quotes, so the pattern matches those.
    The caller asserts the set is NON-EMPTY: a regex that stopped matching
    would otherwise make the contract check below pass by measuring nothing.
    """
    return set(re.findall(r"clock\.get\('(\w+)'\)", ast.unparse(_fn("_deck_transport"))))


def test_the_probe_reads_fields_the_engine_actually_publishes() -> None:
    """The cross-language contract, checked against both production sources."""
    published = _deck_transport_clock_fields()
    assert published, f"no fields parsed out of deckTransportClock in {_ENGINE_TS}"
    reads = _probe_clock_reads()
    assert reads, "no presentation_clock reads in _deck_transport; probe or check moved"
    missing = reads - published
    assert not missing, (
        f"{missing} is read from presentation_clock by scripts/audio_output_probe.py "
        f"but no longer published by deckTransportClock in {_ENGINE_TS}"
    )


def test_the_mirror_publishes_the_clock_under_the_key_the_probe_opens() -> None:
    """`presentation_clock` and `playing` are the mirror's names, not this file's."""
    mirror = _MIRROR_TS.read_text(encoding="utf-8")
    assert "presentation_clock: {" in mirror, f"{_MIRROR_TS} no longer publishes presentation_clock"
    assert "playing: deck.playing" in mirror, f"{_MIRROR_TS} no longer publishes a deck's playing"
    unparsed = ast.unparse(_fn("_deck_transport"))
    assert "published.get('presentation_clock'" in unparsed
    assert "published.get('playing')" in unparsed


def test_the_expected_source_values_are_the_engine_s_own_literals() -> None:
    """A direction confirmed against a source string the engine never emits is
    a confirmation that can never arrive."""
    source = _ENGINE_TS.read_text(encoding="utf-8")
    start = source.index("export function deckTransportClock")
    body = source[start : source.index("\n}\n", start)]
    emitted = set(re.findall(r"source: [^\n]*?'(\w+)' : '(\w+)'", body)[0])
    assert set(probe._SOURCE_FOR.values()) == emitted, (
        f"the probe expects {sorted(probe._SOURCE_FOR.values())} but deckTransportClock "
        f"emits {sorted(emitted)}"
    )


# ----- and against a real app when one is running ------------------------


def test_a_running_app_publishes_a_clock_this_probe_can_read() -> None:
    """The acceptance, run against the real HTTP path when it is available.

    UNAVAILABLE rather than mocked: a stand-in engine would answer with
    whatever this file believes the shape to be, which is the belief under
    test. When no app is running there is nothing to measure and the test says
    so instead of manufacturing a pass.
    """
    try:
        origin = probe._origin_from_lock()
        mirror = probe._get(origin, "/api/v1/state/ui-mirror")
    except Exception as cause:  # noqa: BLE001 - any failure to reach it is UNAVAILABLE
        pytest.skip(f"UNAVAILABLE: no running Open DJ engine to read ({cause})")
    decks = mirror.get("decks", {})
    assert decks, "a running engine published no decks at all"
    deck = int(sorted(decks)[0])
    entry = probe._deck_transport(origin, deck)
    assert isinstance(entry["playing"], bool), f"deck {deck} published playing={entry['playing']!r}"
    assert entry["source"] in set(probe._SOURCE_FOR.values()), (
        f"deck {deck} published an unrecognized clock source {entry['source']!r}"
    )
    assert isinstance(entry["desired_revision"], int)
    assert isinstance(entry["presented_revision"], int)
    # The predicate must reach a decision on real published state, and the
    # opposite direction must not also be satisfiable by the same reading.
    assert isinstance(probe.transport_reached(entry, entry["playing"]), bool)
    assert probe.transport_reached(entry, not entry["playing"]) is False
