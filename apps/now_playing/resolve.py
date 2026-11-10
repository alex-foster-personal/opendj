"""NOWP: which one track the room hears, from the performance ui-mirror (issue #10336).

Serato Live Playlists and community OBS overlays announce a track once it is actually heard, not on
load. Heard means LINKAIR-01 with ``link_joined=True``: LINK only gates the B2B ON AIR tag; a solo
streamer's room still hears this laptop (Speakers on, mixer gain at least ``ON_AIR_GAIN_EPSILON``).
An ``on_air`` of ``None`` counts as not heard.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from apps.b2b_session.on_air import OnAirContext
from apps.b2b_session.peer_now_playing import MAX_MIRROR_AGE_S, shape_now_playing

SETTLE_DELAY_S: Final[float] = 3.0
PREVIOUS_MAX: Final[int] = 5


@dataclass(frozen=True)
class Track:
    stable_id: str
    artist: str | None
    title: str | None
    key_camelot: str | None
    key_musical: str | None
    bpm: float | None
    deck: int
    since: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "artist": _text_json(self.artist),
            "title": _text_json(self.title),
            "key_camelot": self.key_camelot,
            "key_musical": self.key_musical,
            "bpm": _bpm_json(self.bpm),
            "deck": self.deck,
            "since": self.since,
        }


@dataclass(frozen=True)
class DeckObservation:
    deck: int
    stable_id: str
    artist: str | None
    title: str | None
    key_camelot: str | None
    key_musical: str | None
    bpm: float | None
    heard: bool


@dataclass(frozen=True)
class HeardRun:
    track: Track
    started_mono: float
    settled: bool


@dataclass(frozen=True)
class NowPlayingState:
    runs: tuple[HeardRun, ...] = ()
    track: Track | None = None
    previous: tuple[Track, ...] = ()
    generation: int = 0
    last_observed_mono: float | None = None


def _text_json(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _bpm_json(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def _finite_bpm(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _optional_str(raw: Any) -> str | None:
    return raw if isinstance(raw, str) else None


def observe_decks(mirror: Mapping[str, Any]) -> list[DeckObservation]:
    """One observation per loaded deck; ``[]`` when ``decks`` is not a dict."""
    context = OnAirContext.from_mirror(dict(mirror), link_joined=True)
    try:
        shaped = shape_now_playing(dict(mirror), 0.0, "", MAX_MIRROR_AGE_S, context)
    except TypeError:
        return []
    out: list[DeckObservation] = []
    for deck in shaped.get("decks") or []:
        if not isinstance(deck, dict):
            continue
        stable_id = deck.get("stable_id")
        deck_n = deck.get("deck")
        if not isinstance(stable_id, str) or not stable_id or not isinstance(deck_n, int):
            continue
        out.append(
            DeckObservation(
                deck=deck_n,
                stable_id=stable_id,
                artist=_optional_str(deck.get("artist")),
                title=_optional_str(deck.get("title")),
                key_camelot=deck.get("key_camelot") if isinstance(deck.get("key_camelot"), str) else None,
                key_musical=deck.get("key_musical") if isinstance(deck.get("key_musical"), str) else None,
                bpm=_finite_bpm(deck.get("bpm")),
                heard=deck.get("on_air") is True,
            )
        )
    return out


def _track_from_obs(obs: DeckObservation, since: str | None) -> Track:
    return Track(
        stable_id=obs.stable_id,
        artist=obs.artist,
        title=obs.title,
        key_camelot=obs.key_camelot,
        key_musical=obs.key_musical,
        bpm=obs.bpm,
        deck=obs.deck,
        since=since,
    )


def _runs_by_deck(runs: tuple[HeardRun, ...]) -> dict[int, HeardRun]:
    return {run.track.deck: run for run in runs}


def _rebuild_runs(
    prior: tuple[HeardRun, ...],
    decks: list[DeckObservation],
    *,
    observed_mono: float,
    observed_wall: str | None,
) -> list[HeardRun]:
    by_deck = _runs_by_deck(prior)
    rebuilt: list[HeardRun] = []
    for obs in decks:
        if not obs.heard:
            continue
        kept = by_deck.get(obs.deck)
        if kept is not None and kept.track.stable_id == obs.stable_id:
            rebuilt.append(kept)
            continue
        rebuilt.append(
            HeardRun(
                track=_track_from_obs(obs, observed_wall),
                started_mono=observed_mono,
                settled=False,
            )
        )
    return rebuilt


def _mark_settled(
    runs: list[HeardRun], *, observed_mono: float, settle_s: float
) -> tuple[list[HeardRun], list[HeardRun]]:
    updated: list[HeardRun] = []
    newly: list[HeardRun] = []
    for run in runs:
        if not run.settled and observed_mono - run.started_mono >= settle_s:
            settled_run = replace(run, settled=True)
            updated.append(settled_run)
            newly.append(settled_run)
        else:
            updated.append(run)
    return updated, newly


def _pick_winner(candidates: list[HeardRun]) -> HeardRun:
    return min(candidates, key=lambda r: (r.started_mono, r.track.deck))


def _heard_runs(runs: list[HeardRun], decks: list[DeckObservation]) -> list[HeardRun]:
    heard_decks = {obs.deck for obs in decks if obs.heard}
    return [run for run in runs if run.track.deck in heard_decks]


def _push_previous(previous: tuple[Track, ...], old: Track, new: Track) -> tuple[Track, ...]:
    skip = {old.stable_id, new.stable_id}
    tail = tuple(p for p in previous if p.stable_id not in skip)
    return (old, *tail)[:PREVIOUS_MAX]


def _announce(
    state: NowPlayingState,
    winner: HeardRun,
) -> NowPlayingState:
    old = state.track
    if old is not None and old.stable_id == winner.track.stable_id:
        return state
    previous = state.previous if old is None else _push_previous(state.previous, old, winner.track)
    return replace(state, track=winner.track, previous=previous, generation=state.generation + 1)


def fold(
    state: NowPlayingState,
    decks: list[DeckObservation],
    *,
    observed_mono: float,
    observed_wall: str | None,
    settle_s: float = SETTLE_DELAY_S,
    gap_s: float = MAX_MIRROR_AGE_S,
) -> NowPlayingState:
    """Rules 2-6. Idempotent for ``observed_mono <= state.last_observed_mono``."""
    if state.last_observed_mono is not None and observed_mono <= state.last_observed_mono:
        return state

    prior_runs = state.runs
    if state.last_observed_mono is not None and observed_mono - state.last_observed_mono > gap_s:
        prior_runs = ()

    runs = _rebuild_runs(prior_runs, decks, observed_mono=observed_mono, observed_wall=observed_wall)
    runs, newly_settled = _mark_settled(runs, observed_mono=observed_mono, settle_s=settle_s)

    next_state = replace(state, runs=tuple(runs), last_observed_mono=observed_mono)

    if newly_settled:
        winner = _pick_winner(newly_settled)
        next_state = _announce(next_state, winner)
    elif next_state.track is not None:
        announced_id = next_state.track.stable_id
        heard = _heard_runs(runs, decks)
        if not any(r.track.stable_id == announced_id for r in heard):
            others = [r for r in heard if r.settled and r.track.stable_id != announced_id]
            if others:
                winner = _pick_winner(others)
                next_state = _announce(next_state, winner)

    return next_state


def answer(state: NowPlayingState, *, stale: bool) -> dict[str, Any]:
    track = None if state.track is None else state.track.to_json()
    return {
        "track": track,
        "previous": [t.to_json() for t in state.previous],
        "stale": stale,
        "generation": state.generation,
    }
