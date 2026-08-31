"""Acceptance tests for the Open DJ own-deck observer.

Each test maps to one plain-English acceptance criterion:

* if a track becomes audible on deck 1 then a ``track_loaded`` row
  exists with ``deck="1"`` and a UTC timestamp
* if a track is loaded but never audible then no row
* if a track keeps sounding then its row's ``audible_s`` keeps up
* if the engine restarts mid-set then rows written before the restart
  survive and new rows append after them
* if a snapshot describes an impossible deck state then it raises

There is deliberately NO capture-time dwell gate: a brief audition
writes a row too, carrying a small ``audible_s`` for a read-time filter
to judge. See docs/product/set-dwell-threshold-analysis.md.

The wire contract is a periodic deck-state snapshot at
:data:`SNAPSHOT_INTERVAL_S`-ish cadence, so the fixtures below feed
runs of snapshots rather than two lone endpoints.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.sets.sources.opendj_source import (
    ADVISORY_DWELL_S,
    MAX_SNAPSHOT_GAP_S,
    SOURCE_NAME,
    DeckObservationError,
    OpenDjDeckSource,
)
from apps.sets.state import SetsState

T0 = datetime(2026, 8, 31, 20, 0, 0, tzinfo=UTC)

#: Test feed cadence. Comfortably inside the gap clamp so a run of
#: snapshots credits its full wall-clock span.
CADENCE_S = 2.0

#: A run comfortably longer than the advisory read-time default, used
#: where a test wants a clearly-played track rather than an audition.
LONG_RUN_S = ADVISORY_DWELL_S + 4.0


def _deck(
    stable_id: str | None,
    *,
    playing: bool = False,
    audible: bool = False,
    position_ms: float = 0.0,
    duration_ms: float | None = 240_000.0,
    title: str | None = None,
    artist: str | None = None,
) -> dict:
    payload: dict = {
        "stable_id": stable_id,
        "playing": playing,
        "audible": audible,
        "position_ms": position_ms,
        "duration_ms": duration_ms,
    }
    if title is not None:
        payload["title"] = title
    if artist is not None:
        payload["artist"] = artist
    return payload


def _snapshot(offset_s: float, decks: dict[str, dict]) -> dict:
    """Build one wire snapshot at ``T0 + offset_s``."""
    return {
        "observed_at": (T0 + timedelta(seconds=offset_s)).isoformat(
            timespec="milliseconds"
        ),
        "decks": decks,
    }


def _run(
    start_s: float,
    span_s: float,
    decks: dict[str, dict],
    *,
    cadence_s: float = CADENCE_S,
) -> list[dict]:
    """Snapshots holding ``decks`` steady from ``start_s`` for ``span_s``."""
    out: list[dict] = []
    offset = 0.0
    while offset <= span_s:
        out.append(_snapshot(start_s + offset, decks))
        offset += cadence_s
    return out


def _source(sets_state: SetsState, session_id: str) -> OpenDjDeckSource:
    sets_state.open_session(session_id, capture_device="opendj-internal")
    return OpenDjDeckSource(
        session_id=session_id,
        state=sets_state,
        session_started_at=T0,
    )


def _feed(source: OpenDjDeckSource, snapshots: list[dict]) -> None:
    for snap in snapshots:
        source.submit(snap)
    source.poll_once()


# ---------------------------------------------------------------------------
# played / not-played
# ---------------------------------------------------------------------------


@pytest.mark.requirement("SET-01")
def test_audible_track_records_a_row_with_deck_and_utc_timestamp(
    sets_state: SetsState,
):
    """A track audible on deck 1 becomes one set row carrying its dwell."""
    src = _source(sets_state, "s-played")
    _feed(
        src,
        _run(
            0,
            LONG_RUN_S,
            {
                "1": _deck(
                    "trk-a",
                    playing=True,
                    audible=True,
                    title="A",
                    artist="X",
                    duration_ms=120_000.0,
                )
            },
        ),
    )

    rows = sets_state.fetch_events("s-played", action="track_loaded")
    assert len(rows) == 1
    row = rows[0]
    assert row.deck == "1"
    assert row.track_stable_id == "trk-a"
    assert row.source == SOURCE_NAME
    assert row.value["title"] == "A"
    assert row.value["artist"] == "X"
    assert row.value["audible_s"] == pytest.approx(LONG_RUN_S)
    # 64s audible of a 120s track.
    assert row.value["played_fraction"] == pytest.approx(LONG_RUN_S / 120.0, rel=1e-3)
    # Advisory only: stamped so the set records the filter default in force.
    assert row.value["threshold_s"] == ADVISORY_DWELL_S

    stamp = datetime.fromisoformat(row.wall_clock)
    assert stamp.tzinfo is not None, "wall_clock must be timezone-aware"
    assert stamp.utcoffset() == timedelta(0), "wall_clock must be UTC"


@pytest.mark.requirement("SET-01")
def test_loaded_but_never_played_records_nothing(sets_state: SetsState):
    """A deck holding a track it never sounds is not a play."""
    src = _source(sets_state, "s-idle")
    _feed(src, _run(0, LONG_RUN_S * 3, {"1": _deck("trk-a")}))
    assert sets_state.fetch_events("s-idle", action="track_loaded") == []


@pytest.mark.requirement("SET-01")
def test_a_brief_audition_is_recorded_with_its_short_dwell_not_discarded(
    sets_state: SetsState,
):
    """The regression this design exists to prevent.

    A 10s audition used to be deleted at capture by the 30s gate. It is
    now recorded with its real dwell so a read-time filter can judge it,
    and so the evidence survives if the filter is later changed.
    """
    src = _source(sets_state, "s-short")
    short = 10.0
    snaps = _run(0, short, {"1": _deck("trk-a", playing=True, audible=True)})
    snaps += _run(short + CADENCE_S, CADENCE_S, {"1": _deck("trk-a")})
    _feed(src, snaps)

    rows = sets_state.fetch_events("s-short", action="track_loaded")
    assert len(rows) == 1, "a short audition must still leave a row"
    assert rows[0].track_stable_id == "trk-a"
    assert rows[0].value["audible_s"] == pytest.approx(short + CADENCE_S)
    assert rows[0].value["audible_s"] < ADVISORY_DWELL_S


@pytest.mark.requirement("SET-01")
def test_playing_but_not_audible_does_not_count(sets_state: SetsState):
    """``playing`` is intent; ``audible`` is truth. Only truth counts."""
    src = _source(sets_state, "s-silent")
    _feed(
        src,
        _run(
            0,
            LONG_RUN_S * 2,
            {"1": _deck("trk-a", playing=True, audible=False)},
        ),
    )
    assert sets_state.fetch_events("s-silent", action="track_loaded") == []


@pytest.mark.requirement("SET-01")
def test_a_played_track_is_recorded_once_with_its_dwell_kept_current(
    sets_state: SetsState,
):
    """One row per play, updated in place -- not one row per snapshot."""
    src = _source(sets_state, "s-once")
    span = LONG_RUN_S * 3
    _feed(
        src,
        _run(0, span, {"1": _deck("trk-a", playing=True, audible=True)}),
    )
    rows = sets_state.fetch_events("s-once", action="track_loaded")
    assert len(rows) == 1
    assert rows[0].value["audible_s"] == pytest.approx(span)


@pytest.mark.requirement("SET-01")
def test_reloading_the_same_track_later_counts_as_a_second_play(
    sets_state: SetsState,
):
    """Load A, play it, swap to B, come back to A: two genuine plays."""
    src = _source(sets_state, "s-replay")
    snaps = _run(0, LONG_RUN_S, {"1": _deck("trk-a", playing=True, audible=True)})
    mid = LONG_RUN_S + CADENCE_S
    snaps += _run(mid, CADENCE_S, {"1": _deck("trk-b", playing=True, audible=True)})
    tail = mid + CADENCE_S * 2
    snaps += _run(
        tail, LONG_RUN_S, {"1": _deck("trk-a", playing=True, audible=True)}
    )
    _feed(src, snaps)

    rows = sets_state.fetch_events("s-replay", action="track_loaded")
    # trk-b was briefly audible in between, so it earns a row too, carrying
    # a small audible_s for a read-time filter to judge. The two trk-a rows
    # are separate generations, not one row updated twice.
    assert [r.track_stable_id for r in rows] == ["trk-a", "trk-b", "trk-a"]
    by_track = {r.track_stable_id: r for r in rows}
    assert by_track["trk-b"].value["audible_s"] < ADVISORY_DWELL_S
    assert rows[0].value["audible_s"] == pytest.approx(LONG_RUN_S)
    assert rows[2].value["audible_s"] == pytest.approx(LONG_RUN_S)


@pytest.mark.requirement("SET-01")
def test_the_row_appears_on_the_first_audible_second_then_grows(
    sets_state: SetsState,
):
    """Durability: the row must exist long before the track finishes.

    If it were only written at the end, a browser that died mid-track
    would lose the play entirely -- the exact failure the snapshot design
    exists to prevent.
    """
    src = _source(sets_state, "s-grow")
    audible = {"1": _deck("trk-a", playing=True, audible=True)}

    _feed(src, _run(0, CADENCE_S, audible))
    early = sets_state.fetch_events("s-grow", action="track_loaded")
    assert len(early) == 1, "row must exist after the first audible second"
    first_dwell = early[0].value["audible_s"]
    assert first_dwell == pytest.approx(CADENCE_S)
    row_id = src.status()["decks"]["1"]["event_id"]
    assert row_id is not None

    _feed(src, _run(CADENCE_S * 2, LONG_RUN_S, audible))
    later = sets_state.fetch_events("s-grow", action="track_loaded")
    assert len(later) == 1, "still one row, updated rather than duplicated"
    assert src.status()["decks"]["1"]["event_id"] == row_id, (
        "the same row was updated in place, not replaced"
    )
    assert later[0].value["audible_s"] > first_dwell


# ---------------------------------------------------------------------------
# deck handover
# ---------------------------------------------------------------------------


@pytest.mark.requirement("SET-01")
def test_handover_between_decks_emits_track_change(sets_state: SetsState):
    src = _source(sets_state, "s-change")
    deck_a = _deck("trk-a", playing=True, audible=True)
    snaps = _run(0, LONG_RUN_S, {"1": deck_a})
    snaps += _run(
        LONG_RUN_S + CADENCE_S,
        LONG_RUN_S,
        {"1": deck_a, "2": _deck("trk-b", playing=True, audible=True)},
    )
    _feed(src, snaps)

    loaded = sets_state.fetch_events("s-change", action="track_loaded")
    assert [(r.deck, r.track_stable_id) for r in loaded] == [
        ("1", "trk-a"),
        ("2", "trk-b"),
    ]
    changes = sets_state.fetch_events("s-change", action="track_change")
    assert len(changes) == 1
    assert changes[0].value["from_deck"] == "1"
    assert changes[0].value["to_deck"] == "2"
    assert changes[0].value["from_stable_id"] == "trk-a"
    assert changes[0].value["to_stable_id"] == "trk-b"


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------


@pytest.mark.requirement("SET-01")
def test_rows_before_an_engine_restart_survive_and_new_rows_append(
    sets_state: SetsState,
):
    """A restart loses the in-memory accumulator, never the recorded rows."""
    session_id = "s-restart"
    src = _source(sets_state, session_id)
    _feed(
        src,
        _run(0, LONG_RUN_S, {"1": _deck("trk-a", playing=True, audible=True)}),
    )
    before = sets_state.fetch_events(session_id, action="track_loaded")
    assert [r.track_stable_id for r in before] == ["trk-a"]

    # Engine restart: brand-new source object, same session + same DB.
    revived = OpenDjDeckSource(
        session_id=session_id,
        state=sets_state,
        session_started_at=T0,
    )
    _feed(
        revived,
        _run(
            LONG_RUN_S * 2,
            LONG_RUN_S,
            {"2": _deck("trk-b", playing=True, audible=True)},
        ),
    )
    after = sets_state.fetch_events(session_id, action="track_loaded")
    assert [r.track_stable_id for r in after] == ["trk-a", "trk-b"]
    assert [r.deck for r in after] == ["1", "2"]


@pytest.mark.requirement("SET-01")
def test_a_long_observation_gap_is_not_credited_as_continuous_play(
    sets_state: SetsState,
):
    """A backgrounded tab must not bank an hour of imaginary playback."""
    src = _source(sets_state, "s-gap")
    playing = {"1": _deck("trk-a", playing=True, audible=True)}
    _feed(src, [_snapshot(0, playing), _snapshot(3600, playing)])

    # The row exists (the deck WAS audible) but banks only the clamp,
    # never the hour the tab spent asleep.
    rows = sets_state.fetch_events("s-gap", action="track_loaded")
    assert len(rows) == 1
    assert rows[0].value["audible_s"] == pytest.approx(MAX_SNAPSHOT_GAP_S)
    assert src.status()["decks"]["1"]["audible_s"] == pytest.approx(
        MAX_SNAPSHOT_GAP_S
    )


# ---------------------------------------------------------------------------
# fail fast
# ---------------------------------------------------------------------------


@pytest.mark.requirement("SET-01")
def test_unknown_deck_id_raises(sets_state: SetsState):
    src = _source(sets_state, "s-baddeck")
    with pytest.raises(DeckObservationError, match="unknown deck"):
        src.submit(_snapshot(0, {"9": _deck("trk-a")}))


@pytest.mark.requirement("SET-01")
def test_audible_with_no_track_loaded_raises(sets_state: SetsState):
    """A deck cannot be sounding a track it does not hold."""
    src = _source(sets_state, "s-impossible")
    with pytest.raises(DeckObservationError, match="audible"):
        src.submit(_snapshot(0, {"1": _deck(None, playing=True, audible=True)}))


@pytest.mark.requirement("SET-01")
def test_missing_required_field_raises(sets_state: SetsState):
    src = _source(sets_state, "s-missing")
    payload = _deck("trk-a")
    del payload["audible"]
    with pytest.raises(DeckObservationError, match="audible"):
        src.submit(_snapshot(0, {"1": payload}))


@pytest.mark.requirement("SET-01")
def test_non_bool_transport_flag_raises(sets_state: SetsState):
    """Truthy is not true; the string "false" must not read as playing."""
    src = _source(sets_state, "s-truthy")
    payload = _deck("trk-a")
    payload["audible"] = "false"
    with pytest.raises(DeckObservationError, match="bool"):
        src.submit(_snapshot(0, {"1": payload}))


@pytest.mark.requirement("SET-01")
def test_naive_timestamp_raises(sets_state: SetsState):
    """No tz offset means we cannot know it is UTC, so we refuse it."""
    src = _source(sets_state, "s-naive")
    snap = {
        "observed_at": "2026-08-31T20:00:00.000",
        "decks": {"1": _deck("trk-a")},
    }
    with pytest.raises(DeckObservationError, match="UTC"):
        src.submit(snap)


@pytest.mark.requirement("SET-01")
def test_non_utc_timestamp_raises(sets_state: SetsState):
    src = _source(sets_state, "s-offset")
    snap = {
        "observed_at": "2026-08-31T20:00:00.000+02:00",
        "decks": {"1": _deck("trk-a")},
    }
    with pytest.raises(DeckObservationError, match="UTC"):
        src.submit(snap)


@pytest.mark.requirement("SET-01")
def test_out_of_order_snapshot_raises(sets_state: SetsState):
    src = _source(sets_state, "s-reorder")
    src.submit(_snapshot(10, {"1": _deck("trk-a")}))
    with pytest.raises(DeckObservationError, match="older"):
        src.submit(_snapshot(5, {"1": _deck("trk-a")}))


@pytest.mark.requirement("SET-01")
def test_negative_position_raises(sets_state: SetsState):
    src = _source(sets_state, "s-negpos")
    with pytest.raises(DeckObservationError, match="position_ms"):
        src.submit(_snapshot(0, {"1": _deck("trk-a", position_ms=-1.0)}))


@pytest.mark.requirement("SET-01")
def test_decks_must_be_a_mapping(sets_state: SetsState):
    src = _source(sets_state, "s-shape")
    with pytest.raises(DeckObservationError, match="decks"):
        src.submit({"observed_at": T0.isoformat(), "decks": []})


# ---------------------------------------------------------------------------
# agent-native read surface
# ---------------------------------------------------------------------------


@pytest.mark.requirement("SET-01")
def test_status_reports_pending_and_accumulated_dwell(sets_state: SetsState):
    src = _source(sets_state, "s-status")
    src.submit(_snapshot(0, {"1": _deck("trk-a", playing=True, audible=True)}))
    assert src.status()["pending_snapshots"] == 1
    src.poll_once()
    assert src.status()["pending_snapshots"] == 0

    src.submit(_snapshot(4, {"1": _deck("trk-a", playing=True, audible=True)}))
    src.poll_once()
    status = src.status()
    assert status["source"] == SOURCE_NAME
    assert status["advisory_dwell_s"] == ADVISORY_DWELL_S
    assert status["gates_capture"] is False
    deck_one = status["decks"]["1"]
    assert deck_one["stable_id"] == "trk-a"
    assert deck_one["audible_s"] == pytest.approx(4.0)
    # Audible for 4s, well under the advisory 60s, and recorded anyway.
    assert deck_one["recorded"] is True
    assert deck_one["event_id"] is not None
