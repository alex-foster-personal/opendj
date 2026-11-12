"""Read NI-style ``.stem.mp4`` containers (master + four stems).

Mini-PRD
========

Public NI Stems layout (see (private path), "Two layouts, no fake controls"):
track 0 is a stereo master mix for plain players; tracks 1-4 are the four stems
in fixed container order ``drums``, ``bass``, ``other``, ``vocals`` (Mixxx and
the NI Stems ecosystem use this ordering).

This module probes and decodes only. It never writes containers, repairs streams,
or returns a bundle when probing or decode fails.

Acceptance (issue #12233)
-------------------------
* [if] a valid ffmpeg-built four-part fixture is parsed [then] parts are in
  documented container order [else stop].
* [if] metadata tags are present [then] track and stem metadata are returned
  [else stop].
* [if] fewer or more than four usable stem streams exist [then] refuse [else stop].
* [if] ffprobe or decode fails [then] refuse a usable bundle [else stop].
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Documented NI container order for stem streams 1-4 (index 0 is the master).
STEM_MP4_PART_IDS: tuple[str, str, str, str] = ("drums", "bass", "other", "vocals")

_FFPROBE_BINARY = "ffprobe"
_FFPROBE_OVERRIDE_ENV = "MDT_FFPROBE"
_PROBE_TIMEOUT_S = 30.0
_DECODE_TIMEOUT_S = 120.0


class StemMp4Error(ValueError):
    """A ``.stem.mp4`` container cannot be mapped to an ordered four-part set."""


@dataclass(frozen=True, slots=True)
class StemMp4StemDescriptor:
    name: str
    color: str | None


@dataclass(frozen=True, slots=True)
class StemMp4Metadata:
    """Container-level tags plus NI ``stem`` atom content when present."""

    title: str | None
    artist: str | None
    album: str | None
    stems: tuple[StemMp4StemDescriptor, ...]
    stem_atom_version: int | None
    raw_stem_json: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class StemMp4StreamProbe:
    part_id: str
    stream_index: int
    codec_name: str
    sample_rate: int
    channels: int
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class StemMp4Bundle:
    """Probe result: four ordered stem parts and parsed metadata."""

    path: Path
    master_stream_index: int | None
    parts: tuple[StemMp4StreamProbe, StemMp4StreamProbe, StemMp4StreamProbe, StemMp4StreamProbe]
    metadata: StemMp4Metadata


def resolve_ffprobe() -> str:
    override = os.environ.get(_FFPROBE_OVERRIDE_ENV)
    if override:
        resolved = os.path.abspath(override)
        if Path(resolved).is_file() and os.access(resolved, os.X_OK):
            return resolved
        raise StemMp4Error(f"{_FFPROBE_OVERRIDE_ENV}={override!r} is not an executable file")
    exe = shutil.which(_FFPROBE_BINARY)
    if exe is None:
        raise StemMp4Error("ffprobe is not on PATH (set MDT_FFPROBE to override)")
    return exe


def read_stem_mp4(path: Path) -> StemMp4Bundle:
    """Probe ``path`` and return four stem parts in container order or raise."""
    path = path.resolve()
    if not path.is_file():
        raise StemMp4Error(f"stem mp4 not found: {path}")
    streams = _ffprobe_audio_streams(path)
    master_index, stem_streams = _select_stem_streams(streams)
    if len(stem_streams) != 4:
        raise StemMp4Error(f"expected exactly four stem audio streams, got {len(stem_streams)}")
    parts: list[StemMp4StreamProbe] = []
    for part_id, stream in zip(STEM_MP4_PART_IDS, stem_streams, strict=True):
        parts.append(
            StemMp4StreamProbe(
                part_id=part_id,
                stream_index=int(stream["index"]),
                codec_name=str(stream.get("codec_name") or ""),
                sample_rate=int(stream.get("sample_rate") or 0),
                channels=int(stream.get("channels") or 0),
                duration_s=_parse_duration(stream),
            )
        )
    format_tags = _ffprobe_format_tags(path)
    stem_json = _read_stem_atom_json(path)
    metadata = _metadata_from_tags_and_stem(format_tags, stem_json)
    _validate_stem_names(metadata, parts)
    return StemMp4Bundle(
        path=path,
        master_stream_index=master_index,
        parts=(parts[0], parts[1], parts[2], parts[3]),
        metadata=metadata,
    )


def decode_stem_mp4_part(path: Path, part_id: str) -> bytes:
    """Return little-endian stereo 16-bit PCM WAV bytes for one stem part."""
    bundle = read_stem_mp4(path)
    match = next((p for p in bundle.parts if p.part_id == part_id), None)
    if match is None:
        raise StemMp4Error(f"unknown stem part {part_id!r}")
    return _ffmpeg_decode_to_wav(path, match.stream_index)


# ----- ffprobe ---------------------------------------------------------------


def _ffprobe_audio_streams(path: Path) -> list[dict[str, Any]]:
    ffprobe = resolve_ffprobe()
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,sample_rate,channels,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise StemMp4Error(f"ffprobe timed out after {_PROBE_TIMEOUT_S}s") from exc
    if completed.returncode != 0:
        tail = completed.stderr.strip().splitlines()[-1:] or ["ffprobe failed"]
        raise StemMp4Error(tail[0])
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StemMp4Error("ffprobe returned invalid JSON") from exc
    streams = payload.get("streams") or []
    return [s for s in streams if s.get("codec_type") == "audio"]


def _ffprobe_format_tags(path: Path) -> dict[str, str]:
    ffprobe = resolve_ffprobe()
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags=title,artist,album",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise StemMp4Error(f"ffprobe timed out after {_PROBE_TIMEOUT_S}s") from exc
    if completed.returncode != 0:
        raise StemMp4Error("ffprobe could not read format tags")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StemMp4Error("ffprobe returned invalid JSON for format tags") from exc
    tags = payload.get("format", {}).get("tags") or {}
    return {str(k): str(v) for k, v in tags.items()}


def _select_stem_streams(
    audio_streams: list[dict[str, Any]],
) -> tuple[int | None, list[dict[str, Any]]]:
    if not audio_streams:
        raise StemMp4Error("container has no audio streams")
    if len(audio_streams) == 5:
        return int(audio_streams[0]["index"]), audio_streams[1:5]
    if len(audio_streams) == 4:
        return None, audio_streams
    raise StemMp4Error(
        f"expected four stem streams (optionally plus one master); got {len(audio_streams)} audio streams"
    )


def _parse_duration(stream: dict[str, Any]) -> float | None:
    raw = stream.get("duration")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ----- metadata --------------------------------------------------------------


def _metadata_from_tags_and_stem(
    tags: dict[str, str],
    stem_json: dict[str, Any] | None,
) -> StemMp4Metadata:
    descriptors: list[StemMp4StemDescriptor] = []
    version: int | None = None
    if stem_json is not None:
        raw_version = stem_json.get("version")
        try:
            version = None if raw_version is None else int(raw_version)
        except (TypeError, ValueError):
            version = None
        for entry in stem_json.get("stems") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip().lower()
            if not name:
                continue
            color = entry.get("color")
            descriptors.append(
                StemMp4StemDescriptor(
                    name=name,
                    color=str(color) if color is not None else None,
                )
            )
    return StemMp4Metadata(
        title=tags.get("title"),
        artist=tags.get("artist"),
        album=tags.get("album"),
        stems=tuple(descriptors),
        stem_atom_version=version,
        raw_stem_json=stem_json,
    )


def _validate_stem_names(metadata: StemMp4Metadata, parts: list[StemMp4StreamProbe]) -> None:
    if not metadata.stems:
        return
    if len(metadata.stems) != 4:
        raise StemMp4Error(f"stem atom lists {len(metadata.stems)} stems; expected four")
    for expected, desc in zip(STEM_MP4_PART_IDS, metadata.stems, strict=True):
        if desc.name != expected:
            raise StemMp4Error(f"stem atom order mismatch: expected {expected!r}, got {desc.name!r}")


# ----- ISO BMFF stem atom ----------------------------------------------------


def _read_stem_atom_json(path: Path) -> dict[str, Any] | None:
    data = path.read_bytes()
    moov = _find_box(data, 0, len(data), b"moov")
    if moov is None:
        return None
    moov_start, moov_end, moov_header = moov
    stem = _find_box(data, moov_start + moov_header, moov_end, b"stem")
    if stem is None:
        return None
    stem_start, stem_end, stem_header = stem
    payload = data[stem_start + stem_header : stem_end]
    try:
        text = payload.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StemMp4Error("stem atom is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise StemMp4Error("stem atom JSON must be an object")
    return parsed


def _find_box(
    data: bytes,
    start: int,
    end: int,
    kind: bytes,
) -> tuple[int, int, int] | None:
    offset = start
    while offset + 8 <= end:
        header, box_start, box_end = _read_box_bounds(data, offset, end)
        if box_end is None:
            break
        box_type = data[offset + 4 : offset + 8]
        if box_type == kind:
            return box_start, box_end, header
        if box_type in (b"moov", b"udta", b"meta", b"trak", b"mdia", b"minf", b"stbl"):
            found = _find_box(data, box_start + header, box_end, kind)
            if found is not None:
                return found
        offset = box_end
    return None


def _read_box_bounds(data: bytes, offset: int, end: int) -> tuple[int, int, int | None]:
    if offset + 8 > end:
        return 8, offset, None
    size = struct.unpack(">I", data[offset : offset + 4])[0]
    header = 8
    if size == 0:
        size = end - offset
    elif size == 1:
        if offset + 16 > end:
            return header, offset, None
        size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        header = 16
    if size < header or offset + size > end:
        return header, offset, None
    return header, offset, offset + size


# ----- decode ----------------------------------------------------------------


def _ffmpeg_decode_to_wav(path: Path, stream_index: int) -> bytes:
    from apps.shared.ffmpeg import FfmpegUnavailable, resolve_ffmpeg

    try:
        ffmpeg = resolve_ffmpeg()
    except FfmpegUnavailable as exc:
        raise StemMp4Error(str(exc)) from exc
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        f"0:{stream_index}",
        "-f",
        "wav",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=_DECODE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise StemMp4Error(f"ffmpeg decode timed out after {_DECODE_TIMEOUT_S}s") from exc
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        message = tail[-1] if tail else "ffmpeg decode failed"
        raise StemMp4Error(message)
    if not completed.stdout:
        raise StemMp4Error("ffmpeg decode returned no PCM")
    return completed.stdout


__all__ = [
    "STEM_MP4_PART_IDS",
    "StemMp4Bundle",
    "StemMp4Error",
    "StemMp4Metadata",
    "StemMp4StemDescriptor",
    "StemMp4StreamProbe",
    "decode_stem_mp4_part",
    "read_stem_mp4",
    "resolve_ffprobe",
]
