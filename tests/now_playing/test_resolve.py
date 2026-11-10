"""NOWP-11489: streamer now-playing fold (issue #10336).

[if] fold or answer disagrees with settle, winner, or stale rules [then] fail naming the case, [else stop].
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from apps.b2b_session.peer_now_playing import key_notations
from apps.now_playing.resolve import (
    PREVIOUS_MAX,
    SETTLE_DELAY_S,
    NowPlayingState,
    answer,
    fold,
    observe_decks,
)

pytestmark = pytest.mark.requirement("NOWP-11489")


def _mirror(
    decks: dict[int, tuple[str, float]],
    *,
    speakers_on: bool = True,
    extra: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    deck_objs: dict[str, Any] = {}
    channels: dict[str, Any] = {}
    meta = extra or {}
    for deck_n, (stable_id, gain) in decks.items():
        fields = meta.get(deck_n, {})
        deck_objs[str(deck_n)] = {
            "stable_id": stable_id,
            "title": fields.get("title", f"Title {deck_n}"),
            "artist": fields.get("artist", f"Artist {deck_n}"),
            "key": fields.get("key", "Am"),
            "bpm": fields.get("bpm", 124.0),
            "playing": True,
            "position": {"ms": 0},
        }
        channels[str(deck_n)] = {"fader": 1.0, "assign": "THRU", "on_air_gain": gain}
    body: dict[str, Any] = {
        "master_deck": 1,
        "decks": deck_objs,
        "mixer": {"crossfader": 0.5, "channels": channels},
    }
    if speakers_on:
        body["room_output"] = {"speakers_on": True, "room_path": "room_only"}
    return body


def _step(state: NowPlayingState, mirror: dict[str, Any], t: float) -> NowPlayingState:
    return fold(state, observe_decks(mirror), observed_mono=t, observed_wall=f"wall-{t}")


def test_deck_one_settles_after_three_seconds() -> None:
    mirror = _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 0.0)})
    state = NowPlayingState()
    for t in (0.0, 1.0, 2.0):
        state = _step(state, mirror, t)
        assert answer(state, stale=False)["track"] is None
        assert state.generation == 0
    state = _step(state, mirror, 3.0)
    doc = answer(state, stale=False)
    assert doc["track"]["stable_id"] == "sid-1"
    assert doc["track"]["deck"] == 1
    assert state.generation == 1


def test_switch_to_deck_two_after_settle() -> None:
    mirror = _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 0.0)})
    state = NowPlayingState()
    for t in range(4):
        state = _step(state, mirror, float(t))
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)}), 4.0)
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)}), 5.0)
    assert answer(state, stale=False)["track"]["stable_id"] == "sid-1"
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)}), 7.0)
    doc = answer(state, stale=False)
    assert doc["track"]["stable_id"] == "sid-2"
    assert doc["previous"][0]["stable_id"] == "sid-1"
    assert state.generation == 2


def test_under_settle_never_announced() -> None:
    state = NowPlayingState()
    base = _mirror({1: ("sid-1", 1.0)})
    for t in (0.0, 1.0, 2.0, 3.0):
        state = _step(state, base, t)
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)}), 4.0)
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)}), 6.0)
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 0.0)}), 6.5)
    state = _step(state, _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 0.0)}), 10.0)
    assert answer(state, stale=False)["previous"] == []
    assert state.generation == 1


def test_exactly_three_seconds_settles() -> None:
    state = NowPlayingState()
    mirror = _mirror({2: ("sid-2", 1.0)})
    state = _step(state, mirror, 0.0)
    state = _step(state, mirror, SETTLE_DELAY_S)
    assert state.generation == 1


def test_keys_and_nulls() -> None:
    camelot, musical = key_notations("Am")
    mirror = _mirror({1: ("sid-1", 1.0)}, extra={1: {"key": "Am", "artist": "  ", "bpm": None}})
    state = _step(_step(_step(NowPlayingState(), mirror, 0.0), mirror, 1.0), mirror, 3.0)
    track = answer(state, stale=False)["track"]
    assert track["key_camelot"] == camelot
    assert track["key_musical"] == musical
    assert track["artist"] is None
    assert track["bpm"] is None

    c2, m2 = key_notations("8A")
    mirror2 = _mirror({1: ("sid-1", 1.0)}, extra={1: {"key": "8A"}})
    state2 = _step(_step(_step(NowPlayingState(), mirror2, 0.0), mirror2, 1.0), mirror2, 3.0)
    track2 = answer(state2, stale=False)["track"]
    assert (track2["key_camelot"], track2["key_musical"]) == (c2, m2)


def test_tie_lowest_deck_wins() -> None:
    mirror = _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)})
    state = NowPlayingState()
    for t in (0.0, 1.0, 2.0, 3.0):
        state = _step(state, mirror, t)
    assert answer(state, stale=False)["track"]["deck"] == 1
    for t in range(4, 14):
        state = _step(state, mirror, float(t))
    assert state.generation == 1


def test_fallback_when_announced_fades() -> None:
    state = NowPlayingState()
    m1 = _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 0.0)})
    for t in range(4):
        state = _step(state, m1, float(t))
    m_both = _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 1.0)})
    for t in (4.0, 5.0, 6.0, 7.0):
        state = _step(state, m_both, t)
    assert answer(state, stale=False)["track"]["stable_id"] == "sid-2"
    m_back = _mirror({1: ("sid-1", 1.0), 2: ("sid-2", 0.0)})
    state = _step(state, m_back, 8.0)
    doc = answer(state, stale=False)
    assert doc["track"]["stable_id"] == "sid-1"
    prev_ids = [p["stable_id"] for p in doc["previous"]]
    assert "sid-2" in prev_ids
    assert prev_ids.count("sid-1") == 0


def test_generation_idempotent_and_same_stable_id() -> None:
    mirror = _mirror({1: ("sid-1", 1.0)})
    state = NowPlayingState()
    for t in range(4):
        state = _step(state, mirror, float(t))
    same = _step(state, mirror, 3.0)
    assert same == state
    for _ in range(20):
        state = _step(state, mirror, 4.0)
    assert state.generation == 1
    moved = _mirror({2: ("sid-1", 1.0)})
    for t in (5.0, 6.0, 7.0):
        state = _step(state, moved, t)
    assert state.generation == 1
    assert answer(state, stale=False)["track"]["deck"] == 1


def test_previous_capped() -> None:
    state = NowPlayingState()
    ids = [f"sid-{i}" for i in range(7)]
    t = 0.0
    for sid in ids:
        mirror = _mirror({1: (sid, 1.0)})
        for _ in range(4):
            state = _step(state, mirror, t)
            t += 1.0
    doc = answer(state, stale=False)
    assert len(doc["previous"]) == PREVIOUS_MAX


def test_gap_drops_runs_keeps_announcement() -> None:
    mirror = _mirror({1: ("sid-1", 1.0)})
    state = NowPlayingState()
    for t in range(4):
        state = _step(state, mirror, float(t))
    announced = answer(state, stale=False)["track"]
    state = _step(state, mirror, 8.0)
    assert answer(state, stale=False)["track"] == announced
    state = _step(state, mirror, 9.0)
    assert answer(state, stale=False)["track"] == announced
    state = _step(state, mirror, 12.0)
    assert answer(state, stale=False)["track"]["stable_id"] == "sid-1"


def test_solo_heard_without_link() -> None:
    mirror = _mirror({1: ("sid-1", 1.0)}, speakers_on=True)
    state = _step(_step(_step(NowPlayingState(), mirror, 0.0), mirror, 1.0), mirror, 3.0)
    assert answer(state, stale=False)["track"] is not None


def test_speakers_off_never_announced() -> None:
    mirror_off = _mirror({1: ("sid-1", 1.0)}, speakers_on=False)
    state = NowPlayingState()
    for t in range(10):
        state = _step(state, mirror_off, float(t))
    assert answer(state, stale=False)["track"] is None


def test_finite_bpm_rejects_nan() -> None:
    mirror = _mirror({1: ("sid-1", 1.0)}, extra={1: {"bpm": float("nan")}})
    state = _step(_step(_step(NowPlayingState(), mirror, 0.0), mirror, 1.0), mirror, 3.0)
    bpm = answer(state, stale=False)["track"]["bpm"]
    assert bpm is None or math.isfinite(bpm)
