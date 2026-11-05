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
    [if] a WAVEFORMATEXTENSIBLE wav GUID is PCM or IEEE-float [then] it
         imports, [else stop].
    [if] that GUID is any other 16 bytes [then] probe rejects, [else stop].
    [if] wav tag is not 1, 3, or 0xFFFE resolving to those [then] reject
         naming the tag, [else stop].
    [if] AIFC COMM FourCC is outside the accepted set [then] reject naming
         it, [else stop].
    [if] COMM Pascal name overruns the chunk [then] reject, [else stop].
    [if] a `fact` payload is present and shorter than 4 bytes or zero
         [then] reject, [else stop].
    [if] a chunk header or payload crosses EOF or the container end
         [then] reject, [else stop].
    [if] container magic does not match the extension [then ⛔️] reject
    [if] the tag reader (tinytag) claims absurd duration or bitrate [then ⛔️]
         reject
    [if] the tag reader cannot parse a file whose magic checks passed
         [then] it is NOT rejected: the decoder, not the tag reader, is the
         authority on playability, and a library must never lose a track to a
         metadata parser's blind spot

  ✔︎ ✅ 🎯 a decode far shorter than the duration the file claims is a
    truncated file, not a short track (issue #6150 B3).
    [if] a lane decoded under MIN_DECODED_FRACTION of the claimed duration
         [then ⛔️] ``check_decoded_duration`` raises with both durations, so
         the key and stems lanes fail per track instead of analyzing the few
         frames that survived the cut
    [if] the tag reader cannot parse the file or reports no duration
         [then] no verdict: the decode stands, as at ingest
  -> full decode, ffmpeg, librosa, or soundfile. Header + one frame only.
  -> rekordbox adapter ingest (follow-up).
"""

from __future__ import annotations

from pathlib import Path

from apps.shared import _tagreader, paths
from apps.shared._tagreader import HAS_TAG_READER
from apps.shared.audio_aiff_probe import probe_aiff
from apps.shared.audio_chunk_io import UnplayableAudioError
from apps.shared.audio_wav_probe import probe_wav

__all__ = ["MIN_DECODED_FRACTION", "UnplayableAudioError", "check_decoded_duration", "probe_playable_audio"]

# Effective bitrate bounds for duration/size cross-checks (kbps).
_MAX_EFFECTIVE_KBPS = 3200
_MIN_BYTES_PER_SECOND = 1000  # 8 kbps floor
#: A decode shorter than this fraction of the duration the container claims
#: is a truncated file. Wide on purpose: a good file decodes to within a frame
#: of its claim, while a VBR mp3 with no Xing header only has a claim
#: tinytag ESTIMATES from its first 30 s, so a strict ratio would fail good
#: files. The acceptance probe's 4096-byte cut of a 2.04 s mp3 decodes 0.26 s.
MIN_DECODED_FRACTION = 0.5


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
        return
    if ext in {".aiff", ".aif"}:
        _probe_aiff(path, size_bytes)
        return

    # The tag reader cross-checks duration only for formats it can parse.
    # tinytag has no raw ADTS reader, so a raw .aac gets the stdlib frame walk
    # instead: a stream with no whole frame is not playable audio. An MP4
    # container named .aac (ftyp) still goes to the tag reader.
    if ext == ".aac":
        _probe_aac(path, header, size_bytes)
    elif HAS_TAG_READER and _tagreader.can_read(path):
        _probe_tags(path, size_bytes)


def check_decoded_duration(path: Path, decoded_s: float) -> None:
    """Raise :class:`UnplayableAudioError` when a decode is far shorter than the file claims.

    Decoders stop quietly at the cut of a truncated file and return the frames
    before it: a 4096-byte cut of a 2 s A440 mp3 decodes 0.26 s of clean
    A440, which a key estimator reads as A major. So a lane that decodes
    must compare what it got with what the container says it holds. A tag
    reader that cannot parse the file, or gives no duration, gives no verdict.
    """
    if decoded_s <= 0:
        raise UnplayableAudioError("decoded no audio")
    if not HAS_TAG_READER or not _tagreader.can_read(path):
        return
    if _is_streamed_wav(path):
        return
    try:
        claimed = _tagreader.read(path).duration
    except _tagreader.TagReadError:
        return
    if claimed is None or claimed <= 0:
        return
    if decoded_s < float(claimed) * MIN_DECODED_FRACTION:
        raise UnplayableAudioError(
            f"truncated or corrupt audio: decoded {decoded_s:.2f} s of the {float(claimed):.2f} s the file claims"
        )


def _is_streamed_wav(path: Path) -> bool:
    """Return whether a WAV carries the streaming size sentinel, so it claims no real length.

    A WAV written to a pipe (``ffmpeg ... -f wav pipe:1``) cannot seek back to
    patch its sizes, so its RIFF size is 0xFFFFFFFF or 0 and a tag reader turns
    that into a claim of hours. Such a file is read to EOF and its decode is
    the truth, so the length check gives no verdict.
    """
    if path.suffix.lower() not in {".wav", ".wave"}:
        return False
    header = _read_header(path, 8)
    return header[:4] == b"RIFF" and header[4:8] in {b"\xff\xff\xff\xff", b"\x00\x00\x00\x00"}


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
        if not _aac_magic_ok(header):
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


def _aac_magic_ok(header: bytes) -> bool:
    # A raw ADTS stream may open with an ID3v2 tag; the frame walk in
    # _probe_adts skips it and refuses the file if no ADTS follows.
    if _adts_sync_ok(header) or header.startswith(b"ID3"):
        return True
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _adts_sync_ok(header: bytes) -> bool:
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF0) == 0xF0


# ----- wav / aiff -----------------------------------------------------------
def _probe_wav(path: Path, size_bytes: int) -> None:
    rate, _channels, frames = probe_wav(path)
    if _is_streamed_wav(path):
        return
    duration_s = frames / rate if rate else 0.0
    _check_duration_size(duration_s, size_bytes)


def _probe_aiff(path: Path, size_bytes: int) -> None:
    rate, _channels, frames = probe_aiff(path)
    duration_s = frames / rate if rate else 0.0
    _check_duration_size(duration_s, size_bytes)


def _check_duration_size(duration_s: float, size_bytes: int) -> None:
    if duration_s <= 0:
        raise UnplayableAudioError("missing or zero duration")
    if size_bytes < duration_s * _MIN_BYTES_PER_SECOND:
        raise UnplayableAudioError("file too small for claimed duration")
    if duration_s >= 0.05:
        effective_kbps = (size_bytes * 8) / duration_s / 1000
        if effective_kbps > _MAX_EFFECTIVE_KBPS:
            raise UnplayableAudioError(f"absurd effective bitrate {effective_kbps:.0f} kbps")


# ----- tag-reader cross-check -----------------------------------------------
def _probe_aac(path: Path, header: bytes, size_bytes: int) -> None:
    if header[4:8] != b"ftyp":
        _probe_adts(path, size_bytes)
    elif HAS_TAG_READER:
        _probe_tags(path, size_bytes)


def _probe_adts(path: Path, size_bytes: int) -> None:
    length = _tagreader.adts_duration(path)
    if not length:
        raise UnplayableAudioError("no complete aac/adts frames")
    _check_duration_size(length, size_bytes)


def _probe_tags(path: Path, size_bytes: int) -> None:
    try:
        tag = _tagreader.read(path)
    except _tagreader.TagReadError as exc:
        raise UnplayableAudioError(f"tag reader parse failed: {exc}") from exc

    length = tag.duration
    if length is None or length <= 0:
        raise UnplayableAudioError("missing or zero duration")

    _check_duration_size(float(length), size_bytes)
