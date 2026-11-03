"""Shared RESCUE-01 snapshot payloads for webui rescue tests."""

from __future__ import annotations

from typing import Any, Literal

RescueSnapshotReason = Literal["periodic", "transport", "load"]


def minimal_rescue_snapshot(
    captured_at_ms: int,
    *,
    marker: str | None = None,
    reason: RescueSnapshotReason = "transport",
    master_deck: int | None = None,
    playing: bool = False,
    position_ms: int = 0,
) -> dict[str, Any]:
    """Build a schema-1 gig snapshot that passes ``apps.rescue.ring`` validation."""
    deck = {
        "deck_id": 1,
        "stable_id": "abc",
        "source_path": None,
        "playing": playing,
        "position_ms": position_ms,
        "beat_stamp": {"kind": "sample", "position_ms": position_ms},
        "pitch": 1.0,
        "pitch_range": 8,
        "master_tempo_enabled": True,
        "key_sync_enabled": False,
        "quantize_enabled": True,
        "beat_sync_enabled": True,
        "sync_mode": "bar",
        "is_master": False,
        "cue_ms": None,
        "loop": None,
        "hot_cue_armed": None,
        "stems": {
            "vocal": {"muted": False, "solo": False, "gain": 1.0},
            "instrumental": {"muted": False, "solo": False, "gain": 1.0},
            "drums": {"muted": False, "solo": False, "gain": 1.0},
        },
        "mixer_channel": {
            "trim": 0.5,
            "eq_high": 0.5,
            "eq_mid": 0.5,
            "eq_low": 0.5,
            "filter": 0.5,
            "fader": 1.0,
            "assign": "THRU",
            "cue_enabled": False,
        },
    }
    decks = {
        str(i): {
            **deck,
            "deck_id": i,
            "is_master": master_deck is not None and i == master_deck,
        }
        for i in range(1, 5)
    }
    payload: dict[str, Any] = {
        "schema": 1,
        "captured_at_ms": captured_at_ms,
        "reason": reason,
        "app_posture": "gig",
        "master_deck": master_deck,
        "playlist_id": None,
        "decks": decks,
        "mixer": {
            "crossfader": 0.5,
            "master": 1.0,
            "headphones": {
                "mix": 0.5,
                "level": 0.5,
                "output_mode": "practice",
                "selected_master_output_device_id": None,
                "selected_output_device_id": None,
            },
        },
    }
    if marker is not None:
        payload["marker"] = marker
    return payload
