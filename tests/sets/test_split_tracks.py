"""Per-track WAV split from joined session recording (issue #10378)."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from apps.sets.manifest import read_manifest
from apps.sets.paths import joined_recording_path, sanitize_filename_part
from apps.sets.soundcloud_export import SoundcloudTracklistRow
from apps.sets.split_tracks import (
    NoTracklist,
    RecordingNotJoined,
    RecordingNotPcm16,
    plan_cuts,
    split_recording,
    track_file_name,
)
from tests.sets.test_soundcloud_export import _play, _seed_session

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "apps" / "sets" / "schemas"


def _write_wav(
    path: Path,
    segments: list[bytes],
    *,
    rate: int = 8000,
    channels: int = 2,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        for chunk in segments:
            wf.writeframes(chunk)


def _segment(kind: str, seconds: float, rate: int, channels: int = 2) -> bytes:
    n_frames = int(seconds * rate)
    if kind == "silence":
        return b"\x00\x00" * (n_frames * channels)
    freq = {"a": 440.0, "b": 554.37, "c": 659.25}[kind]
    out = bytearray()
    for i in range(n_frames):
        sample = int(16000 * math.sin(2 * math.pi * freq * i / rate))
        packed = struct.pack("<h", sample)
        for _ in range(channels):
            out.extend(packed)
    return bytes(out)


THREE_PLAY_TIMELINE = [
    _play("s1", 0.0, title="Title A", artist="Artist A", track_stable_id="id-a"),
    _play("s1", 3.0, title="Title B", artist="Artist B", track_stable_id="id-b"),
    _play("s1", 6.0, title="Title C", artist="Artist C", track_stable_id="id-c"),
]


def _seed_with_joined(sets_root: Path, timeline: list[dict[str, Any]] | None = None) -> Path:
    sess = _seed_session(sets_root, timeline=timeline or THREE_PLAY_TIMELINE)
    joined = joined_recording_path(sess, "s1")
    _write_wav(
        joined,
        [_segment("a", 3.0, 8000), _segment("b", 3.0, 8000), _segment("c", 3.0, 8000)],
    )
    return sess


@pytest.mark.requirement("SET-12012")
def test_nine_second_wav_splits_three_equal_files(sets_root: Path) -> None:
    """[if] a 9 s joined WAV has plays at 0/3/6 s [then] three 3 s files match source frames, [else stop]."""
    sess = _seed_with_joined(sets_root)
    files = split_recording("s1", sets_root=sets_root)
    assert len(files) == 3
    assert [f.name for f in files] == [
        "01 Artist A - Title A.wav",
        "02 Artist B - Title B.wav",
        "03 Artist C - Title C.wav",
    ]
    with wave.open(str(joined_recording_path(sess, "s1")), "rb") as src:
        rate = src.getframerate()
        full = src.readframes(src.getnframes())
    for idx, tf in enumerate(files):
        path = sess / tf.name
        with wave.open(str(path), "rb") as part:
            assert part.getframerate() == rate
            assert part.getnchannels() == 2
            assert part.getsampwidth() == 2
            assert part.getnframes() == rate * 3
            expected = full[idx * rate * 3 * 4 : (idx + 1) * rate * 3 * 4]
            assert part.readframes(part.getnframes()) == expected


@pytest.mark.requirement("SET-12012")
def test_sanitize_filename_part_strips_forbidden_chars() -> None:
    """[if] artist/title carry forbidden chars [then] names collapse to safe text, [else stop]."""
    assert sanitize_filename_part('A<>:"/\\|?*  B') == "A B"
    assert sanitize_filename_part("x\t\ty  ") == "x y"


@pytest.mark.requirement("SET-12012")
def test_sanitize_via_split(sets_root: Path) -> None:
    """[if] split names tracks [then] forbidden chars are stripped from output filenames, [else stop]."""
    timeline = [
        _play(
            "s1",
            0.0,
            artist='A<>:"/\\|?*  B',
            title="x\t\ty  ",
            track_stable_id="x1",
        ),
        _play("s1", 3.0, title="Tail", artist="Z", track_stable_id="x2"),
        _play("s1", 6.0, title="End", artist="Z2", track_stable_id="x3"),
    ]
    _seed_with_joined(sets_root, timeline)
    files = split_recording("s1", sets_root=sets_root)
    assert files[0].name == "01 A B - x y.wav"


@pytest.mark.requirement("SET-12012")
def test_duplicate_artist_title_get_distinct_numbers(sets_root: Path) -> None:
    """[if] two tracks share display names [then] NN prefix keeps files distinct, [else stop]."""
    timeline = [
        _play("s1", 0.0, artist="Same", title="Tune", track_stable_id="u1"),
        _play("s1", 3.0, artist="Same", title="Tune", track_stable_id="u2"),
        _play("s1", 6.0, artist="Same", title="Tune", track_stable_id="u3"),
    ]
    sess = _seed_with_joined(sets_root, timeline)
    split_recording("s1", sets_root=sets_root)
    names = sorted(p.name for p in sess.glob("*.wav") if p.name[0].isdigit())
    assert "02 Same - Tune.wav" in names
    assert len([n for n in names if n.endswith("Same - Tune.wav")]) == 3


@pytest.mark.requirement("SET-12012")
def test_hidden_track_uses_id_placeholder(sets_root: Path) -> None:
    """[if] a stable id is marked hidden [then] the file is NN ID - ID.wav, [else stop]."""
    _seed_with_joined(sets_root)
    files = split_recording("s1", sets_root=sets_root, hidden_stable_ids=frozenset({"id-b"}))
    assert any(f.name == "02 ID - ID.wav" for f in files)


@pytest.mark.requirement("SET-12012")
def test_plan_cuts_pure_rules() -> None:
    """[if] cut planning is pure [then] first play starts at 0 and dup timestamps keep last, [else stop]."""
    rate = 8000
    n_frames = rate * 10
    row = SoundcloudTracklistRow
    rows = [
        row(1.5, "0:01", "t", "a", "1", None, None, "a - t"),
        row(12.0, "0:12", "past", "a", "2", None, None, "a - past"),
        row(5.0, "0:05", "first", "a", "3", None, None, "a - first"),
        row(5.0, "0:05", "last", "a", "4", None, None, "a - last"),
    ]
    cuts = plan_cuts(rows, frame_rate=rate, n_frames=n_frames, hidden_stable_ids=frozenset())
    assert cuts[0].start_frame == 0
    assert not any(c.start_frame >= n_frames for c in cuts)
    dup = [c for c in cuts if c.start_frame == 5 * rate]
    assert len(dup) == 1 and "last" in track_file_name(dup[0].number, 2, "a", "last", hidden=False)
    many = [row(float(i), "0:00", f"t{i}", "a", str(i), None, None, "x") for i in range(100)]
    cuts100 = plan_cuts(many, frame_rate=rate, n_frames=rate * 200, hidden_stable_ids=frozenset())
    assert cuts100[-1].name.startswith("100 ")


@pytest.mark.requirement("SET-12012")
def test_refused_states(sets_root: Path) -> None:
    """[if] preconditions fail [then] split refuses with the right code, [else stop]."""
    _seed_session(sets_root, timeline=THREE_PLAY_TIMELINE)
    with pytest.raises(RecordingNotJoined) as exc:
        split_recording("s1", sets_root=sets_root)
    assert exc.value.code == "RECORDING_NOT_JOINED"

    sess = _seed_with_joined(sets_root, timeline=[])
    (sess / "timeline.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(NoTracklist):
        split_recording("s1", sets_root=sets_root)

    bad = joined_recording_path(sess, "s1")
    bad.unlink()
    with wave.open(str(bad), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(8000)
        wf.writeframes(b"\x80" * 100)
    with pytest.raises(RecordingNotPcm16):
        split_recording("s1", sets_root=sets_root)


@pytest.mark.requirement("SET-12012")
def test_run_twice_replaces_files(sets_root: Path) -> None:
    """[if] split runs twice [then] stale track files are removed, [else stop]."""
    _seed_with_joined(sets_root)
    split_recording("s1", sets_root=sets_root)
    split_recording("s1", sets_root=sets_root)
    sess = sets_root / "s1"
    wavs = list(sess.glob("[0-9][0-9] *.wav"))
    assert len(wavs) == 3
    manifest = read_manifest(sess)
    assert len(manifest.track_files) == 3

    timeline_two = THREE_PLAY_TIMELINE[:2]
    (sess / "timeline.jsonl").write_text(
        "\n".join(json.dumps(row) for row in timeline_two) + "\n",
        encoding="utf-8",
    )
    joined = joined_recording_path(sess, "s1")
    _write_wav(joined, [_segment("a", 3.0, 8000), _segment("b", 3.0, 8000)])
    split_recording("s1", sets_root=sets_root)
    assert not any(p.name.startswith("03 ") for p in sess.glob("*.wav"))


@pytest.mark.requirement("SET-12012")
def test_manifest_validates_with_track_files(sets_root: Path) -> None:
    """[if] split finishes [then] manifest track_files validates against schema, [else stop]."""
    sess = _seed_with_joined(sets_root)
    split_recording("s1", sets_root=sets_root)
    manifest = read_manifest(sess)
    from apps.sets.manifest import TrackFile

    assert isinstance(manifest.track_files[0], TrackFile)
    tf = manifest.track_files[0]
    assert tf.size_bytes == (sess / tf.name).stat().st_size
    schema = json.loads((SCHEMA_DIR / "session_manifest.schema.json").read_text(encoding="utf-8"))
    payload = json.loads((sess / "manifest.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)
    payload.pop("track_files", None)
    Draft202012Validator(schema).validate(payload)


@pytest.mark.requirement("SET-12012")
def test_out_dir_writes_outside_session(sets_root: Path, tmp_path: Path) -> None:
    """[if] out_dir is set [then] files land there with absolute manifest paths, [else stop]."""
    _seed_with_joined(sets_root)
    out = tmp_path / "out"
    files = split_recording("s1", sets_root=sets_root, out_dir=out)
    assert not list((sets_root / "s1").glob("[0-9][0-9] *.wav"))
    for tf in files:
        resolved = Path(tf.path)
        assert resolved.is_absolute()
        assert resolved.parent == out.resolve()
        assert resolved.is_file()
