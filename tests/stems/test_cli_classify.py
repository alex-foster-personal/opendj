"""Pure helpers for stem CLI classification (no torch)."""

from __future__ import annotations

from pathlib import Path

from apps.stems.cli import (
    CATEGORY_CACHED,
    CATEGORY_MISSING,
    CATEGORY_TODO,
    classify_stems,
)
from apps.vocals.cli import VocalTrack


def _tr(stable_id: str, *, on_disk: bool) -> VocalTrack:
    return VocalTrack(
        stable_id=stable_id,
        vendor_id="x",
        title="t",
        length_s=10,
        folder_path=None,
        analysis_data_path=None,
        audio_path=Path("/tmp/nope.wav") if on_disk else None,
        audio_on_disk=on_disk,
    )


def test_classify_missing_and_todo(tmp_path: Path) -> None:
    tracks = [_tr("a", on_disk=False), _tr("b", on_disk=True)]
    classify_stems(tracks, tmp_path)
    assert tracks[0].category == CATEGORY_MISSING
    assert tracks[1].category == CATEGORY_TODO


def test_classify_cached_when_bundle_valid(tmp_path: Path) -> None:
    import json
    import wave

    sid = "track-001"
    bundle = tmp_path / sid
    bundle.mkdir()
    for part in ("vocals", "drums", "bass", "other"):
        with wave.open(str(bundle / f"{part}.wav"), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(44100)
            out.writeframes(b"\x00\x00" * 8)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stable_id": sid,
                "model": {"name": "htdemucs", "version": "4.0.1"},
                "source": {"path": "/x.wav", "sha256": "a" * 64},
                "files": {
                    "vocals": "vocals.wav",
                    "drums": "drums.wav",
                    "bass": "bass.wav",
                    "other": "other.wav",
                },
            }
        ),
        encoding="utf-8",
    )
    tracks = [_tr(sid, on_disk=True)]
    classify_stems(tracks, tmp_path)
    assert tracks[0].category == CATEGORY_CACHED
