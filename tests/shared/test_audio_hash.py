"""Per-format audio identity regression tests.

The tag blocks are written by mutagen in CI, not Mixed In Key. A Mac capture
with MIK remains the open acceptance item documented in issue #3864.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.shared.hashing import sha256_audio_payload, sha256_file

pytestmark = pytest.mark.requires_mutagen


def _tag(path: Path, value: str) -> None:
    from mutagen import File
    from mutagen.id3 import TIT2

    audio = File(path)
    assert audio is not None
    if audio.tags is None:
        audio.add_tags()
    if path.suffix.lower() in {".mp3", ".aiff", ".aif", ".aifc", ".wav"}:
        audio.tags.add(TIT2(encoding=3, text=[value]))
    else:
        audio.tags["title"] = value
    audio.save()


@pytest.mark.parametrize("extension", ("mp3", "aiff", "flac", "m4a", "wav"))
def test_library_written_retags_preserve_audio_identity(tmp_path: Path, extension: str) -> None:
    """if mutagen rewrites tags in each supported format then payload identity is stable"""
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
