"""Per-format audio identity regression tests.

The tag blocks are written with ffmpeg ``-c copy -metadata``, not Mixed In
Key. A Mac capture with MIK remains the open acceptance item in issue #3864.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.shared.hashing import sha256_audio_payload, sha256_file

def _tag(src: Path, dst: Path, value: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-c", "copy",
            "-metadata", f"title={value}",
            str(dst),
        ],
        check=True,
    )


@pytest.mark.parametrize("extension", ("mp3", "aiff", "flac", "m4a", "wav"))
def test_library_written_retags_preserve_audio_identity(tmp_path: Path, extension: str) -> None:
    """if ffmpeg rewrites the title in each supported format then payload identity is stable"""
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
    before = tmp_path / f"before.{extension}"
    after = tmp_path / f"after.{extension}"
    _tag(original, before, "before")
    _tag(original, after, "after")
    original = before
    tagged = after
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
        tagged = path.with_name(path.stem + "-tagged.wav")
        _tag(path, tagged, "same tags")
        path.write_bytes(tagged.read_bytes())
    assert sha256_audio_payload(first) != sha256_audio_payload(second)
