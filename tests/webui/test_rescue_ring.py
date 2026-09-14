"""RESCUE-01: atomic rescue snapshot ring writer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.rescue.ring import (
    MAX_BYTES,
    RING_SIZE,
    RescueSnapshotInvalidError,
    RescueSnapshotTooLargeError,
    append_snapshot,
    read_index,
    read_latest,
    write_atomic,
)

pytestmark = pytest.mark.requirement("RESCUE-01")


def _minimal_snapshot(captured_at_ms: int, marker: str = "v1") -> dict:
    deck = {
        "deck_id": 1,
        "stable_id": "abc",
        "source_path": None,
        "playing": False,
        "position_ms": 0,
        "beat_stamp": {"kind": "sample", "position_ms": 0},
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
    decks = {str(i): {**deck, "deck_id": i} for i in range(1, 5)}
    return {
        "schema": 1,
        "captured_at_ms": captured_at_ms,
        "reason": "transport",
        "app_posture": "gig",
        "master_deck": None,
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
        "marker": marker,
    }


def test_append_updates_index_and_newest_slot(tmp_path: Path) -> None:
    for index in range(RING_SIZE + 2):
        append_snapshot(tmp_path, _minimal_snapshot(1000 + index, marker=f"v{index}"))
    ring_index = read_index(tmp_path)
    assert ring_index.newest_slot == 1
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v9"
    assert ring_index.slots[ring_index.newest_slot].bytes > 0


def test_snapshot_rejected_over_32k(tmp_path: Path) -> None:
    payload = _minimal_snapshot(1)
    payload["padding"] = "x" * (MAX_BYTES + 1)
    with pytest.raises(RescueSnapshotTooLargeError):
        append_snapshot(tmp_path, payload)


def test_interrupted_write_keeps_prior_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rescue_dir = tmp_path / "state" / "rescue"
    rescue_dir.mkdir(parents=True)
    append_snapshot(tmp_path, _minimal_snapshot(1, marker="v1"))
    slot_path = rescue_dir / "ring-0.json"
    before = slot_path.read_text(encoding="utf-8")
    real_replace = os.replace

    def failing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(src).endswith(".tmp"):
            raise OSError("simulated kill before rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated kill"):
        write_atomic(rescue_dir / "ring-1.json", json.dumps(_minimal_snapshot(2)) + "\n")
    assert slot_path.read_text(encoding="utf-8") == before
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v1"


def test_latest_skips_missing_slot(tmp_path: Path) -> None:
    append_snapshot(tmp_path, _minimal_snapshot(1, marker="v1"))
    append_snapshot(tmp_path, _minimal_snapshot(2, marker="v2"))
    index = read_index(tmp_path)
    newest = index.newest_slot
    (tmp_path / "state" / "rescue" / f"ring-{newest}.json").unlink()
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v1"


def test_invalid_snapshot_schema_rejected(tmp_path: Path) -> None:
    with pytest.raises(RescueSnapshotInvalidError):
        append_snapshot(tmp_path, {"schema": 2, "captured_at_ms": 1})
