"""Join rolling ``audio_*.wav`` segments before per-track split (issue #10378)."""

from __future__ import annotations

import os
import wave
from datetime import datetime
from pathlib import Path

import pytest

from apps.sets.join_recording import (
    RecordingMixedFormat,
    RecordingNotJoined,
    RecordingNotPcm16,
    ensure_joined_recording,
)
from apps.sets.paths import joined_recording_path
from apps.sets.split_tracks import split_recording
from tests.sets.test_soundcloud_export import _seed_session
from tests.sets.test_split_tracks import THREE_PLAY_TIMELINE, _segment, _write_wav

SESSION_START = "2026-04-17T21:30:00+00:00"


def _audio_name(offset_s: int) -> str:
    base = "2026-04-17T21-30-00"
    parts = base.split("T")
    time_part = parts[1]
    h, m, s = (int(x) for x in time_part.split("-"))
    s += offset_s
    while s >= 60:
        s -= 60
        m += 1
    while m >= 60:
        m -= 60
        h += 1
    stamp = f"{parts[0]}T{h:02d}-{m:02d}-{s:02d}"
    return f"audio_{stamp}.wav"


def _seed_segments_only(
    sets_root: Path,
    names_and_audio: list[tuple[str, list[bytes]]],
    *,
    rate: int = 8000,
) -> Path:
    sess = _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    for name, chunks in names_and_audio:
        _write_wav(sess / name, chunks, rate=rate)
    return sess


@pytest.mark.requirement("SET-12012")
def test_split_from_segments_only_builds_joined_and_cuts(sets_root: Path) -> None:
    """[if] only rolling WAV segments exist [then] split joins and cuts three tracks, [else stop]."""
    names = [
        (_audio_name(0), [_segment("a", 3.0, 8000)]),
        (_audio_name(3), [_segment("b", 3.0, 8000)]),
        (_audio_name(6), [_segment("c", 3.0, 8000)]),
    ]
    sess = _seed_segments_only(sets_root, names)
    joined = joined_recording_path(sess, "s1")
    assert not joined.is_file()
    files = split_recording("s1", sets_root=sets_root)
    assert len(files) == 3
    assert joined.is_file()
    with wave.open(str(joined), "rb") as wf:
        assert wf.getnframes() == 8000 * 9
    with wave.open(str(joined), "rb") as src:
        full = src.readframes(src.getnframes())
    for idx, tf in enumerate(files):
        with wave.open(str(sess / tf.name), "rb") as part:
            assert part.readframes(part.getnframes()) == full[idx * 8000 * 3 * 4 : (idx + 1) * 8000 * 3 * 4]


def test_gap_over_one_second_gets_silence(sets_root: Path) -> None:
    """[if] the next segment starts more than 1 s after prior content [then] silence fills the gap, [else stop]."""
    sess = _seed_segments_only(
        sets_root,
        [
            (_audio_name(0), [_segment("a", 3.0, 8000)]),
            (_audio_name(8), [_segment("b", 3.0, 8000)]),
        ],
    )
    joined = ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))
    with wave.open(str(joined), "rb") as wf:
        assert wf.getnframes() == 8000 * 11


def test_sub_second_gap_no_padding(sets_root: Path) -> None:
    """[if] the next segment is within 1 s on the timeline [then] output length is sum of segments, [else stop]."""
    sess = _seed_segments_only(
        sets_root,
        [
            (_audio_name(0), [_segment("a", 3.0, 8000)]),
            (_audio_name(3), [_segment("b", 3.0, 8000)]),
        ],
    )
    joined = ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))
    with wave.open(str(joined), "rb") as wf:
        assert wf.getnframes() == 8000 * 6


def test_mixed_sample_rate_refused(sets_root: Path) -> None:
    """[if] segments disagree on rate [then] RECORDING_MIXED_FORMAT is raised, [else stop]."""
    sess = _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    _write_wav(sess / _audio_name(0), [_segment("a", 1.0, 8000)], rate=8000)
    _write_wav(sess / _audio_name(3), [_segment("b", 1.0, 8000)], rate=16000)
    with pytest.raises(RecordingMixedFormat) as exc:
        ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))
    assert exc.value.code == "RECORDING_MIXED_FORMAT"
    assert not joined_recording_path(sess, "s1").with_name("s1.wav.part").exists()


def test_mp3_only_refused(sets_root: Path) -> None:
    """[if] only MP3 segments exist [then] RECORDING_NOT_JOINED is raised, [else stop]."""
    sess = _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    (sess / "audio_2026-04-17T21-30-00.mp3").write_bytes(b"fake")
    with pytest.raises(RecordingNotJoined, match="MP3"):
        ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))


def test_stale_joined_rebuilt_when_newer_segment(sets_root: Path, tmp_path: Path) -> None:
    """[if] a newer segment lands after a joined file [then] split rebuilds the join, [else stop]."""
    sess = _seed_segments_only(
        sets_root,
        [(_audio_name(0), [_segment("a", 3.0, 8000)])],
    )
    joined = joined_recording_path(sess, "s1")
    _write_wav(joined, [_segment("a", 2.0, 8000)])
    old_bytes = joined.read_bytes()
    seg2 = sess / _audio_name(3)
    _write_wav(seg2, [_segment("b", 3.0, 8000)])
    os.utime(seg2, (2_000_000_000.0, 2_000_000_000.0))
    ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))
    assert joined.read_bytes() != old_bytes
    with wave.open(str(joined), "rb") as wf:
        assert wf.getnframes() == 8000 * 6


def test_fresh_joined_left_alone(sets_root: Path) -> None:
    """[if] joined mtime is newer than all segments [then] bytes are unchanged, [else stop]."""
    sess = _seed_segments_only(
        sets_root,
        [(_audio_name(0), [_segment("a", 1.0, 8000)])],
    )
    joined = joined_recording_path(sess, "s1")
    _write_wav(joined, [_segment("c", 5.0, 8000)])
    os.utime(joined, (3_000_000_000.0, 3_000_000_000.0))
    before = joined.read_bytes()
    ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))
    assert joined.read_bytes() == before


def test_non_pcm_segment_refused(sets_root: Path) -> None:
    """[if] a WAV segment is not PCM16 [then] RECORDING_NOT_PCM16 is raised, [else stop]."""
    sess = _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    bad = sess / _audio_name(0)
    with wave.open(str(bad), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(8000)
        wf.writeframes(b"\x80" * 100)
    with pytest.raises(RecordingNotPcm16):
        ensure_joined_recording(sess, "s1", datetime.fromisoformat(SESSION_START))
    assert not joined_recording_path(sess, "s1").with_name("s1.wav.part").exists()
