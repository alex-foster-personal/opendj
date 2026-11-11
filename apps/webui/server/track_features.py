"""Project web UI backend Track records into DJ Copilot TrackFeature shape.

Shared by Copilot and PLAY IT routes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from apps.shared.harmonic import TrackFeature, key_to_camelot

from .backend import Track

__all__ = ["camelot_or_none", "energy_of", "track_to_feature"]


def _last_played_or_none(raw: str | None) -> datetime | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def camelot_or_none(key: str | None) -> str | None:
    """Normalize any key notation to Camelot; unparseable -> missing."""
    if key is None or not key.strip():
        return None
    try:
        return str(key_to_camelot(key))
    except ValueError:
        return None


def energy_of(track: Track) -> int | None:
    """Energy (MIK 1-10 scale) from the provenance envelope."""
    prov = (track.provenance or {}).get("energy")
    if prov is None or prov.value is None:
        return None
    return int(prov.value)


def track_to_feature(track: Track) -> TrackFeature:
    return TrackFeature(
        stable_id=track.stable_id,
        artist=track.artist,
        bpm=track.bpm,
        key_camelot=camelot_or_none(track.key),
        energy=energy_of(track),
        title=track.title,
        last_played_at=_last_played_or_none(track.last_played_at),
    )
