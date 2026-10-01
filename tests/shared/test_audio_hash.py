"""Per-format audio identity regression tests.

The tag blocks are written in CI by the in-house ID3v2 / FLAC writers (MP3,
FLAC) and by an ffmpeg stream-copy remux (AIFF, M4A, WAV), not Mixed In Key. A
Mac capture with MIK remains the open acceptance item documented in issue
#3864.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.shared import flac_meta, id3v2
from apps.shared.hashing import sha256_audio_payload, sha256_file


def _tag(path: Path, value: str) -> None:
    """Rewrite ``path``'s title tag in place, leaving the audio payload alone."""
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        tag = id3v2.load_or_new(path)
        tag.set_text("TIT2", value)
        id3v2.save(path, tag)
        return
    if suffix == ".flac":
        meta = flac_meta.read(path)
        meta.set("TITLE", value)
        flac_meta.save(path, meta)
        return
    remuxed = path.with_name(f"retag-{path.name}")
    id3_args = ["-write_id3v2", "1"] if suffix in {".aiff", ".aif"} else []
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-c", "copy",
            "-map_metadata", "-1", "-metadata", f"title={value}", *id3_args, str(remuxed),
        ],
        check=True,
    )
    remuxed.replace(path)


@pytest.mark.parametrize("extension", ("mp3", "aiff", "flac", "m4a", "wav"))
def test_library_written_retags_preserve_audio_identity(tmp_path: Path, extension: str) -> None:
    """if a tag rewrite in each supported format happens then payload identity is stable"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to create the per-format payload")
    original = tmp_path / f"original.{extension}"
    tagged = tmp_path / f"tagged.{extension}"
    source = tmp_path / "source.wav"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.1", str(source),
        ],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(source), str(original)],
        check=True,
    )
    tagged.write_bytes(original.read_bytes())
    _tag(original, "before")
    _tag(tagged, "after")
    assert sha256_file(original) != sha256_file(tagged)
    assert sha256_audio_payload(original) == sha256_audio_payload(tagged)


def test_different_recordings_with_same_tags_do_not_share_audio_identity(tmp_path: Path) -> None:
    """if two WAV payloads differ but tags match then audio identity differs"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to create the payloads")
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    for frequency, path in ((440, first), (880, second)):
        subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                f"sine=frequency={frequency}:duration=0.1", str(path),
            ],
            check=True,
        )
        _tag(path, "same tags")
    assert sha256_audio_payload(first) != sha256_audio_payload(second)
