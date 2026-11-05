"""B2BBOOTH-124 on the ENGINE: a transport op on a deck the AUDIO laptop has not reported ready for its
current load (file transferred and loaded there) is held, and commits once that report arrives; a held
play then starts from where the audio laptop's freshly loaded deck sits (its cue point), not from where
it would have been by then.

The audio laptop's page reports ``ready {deck, seq, pos_frames}`` (seq = the deck's committed load) after
each load, and for every deck when it takes the audio role (seq 0 on an empty deck), which is also how
the engine learns that page reports readiness at all: with no report from the audio side yet, nothing is
gated (an engine-only agent, or an older page, keeps today's behavior).

- [if] B loads its own track on deck 3 and plays, the audio laptop A not ready [then] the play waits; A's
  ready commits it at ready time plus its lead, from A's reported position
- [if] the ready names an older load, or comes from the control laptop [then] refused, the play still waits
- [if] another load lands first [then] the held play is dropped; that load's ready releases nothing
- [if] the play was held for a grab first (B2BBOOTH-123) [then] after the grant it still waits for ready
- [if] a held seek is released [then] it keeps its own position (only play and cue start from the cue point)
- [if] the deck is already ready, or the audio side never reports [then] a play commits at once (controls)

[if] a play on the audio laptop starts before its ready report or from the wrong cue [then] fail, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.b2b_booth.lease import GRAB_FALLBACK_US, RENEW_INTERVAL_US
from tests.b2b_booth.booth_pair import T0, Pair, same_booth

pytestmark = [pytest.mark.requirement("B2BBOOTH-124")]

TRACK = {"sha256": "12" * 32, "bytes": 9000, "title": "B only", "artist": "B", "bpm": 124.0, "key": "5A",
         "duration_s": 300.0, "owner_track_id": "rb-b1"}
LEAD = 150_000
CUE = 4_410.0


def _load(deck: int) -> dict[str, Any]:
    return {"op": "load", "deck": deck, "track": TRACK}


def _play(deck: int, now: int, pos: float = 96_000.0) -> dict[str, Any]:
    return {"op": "play", "deck": deck, "at_link_us": now + LEAD, "pos_frames": pos}


def _ready(deck: int, seq: int, pos: float = CUE) -> dict[str, Any]:
    return {"op": "ready", "deck": deck, "seq": seq, "pos_frames": pos}


def _names(applied: list[dict[str, Any]]) -> list[str]:
    return [c["op"]["op"] + (f":{c['op']['phase']}" if "phase" in c["op"] else "") for c in applied]


def _reporting(pair: Pair, now: int = T0 + 1) -> None:
    """The audio laptop's (inviter's) page takes the role: every empty deck reported ready at seq 0."""
    for deck in (1, 2, 3, 4):
        assert _names(pair.act("inviter", _ready(deck, 0, 0.0), now)) == ["ready"]


def _load_seq(pair: Pair, deck: int) -> int:
    return pair.inviter.state.load_seq[deck]


def test_a_play_waits_for_the_audio_laptop_then_starts_from_its_cue_point() -> None:
    pair = Pair()
    _reporting(pair)
    assert _names(pair.act("joiner", _load(3), T0 + 10)) == ["load"]
    assert pair.act("joiner", _play(3, T0 + 20), T0 + 20) == [], "if the play commits before A is ready then broken"
    t = T0 + 3_000_000  # the transfer took 3 s
    released = pair.act("inviter", _ready(3, _load_seq(pair, 3)), t)
    assert _names(released) == ["ready", "play"]
    assert released[1]["op"]["at_link_us"] == t + LEAD, "ready time plus the lead the play was sent with"
    assert released[1]["op"]["pos_frames"] == CUE, "from the audio laptop's cue point, not where it would have been"
    assert released[1]["by"] == "joiner"
    assert same_booth(pair.inviter, pair.joiner)


@pytest.mark.parametrize(("who", "seq_delta", "code"), [("joiner", 0, "not_audio_laptop"), ("inviter", -1, "stale_ready")])
def test_a_ready_from_the_control_laptop_or_for_an_older_load_is_refused(who: str, seq_delta: int, code: str) -> None:
    pair = Pair()
    _reporting(pair)
    pair.act("joiner", _load(3), T0 + 10)
    pair.act("joiner", _play(3, T0 + 20), T0 + 20)
    out = pair.core(who).intent_op(_ready(3, _load_seq(pair, 3) + seq_delta), T0 + 30)
    pair.flush(who, out, T0 + 30)
    refused = out.refused if who == "inviter" else [e for e in pair.events["joiner"] if e["type"] == "refusal"][-1]["reason"]
    assert refused == code
    assert pair.inviter.state.decks[3]["playing"] is False, "the play still waits"


def test_another_load_first_drops_the_held_play() -> None:
    pair = Pair()
    _reporting(pair)
    pair.act("joiner", _load(3), T0 + 10)
    pair.act("joiner", _play(3, T0 + 20), T0 + 20)
    pair.act("joiner", _load(3) | {"track": {**TRACK, "title": "Other"}}, T0 + 30)
    assert _names(pair.act("inviter", _ready(3, _load_seq(pair, 3)), T0 + 40)) == ["ready"]
    assert pair.inviter.state.decks[3]["playing"] is False
    assert pair.inviter.arbiter is not None and pair.inviter.arbiter.held_dropped == 1


def test_a_play_held_for_a_grab_still_waits_for_ready_after_the_grant() -> None:
    pair = Pair()
    _reporting(pair)
    for t in range(T0, T0 + 100_001, RENEW_INTERVAL_US):
        pair.renew("inviter", t)
    pair.act("joiner", _load(1), T0 + 100_000)
    pair.act("joiner", _play(1, T0 + 110_000), T0 + 110_000)
    assert _names(pair.tick(T0 + 100_000 + GRAB_FALLBACK_US)) == ["grab:granted", "load"], "granted and loaded, not played"
    released = pair.act("inviter", _ready(1, _load_seq(pair, 1)), T0 + 4_000_000)
    assert _names(released) == ["ready", "play"]
    assert released[1]["op"]["pos_frames"] == CUE


def test_a_held_seek_keeps_its_own_position() -> None:
    pair = Pair()
    _reporting(pair)
    pair.act("joiner", _load(3), T0 + 10)
    pair.act("joiner", {"op": "seek", "deck": 3, "at_link_us": T0 + 20, "pos_frames": 48_000.0}, T0 + 20)
    pair.act("joiner", _play(3, T0 + 30), T0 + 30)
    released = pair.act("inviter", _ready(3, _load_seq(pair, 3)), T0 + 1_000_000)
    assert [(c["op"]["op"], c["op"]["pos_frames"]) for c in released[1:]] == [("seek", 48_000.0), ("play", CUE)]


def test_controls_a_ready_deck_and_a_non_reporting_audio_side_play_at_once() -> None:
    pair = Pair()
    _reporting(pair)
    pair.act("joiner", _load(3), T0 + 10)
    pair.act("inviter", _ready(3, _load_seq(pair, 3)), T0 + 20)
    assert _names(pair.act("joiner", _play(3, T0 + 30), T0 + 30)) == ["play"], "a ready deck plays at once"
    quiet = Pair()  # the audio side never reported: today's behavior
    quiet.act("joiner", _load(3), T0 + 10)
    assert _names(quiet.act("joiner", _play(3, T0 + 20), T0 + 20)) == ["play"]
