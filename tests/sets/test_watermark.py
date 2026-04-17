"""Tests for :mod:`apps.sets.watermark` -- stdlib ID3v2.4 COMM writer."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sets import watermark


@pytest.mark.requirement("SET-01")
def test_build_comm_frame_round_trips():
    frame = watermark.build_comm_frame("hello world")
    tag = watermark.build_id3v24_tag(frame)
    # Header sanity
    assert tag[:3] == b"ID3"
    assert tag[3] == 4  # version major
    # Body contains our text
    assert b"hello world" in tag


@pytest.mark.requirement("SET-01")
def test_synchsafe_boundaries_round_trip():
    for size in (0, 1, 127, 128, 2097152, watermark._SYNC_SAFE_MAX):
        encoded = watermark._synchsafe(size)
        assert watermark._decode_synchsafe(encoded) == size


@pytest.mark.requirement("SET-01")
def test_stamp_prepends_tag_to_raw_mp3(tmp_path: Path):
    """A fresh MP3 with no existing ID3v2 gets one on stamp."""
    mp3 = tmp_path / "seg.mp3"
    # A plausible MPEG frame sync (0xFFFB) preserves the format feel.
    mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 200)
    watermark.stamp(mp3)
    frames = watermark.read_comm_frames(mp3)
    assert len(frames) == 1
    lang, desc, text = frames[0]
    assert lang == "eng"
    assert desc == "legal"
    assert "personal-review-only" in text


@pytest.mark.requirement("SET-01")
def test_stamp_appends_frame_when_existing_tag_present(tmp_path: Path):
    mp3 = tmp_path / "seg.mp3"
    mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 200)
    watermark.stamp(mp3, privacy_note="first note")
    watermark.stamp(mp3, privacy_note="second note")
    frames = watermark.read_comm_frames(mp3)
    texts = [t for _, _, t in frames]
    assert "first note" in texts
    assert "second note" in texts
    # Original MP3 frames still present after the tag.
    data = mp3.read_bytes()
    assert b"\xff\xfb\x90\x64" in data


@pytest.mark.requirement("SET-01")
def test_build_comm_frame_rejects_non_3letter_language():
    with pytest.raises(ValueError):
        watermark.build_comm_frame("x", language="englsh")


@pytest.mark.requirement("SET-01")
def test_mp3_carries_personal_review_only_id3_comm(tmp_path: Path):
    mp3 = tmp_path / "audio_2026-04-17T21-30-00.mp3"
    mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 512)
    watermark.stamp(mp3)
    frames = watermark.read_comm_frames(mp3)
    assert any("personal-review-only" in t for *_, t in frames)
