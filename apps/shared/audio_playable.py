"""Playable-audio probe for folder ingest rejection.

Folder import must not treat an allowlisted extension as proof of audio. This
module performs base-install-safe checks before a track row is written.

MINI-PRD
--------
Status key: `->` out of scope | `?` todo | `✔︎` done | `✔︎ ✅` done + ran + works
as expected | `✔︎ ✅ 🎯` done + working + regression tests.

  ✔︎ ✅ 🎯 reject empty, header-only, truncated, prose-in-.wav, riff-garbage,
    and corrupt-container payloads at folder ingest.
    [if] a zero-byte allowlisted file is imported [then ⛔️] reject before row
         insert with reason ``empty file``
    [if] a wav has no PCM frames or truncates on read [then ⛔️] reject
    [if] container magic does not match the extension [then ⛔️] reject
    [if] the tag reader (tinytag) claims absurd duration or bitrate [then ⛔️]
         reject

  -> full decode, ffmpeg, librosa, or soundfile. Header + one frame only.
  -> rekordbox adapter ingest (follow-up).
"""

from __future__ import annotations

import wave
from pathlib import Path

from apps.shared import _tagreader, paths
from apps.shared._tagreader import HAS_TAG_READER

__all__ = ["UnplayableAudioError", "probe_playable_audio"]

# Effective bitrate bounds for duration/size cross-checks (kbps).
_MAX_EFFECTIVE_KBPS = 3200
_MIN_BYTES_PER_SECOND = 1000  # 8 kbps floor


class UnplayableAudioError(Exception):
    """Raised when a path is not playable audio for library ingest."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def probe_playable_audio(path: Path) -> None:
    """Raise :class:`UnplayableAudioError` when ``path`` is not playable audio."""
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise UnplayableAudioError("unreadable file") from exc

    if size_bytes == 0:
        raise UnplayableAudioError("empty file")

    ext = path.suffix.lower()
    if ext not in paths.AUDIO_EXTENSIONS:
        raise UnplayableAudioError(f"unsupported extension {ext}")

    header = _read_header(path)
    _check_container_magic(ext, header)

    if ext in {".wav", ".wave"}:
        _probe_wav(path, size_bytes)
    elif ext in {".aiff", ".aif"}:
        _probe_aiff_header(header, size_bytes)

    if HAS_TAG_READER:
        _probe_tags(path, size_bytes)


# ----- header helpers -------------------------------------------------------
def _read_header(path: Path, count: int = 16) -> bytes:
    with path.open("rb") as handle:
        return handle.read(count)


def _check_container_magic(ext: str, header: bytes) -> None:
    if ext == ".flac":
        if not header.startswith(b"fLaC"):
            raise UnplayableAudioError("missing flac magic")
        return

    if ext == ".ogg":
        if not header.startswith(b"OggS"):
            raise UnplayableAudioError("missing ogg magic")
        return

    if ext in {".m4a", ".alac", ".mp4"}:
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise UnplayableAudioError("missing ftyp box")
        return

    if ext == ".aac":
        if not _adts_sync_ok(header) and not (
            len(header) >= 12 and header[4:8] == b"ftyp"
        ):
            raise UnplayableAudioError("missing aac/adts magic")
        return

    if ext == ".mp3":
        if not _mp3_magic_ok(header):
            raise UnplayableAudioError("missing mp3 magic")
        return

    if ext in {".wav", ".wave"}:
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise UnplayableAudioError("missing wav riff header")
        return

    if ext in {".aiff", ".aif"}:
        if len(header) < 12 or header[:4] != b"FORM":
            raise UnplayableAudioError("missing aiff form header")
        return


def _mp3_magic_ok(header: bytes) -> bool:
    if header.startswith(b"ID3"):
        return True
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        layer = (header[1] >> 1) & 0x03
        return layer != 0
    return False


def _adts_sync_ok(header: bytes) -> bool:
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF0) == 0xF0


# ----- wav / aiff -----------------------------------------------------------
def _probe_wav(path: Path, size_bytes: int) -> None:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            if frames <= 0:
                raise UnplayableAudioError("no pcm frames")
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            _check_pcm_params(rate, channels, width)
            min_bytes = frames * channels * width + 44
            if size_bytes < min_bytes:
                raise UnplayableAudioError("truncated wav")
            handle.readframes(1)
            duration_s = frames / rate if rate else 0.0
            _check_duration_size(duration_s, size_bytes)
    except UnplayableAudioError:
        raise
    except wave.Error as exc:
        raise UnplayableAudioError(f"invalid wav: {exc}") from exc
    except EOFError as exc:
        raise UnplayableAudioError("truncated wav") from exc


def _probe_aiff_header(header: bytes, size_bytes: int) -> None:
    if len(header) < 12:
        raise UnplayableAudioError("truncated aiff header")
    form_type = header[8:12]
    if form_type not in {b"AIFF", b"AIFC"}:
        raise UnplayableAudioError("missing aiff form type")
    if size_bytes < 128:
        raise UnplayableAudioError("aiff file too small")


def _check_pcm_params(rate: int, channels: int, width: int) -> None:
    if rate < 8000 or rate > 192_000:
        raise UnplayableAudioError("implausible sample rate")
    if channels < 1 or channels > 8:
        raise UnplayableAudioError("implausible channel count")
    if width not in {1, 2, 3, 4}:
        raise UnplayableAudioError("implausible sample width")


def _check_duration_size(duration_s: float, size_bytes: int) -> None:
    if duration_s <= 0:
        raise UnplayableAudioError("missing or zero duration")
    if size_bytes < duration_s * _MIN_BYTES_PER_SECOND:
        raise UnplayableAudioError("file too small for claimed duration")
    if duration_s >= 0.05:
        effective_kbps = (size_bytes * 8) / duration_s / 1000
        if effective_kbps > _MAX_EFFECTIVE_KBPS:
            raise UnplayableAudioError(
                f"absurd effective bitrate {effective_kbps:.0f} kbps"
            )


# ----- tag-reader cross-check -----------------------------------------------
def _probe_tags(path: Path, size_bytes: int) -> None:
    try:
        tag = _tagreader.read(path)
    except _tagreader.TagReadError as exc:
        raise UnplayableAudioError(f"tag reader parse failed: {exc}") from exc

    length = tag.duration
    if length is None or length <= 0:
        raise UnplayableAudioError("missing or zero duration")

    _check_duration_size(float(length), size_bytes)
