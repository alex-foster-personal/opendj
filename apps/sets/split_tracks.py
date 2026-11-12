"""Cut a joined session WAV into one 16-bit PCM file per kept track_loaded row.

Reads ``<session_dir>/<session_id>.wav`` and builds it from rolling ``audio_*.wav``
segments when it is missing or older than the newest segment
(:func:`join_recording.ensure_joined_recording`). Frame 0 of the joined file is
session start, matching ``timestamp_s`` in ``timeline.jsonl``.

Cut rows reuse :func:`iter_kept_plays` from :mod:`apps.sets.soundcloud_export`
until #10086 centralises tracklist export.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths as sets_paths
from .join_recording import (
    RecordingMixedFormat,
    RecordingNotJoined,
    RecordingNotPcm16,
    SplitRefused,
    ensure_joined_recording,
)
from .manifest import TrackFile, read_manifest, write_manifest
from .paths import sanitize_filename_part
from .record import TimelineJsonl
from .soundcloud_export import SessionNotFound, SoundcloudTracklistRow, iter_kept_plays


class NoTracklist(SplitRefused):
    code = "NO_TRACKLIST"


@dataclass(frozen=True)
class Cut:
    number: int
    name: str
    start_frame: int
    end_frame: int


def track_file_name(
    number: int,
    width: int,
    artist: str | None,
    title: str | None,
    *,
    hidden: bool,
) -> str:
    if hidden:
        return f"{number:0{width}d} ID - ID.wav"
    art = sanitize_filename_part(artist or "")[:100].strip(" .") or "Unknown"
    tit = sanitize_filename_part(title or "")[:100].strip(" .") or "Unknown"
    return f"{number:0{width}d} {art} - {tit}.wav"


def plan_cuts(
    rows: list[SoundcloudTracklistRow],
    *,
    frame_rate: int,
    n_frames: int,
    hidden_stable_ids: frozenset[str],
) -> list[Cut]:
    if not rows or n_frames <= 0:
        return []

    starts: list[tuple[SoundcloudTracklistRow, int]] = []
    for row in rows:
        start = min(round(row.timestamp_s * frame_rate), n_frames)
        starts.append((row, start))

    # force first segment from frame 0
    if starts:
        row0, _ = starts[0]
        starts[0] = (row0, 0)

    # drop past end, dedupe same start (keep last)
    filtered: list[tuple[SoundcloudTracklistRow, int]] = []
    for row, start in starts:
        if start >= n_frames:
            continue
        if filtered and filtered[-1][1] == start:
            filtered[-1] = (row, start)
        else:
            filtered.append((row, start))

    if not filtered:
        return []

    count = len(filtered)
    width = max(2, len(str(count)))
    cuts: list[Cut] = []
    for i, (row, start_frame) in enumerate(filtered):
        end_frame = filtered[i + 1][1] if i + 1 < len(filtered) else n_frames
        if end_frame <= start_frame:
            continue
        hidden = bool(row.track_stable_id and row.track_stable_id in hidden_stable_ids)
        name = track_file_name(
            i + 1,
            width,
            row.artist,
            row.title,
            hidden=hidden,
        )
        cuts.append(Cut(number=i + 1, name=name, start_frame=start_frame, end_frame=end_frame))
    return cuts


def _write_slice(
    src: wave.Wave_read,
    cut: Cut,
    dest: Path,
    *,
    nchannels: int,
    framerate: int,
) -> int:
    src.setpos(cut.start_frame)
    part = dest.with_name(dest.name + ".part")
    n_to_read = cut.end_frame - cut.start_frame
    chunk_frames = 1 << 16
    with wave.open(str(part), "wb") as out:
        out.setnchannels(nchannels)
        out.setsampwidth(2)
        out.setframerate(framerate)
        remaining = n_to_read
        while remaining > 0:
            take = min(remaining, chunk_frames)
            out.writeframes(src.readframes(take))
            remaining -= take
    part.replace(dest)
    return dest.stat().st_size


def _resolve_stale_path(session_path: Path, stored: str) -> Path:
    p = Path(stored)
    if p.is_absolute():
        return p
    return session_path / stored


def split_recording(
    session_id: str,
    *,
    sets_root: Path | None = None,
    out_dir: Path | None = None,
    hidden_stable_ids: frozenset[str] = frozenset(),
) -> list[TrackFile]:
    session_path = sets_paths.session_dir(session_id, root=sets_root)
    manifest_path = session_path / "manifest.json"
    if not manifest_path.is_file():
        raise SessionNotFound(f"session {session_id!r} not found")

    prior = read_manifest(session_path)
    joined = ensure_joined_recording(session_path, session_id, datetime.fromisoformat(prior.started_at))

    if out_dir is not None:
        if not out_dir.is_absolute():
            raise ValueError("out_dir must be an absolute path")
        dest_dir = out_dir.resolve()
    else:
        dest_dir = session_path

    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with wave.open(str(joined), "rb") as wf:
            if wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
                raise RecordingNotPcm16("joined recording must be 16-bit uncompressed PCM")
            framerate = wf.getframerate()
            nchannels = wf.getnchannels()
            n_frames = wf.getnframes()
            events = TimelineJsonl(session_path / "timeline.jsonl").iter_events()
            rows = list(iter_kept_plays(events))
            cuts = plan_cuts(
                rows,
                frame_rate=framerate,
                n_frames=n_frames,
                hidden_stable_ids=hidden_stable_ids,
            )
            if not cuts:
                raise NoTracklist("session has no tracklist rows inside the recording")

            prior = read_manifest(session_path)
            new_names = {c.name for c in cuts}
            for old in prior.track_files:
                stale = _resolve_stale_path(session_path, old.path)
                if stale.name not in new_names and stale.is_file():
                    stale.unlink()

            files: list[TrackFile] = []
            for cut in cuts:
                out_path = dest_dir / cut.name
                size = _write_slice(
                    wf,
                    cut,
                    out_path,
                    nchannels=nchannels,
                    framerate=framerate,
                )
                path_str = str(out_path.resolve()) if out_dir is not None else cut.name
                files.append(
                    TrackFile(
                        name=cut.name,
                        path=path_str,
                        start_t_s=cut.start_frame / framerate,
                        duration_s=(cut.end_frame - cut.start_frame) / framerate,
                        size_bytes=size,
                    )
                )

    except wave.Error as exc:
        raise RecordingNotPcm16(f"joined recording is not readable PCM16: {exc}") from exc

    manifest = read_manifest(session_path)
    manifest.track_files = files
    write_manifest(session_path, manifest)
    return files


__all__ = [
    "Cut",
    "NoTracklist",
    "RecordingMixedFormat",
    "RecordingNotJoined",
    "RecordingNotPcm16",
    "SplitRefused",
    "plan_cuts",
    "split_recording",
    "track_file_name",
]
