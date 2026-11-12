"""Build ``<session_id>.wav`` from rolling ``audio_*.wav`` recorder segments.

``MasterMixWriter`` writes segment files stamped with UTC start times in the
filename. Split needs one joined timeline-aligned file; this module writes it
when missing or older than the newest segment, padding gaps over 1 s with
silence (segment names carry whole UTC seconds).
"""

from __future__ import annotations

import wave
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from . import paths as sets_paths
from .paths import joined_recording_path
from .record import _segment_start_from_name

GAP_TOLERANCE_S = 1.0
_CHUNK_FRAMES = 1 << 16


class SplitRefused(RuntimeError):
    """Split cannot proceed; carries a machine-readable :attr:`code`."""

    code: str = "SPLIT_REFUSED"


class RecordingNotJoined(SplitRefused):
    code = "RECORDING_NOT_JOINED"


class RecordingNotPcm16(SplitRefused):
    code = "RECORDING_NOT_PCM16"


class RecordingMixedFormat(SplitRefused):
    code = "RECORDING_MIXED_FORMAT"


def _raise_not_pcm16(path: Path, *, detail: str, cause: BaseException | None = None) -> NoReturn:
    msg = f"segment {path.name!r} {detail}"
    if cause is not None:
        raise RecordingNotPcm16(msg) from cause
    raise RecordingNotPcm16(msg)


def _raise_mixed_format(path: Path, params: tuple[int, int], expected: tuple[int, int]) -> NoReturn:
    raise RecordingMixedFormat(f"segments disagree on channels/rate: {path.name!r} has {params}, expected {expected}")


def _joined_is_current(joined: Path, wavs: list[Path]) -> bool:
    return joined.is_file() and (not wavs or joined.stat().st_mtime >= max(p.stat().st_mtime for p in wavs))


def _raise_no_segments_to_join(all_segments: list[Path]) -> NoReturn:
    mp3s = [p for p in all_segments if p.suffix.lower() == ".mp3"]
    if mp3s:
        raise RecordingNotJoined("no joined recording and only MP3 segments, which cannot be joined")
    raise RecordingNotJoined("no joined recording and no WAV segments to join")


def _pcm16_params(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
                _raise_not_pcm16(path, detail="must be 16-bit uncompressed PCM")
            return wf.getnchannels(), wf.getframerate()
    except wave.Error as exc:
        _raise_not_pcm16(path, detail=f"is not readable PCM16: {exc}", cause=exc)


def _segment_plan(
    wavs: list[Path],
    started_at: datetime,
) -> tuple[int, int, list[tuple[Path, int]]]:
    first_params: tuple[int, int] | None = None
    plan: list[tuple[Path, int]] = []
    for path in wavs:
        params = _pcm16_params(path)
        if first_params is None:
            first_params = params
        elif params != first_params:
            _raise_mixed_format(path, params, first_params)
        start_s = _segment_start_from_name(path.name, started_at)
        plan.append((path, round(start_s * params[1])))
    assert first_params is not None
    return first_params[0], first_params[1], plan


def _write_silence(out: wave.Wave_write, frames: int, silence_frame: bytes) -> None:
    remaining = frames
    while remaining > 0:
        take = min(remaining, _CHUNK_FRAMES)
        out.writeframes(silence_frame * take)
        remaining -= take


def _copy_segment(out: wave.Wave_write, path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        n_frames = wf.getnframes()
        remaining = n_frames
        while remaining > 0:
            take = min(remaining, _CHUNK_FRAMES)
            out.writeframes(wf.readframes(take))
            remaining -= take
        return n_frames


def _build_joined_file(
    part: Path,
    nchannels: int,
    framerate: int,
    plan: list[tuple[Path, int]],
) -> None:
    silence_frame = b"\x00\x00" * nchannels
    gap_limit = round(GAP_TOLERANCE_S * framerate)
    with wave.open(str(part), "wb") as out:
        out.setnchannels(nchannels)
        out.setsampwidth(2)
        out.setframerate(framerate)
        cursor = 0
        for path, target in plan:
            gap_frames = target - cursor
            if gap_frames > gap_limit:
                _write_silence(out, gap_frames, silence_frame)
                cursor = target
            cursor += _copy_segment(out, path)


def ensure_joined_recording(
    session_path: Path,
    session_id: str,
    started_at: datetime,
) -> Path:
    joined = joined_recording_path(session_path, session_id)
    all_segments = sets_paths.segment_files(session_path)
    wavs = [p for p in all_segments if p.suffix.lower() == ".wav"]

    if _joined_is_current(joined, wavs):
        return joined

    if not wavs:
        _raise_no_segments_to_join(all_segments)

    part = joined.with_name(joined.name + ".part")
    try:
        nchannels, framerate, plan = _segment_plan(wavs, started_at)
        _build_joined_file(part, nchannels, framerate, plan)
        part.replace(joined)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    else:
        return joined


__all__ = [
    "GAP_TOLERANCE_S",
    "RecordingMixedFormat",
    "RecordingNotJoined",
    "RecordingNotPcm16",
    "SplitRefused",
    "ensure_joined_recording",
]
