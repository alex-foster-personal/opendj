"""Wire format for Open DJ deck-state snapshots: types and validation.

Split from :mod:`apps.sets.sources.opendj_source` so that parsing what the
browser sent stays separate from accumulating dwell and writing rows. This
module is pure: it decides only whether a payload is interpretable, never
what it means for a set.

Validation is deliberately strict and raises :class:`DeckObservationError`
rather than coercing. A truthy string quietly read as ``True``, or a naive
timestamp assumed to be UTC, would corrupt a set recording in a way nobody
would notice until the tracklist was wrong.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

#: The decks Open DJ exposes (``DeckId = 1|2|3|4`` in the frontend).
KNOWN_DECK_IDS: frozenset[str] = frozenset({"1", "2", "3", "4"})

_REQUIRED_DECK_FIELDS: tuple[str, ...] = (
    "stable_id",
    "playing",
    "audible",
    "position_ms",
)



class DeckObservationError(ValueError):
    """A snapshot could not be interpreted. Never guessed around."""


# ---------------------------------------------------------------------------
# wire types
# ---------------------------------------------------------------------------



@dataclass(frozen=True)
class DeckObservation:
    """One deck's state at one instant, already validated."""

    deck: str
    stable_id: str | None
    playing: bool
    audible: bool
    position_ms: float
    duration_ms: float | None
    title: str | None
    artist: str | None


@dataclass(frozen=True)
class DeckSnapshot:
    """Every deck's state at one instant."""

    observed_at: datetime
    decks: tuple[DeckObservation, ...]



# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------




def _require_bool(value: Any, field: str, deck: str) -> bool:
    if not isinstance(value, bool):
        raise DeckObservationError(
            f"deck {deck}: {field} must be a bool, got "
            f"{type(value).__name__} {value!r}"
        )
    return value


def _require_number(value: Any, field: str, deck: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeckObservationError(
            f"deck {deck}: {field} must be a number, got "
            f"{type(value).__name__} {value!r}"
        )
    return float(value)


def _parse_utc(raw: Any) -> datetime:
    """Parse an explicitly-UTC ISO 8601 stamp. Naive input is refused."""
    if not isinstance(raw, str):
        raise DeckObservationError(
            f"observed_at must be an ISO 8601 UTC string, got {type(raw).__name__}"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeckObservationError(
            f"observed_at is not ISO 8601: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise DeckObservationError(
            f"observed_at {raw!r} has no offset; it must be explicitly UTC"
        )
    if parsed.utcoffset() != timedelta(0):
        raise DeckObservationError(
            f"observed_at {raw!r} is not UTC; send a UTC stamp"
        )
    return parsed


def _require_deck_envelope(deck_id: Any, payload: Any) -> None:
    """The deck is one we know and its payload carries every field."""
    if not isinstance(deck_id, str) or deck_id not in KNOWN_DECK_IDS:
        raise DeckObservationError(
            f"unknown deck {deck_id!r}; known decks are {sorted(KNOWN_DECK_IDS)}"
        )
    if not isinstance(payload, Mapping):
        raise DeckObservationError(
            f"deck {deck_id}: state must be an object, got "
            f"{type(payload).__name__}"
        )
    for field in _REQUIRED_DECK_FIELDS:
        if field not in payload:
            raise DeckObservationError(f"deck {deck_id}: missing {field}")


def _require_stable_id(value: Any, deck_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeckObservationError(
            f"deck {deck_id}: stable_id must be a string or null"
        )
    if not value:
        raise DeckObservationError(
            f"deck {deck_id}: stable_id must not be empty; use null for an "
            "empty deck"
        )
    return value


def _require_position_ms(value: Any, deck_id: str) -> float:
    position_ms = _require_number(value, "position_ms", deck_id)
    if position_ms < 0:
        raise DeckObservationError(
            f"deck {deck_id}: position_ms must not be negative, got {position_ms}"
        )
    return position_ms


def _require_duration_ms(value: Any, deck_id: str) -> float | None:
    if value is None:
        return None
    duration_ms = _require_number(value, "duration_ms", deck_id)
    if duration_ms <= 0:
        raise DeckObservationError(
            f"deck {deck_id}: duration_ms must be positive, got {duration_ms}"
        )
    return duration_ms


def _parse_deck(deck_id: Any, payload: Any) -> DeckObservation:
    _require_deck_envelope(deck_id, payload)
    stable_id = _require_stable_id(payload["stable_id"], deck_id)
    playing = _require_bool(payload["playing"], "playing", deck_id)
    audible = _require_bool(payload["audible"], "audible", deck_id)
    if audible and stable_id is None:
        raise DeckObservationError(
            f"deck {deck_id}: audible with no track loaded is not a state a "
            "deck can be in"
        )
    return DeckObservation(
        deck=deck_id,
        stable_id=stable_id,
        playing=playing,
        audible=audible,
        position_ms=_require_position_ms(payload["position_ms"], deck_id),
        duration_ms=_require_duration_ms(payload.get("duration_ms"), deck_id),
        title=_optional_str(payload.get("title"), "title", deck_id),
        artist=_optional_str(payload.get("artist"), "artist", deck_id),
    )


def _optional_str(value: Any, field: str, deck: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeckObservationError(
            f"deck {deck}: {field} must be a string or null"
        )
    return value


def parse_snapshot(payload: Any) -> DeckSnapshot:
    """Validate one wire snapshot. Raises :class:`DeckObservationError`."""
    if not isinstance(payload, Mapping):
        raise DeckObservationError(
            f"snapshot must be an object, got {type(payload).__name__}"
        )
    if "observed_at" not in payload:
        raise DeckObservationError("snapshot is missing observed_at")
    if "decks" not in payload:
        raise DeckObservationError("snapshot is missing decks")
    decks = payload["decks"]
    if not isinstance(decks, Mapping):
        raise DeckObservationError(
            f"decks must be an object keyed by deck id, got "
            f"{type(decks).__name__}"
        )
    observed_at = _parse_utc(payload["observed_at"])
    parsed = tuple(
        _parse_deck(deck_id, state) for deck_id, state in sorted(decks.items())
    )
    return DeckSnapshot(observed_at=observed_at, decks=parsed)


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------



def played_fraction(audible_s: float, duration_ms: float | None) -> float | None:
    """Audible time as a fraction of track length, or None if length is unknown.

    Not clamped to 1.0: looping, pitch-down and a track left running past
    its nominal end legitimately exceed it, and the measured rekordbox
    history puts 505 of 3678 real gaps at or above 1.0. Clamping would
    erase a real and common signal.
    """
    if duration_ms is None or duration_ms <= 0:
        return None
    return round(audible_s / (duration_ms / 1000.0), 4)


def iso_utc(at: datetime) -> str:
    """UTC ISO 8601 with a real offset. The zone prints itself; never typed."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds")



__all__ = [
    "KNOWN_DECK_IDS",
    "DeckObservation",
    "DeckObservationError",
    "DeckSnapshot",
    "iso_utc",
    "parse_snapshot",
    "played_fraction",
]
