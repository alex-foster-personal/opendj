"""Tracklist of a recorded set (issue #10391), from sets sqlite events.

Does not read ``timeline.jsonl`` as the primary source: deck sources write
``track_loaded`` to sqlite only. Jsonl is a fallback when sqlite has no rows.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from apps.shared.m3u8 import M3u8Entry, render_m3u8

from . import paths as sets_paths
from .audio import list_segments
from .classify import read_transitions
from .record import TimelineJsonl
from .sessions import get_session
from .soundcloud_export import (
    LICENSING_REMINDER,
    SessionNotFound,
    _display_name,
    _optional_string,
    _stable_id,
    _timestamp_s,
    format_soundcloud_timestamp,
)
from .state import SetsState

TracklistFormat = Literal["txt", "md", "csv", "timestamped", "soundcloud", "cue", "m3u8"]
TEXT_FORMATS = ("txt", "md", "csv", "timestamped", "soundcloud")
FILE_FORMATS = ("cue", "m3u8")
HIDDEN_LABEL = "ID"

_FORMAT_MEDIA: dict[TracklistFormat, tuple[str, str]] = {
    "txt": ("text/plain", ".txt"),
    "md": ("text/markdown", ".md"),
    "csv": ("text/csv", ".csv"),
    "timestamped": ("text/plain", ".timestamped.txt"),
    "soundcloud": ("text/plain", ".soundcloud.txt"),
    "cue": ("application/x-cue", ".cue"),
    "m3u8": ("audio/x-mpegurl", ".m3u8"),
}


@dataclass(frozen=True)
class LibraryTrack:
    artist: str | None = None
    title: str | None = None
    key: str | None = None
    duration_ms: int | None = None
    file_path: str | None = None


TrackLookup = Callable[[Sequence[str]], Mapping[str, LibraryTrack]]


@dataclass(frozen=True)
class TracklistRow:
    position: int
    start_s: float
    start_label: str
    artist: str | None
    title: str | None
    display_name: str
    key: str | None
    played_s: float | None
    played_label: str | None
    transition_label: str | None
    hidden: bool
    track_stable_id: str | None
    source: str | None
    deck: str | None
    duration_ms: int | None
    file_path: str | None


@dataclass(frozen=True)
class Tracklist:
    session_id: str
    rows: list[TracklistRow]
    segments: list[tuple[str, float]]

    def to_dict(self) -> dict[str, Any]:
        rows_out: list[dict[str, Any]] = []
        for row in self.rows:
            d = asdict(row)
            d.pop("file_path", None)
            rows_out.append(d)
        return {"session_id": self.session_id, "rows": rows_out}


def format_start_label(start_s: float) -> str:
    total = max(0, math.floor(start_s))
    minutes, seconds = divmod(int(total), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _duration_ms_from_value(value: dict[str, Any]) -> int | None:
    ms = value.get("duration_ms")
    if isinstance(ms, int) and not isinstance(ms, bool):
        return ms
    if isinstance(ms, float) and not isinstance(ms, bool):
        return int(ms)
    dur_s = value.get("duration_s")
    if isinstance(dur_s, (int, float)) and not isinstance(dur_s, bool):
        return round(float(dur_s) * 1000)
    return None


def _iter_kept_track_events(events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Kept ``track_loaded`` rows in play order (sqlite or jsonl dicts)."""
    last_stable_id: str | None = None
    have_last = False
    for event in events:
        if event.get("action") != "track_loaded":
            continue
        value = event.get("value")
        if not isinstance(value, dict):
            value = {}
        title = _optional_string(value.get("title"))
        stable_id = _stable_id(event.get("track_stable_id"))
        if stable_id is None and title is None:
            continue
        if have_last and stable_id is not None and stable_id == last_stable_id:
            continue
        audible = _numeric(value.get("audible_s"))
        threshold = _numeric(value.get("threshold_s"))
        if audible is not None and threshold is not None and audible < threshold:
            continue
        yield event
        last_stable_id = stable_id
        have_last = True


def _collect_events(session_id: str, *, sets_root: Path | None, state: SetsState) -> list[dict[str, Any]]:
    db_events = state.fetch_events(session_id)
    has_track = any(e.action == "track_loaded" for e in db_events)
    if has_track:
        return [e.to_dict() for e in db_events]
    session_path = sets_paths.session_dir(session_id, root=sets_root)
    return list(TimelineJsonl(session_path / "timeline.jsonl").iter_events())


def _transition_predicted_class(chosen: dict[str, Any] | None) -> str | None:
    if chosen is None:
        return None
    pc = chosen.get("predicted_class")
    return pc if isinstance(pc, str) else None


def _find_transition_to_track(
    transitions: list[dict[str, Any]],
    used: set[int],
    *,
    to_track: str,
    min_start_s: float,
) -> tuple[dict[str, Any] | None, int | None]:
    for idx, tr in enumerate(transitions):
        if idx in used:
            continue
        t_change = _numeric(tr.get("t_change_s"))
        if tr.get("to_track") != to_track or t_change is None:
            continue
        if t_change >= min_start_s:
            return tr, idx
    return None, None


def _find_transition_in_window(
    transitions: list[dict[str, Any]],
    used: set[int],
    *,
    window_start: float,
    window_end: float,
) -> tuple[dict[str, Any] | None, int | None]:
    for idx, tr in enumerate(transitions):
        if idx in used:
            continue
        t_change = _numeric(tr.get("t_change_s"))
        if t_change is None:
            continue
        if window_start <= t_change < window_end:
            return tr, idx
    return None, None


def _transition_labels(
    rows: list[TracklistRow],
    transitions: list[dict[str, Any]],
) -> list[str | None]:
    labels: list[str | None] = [None] * len(rows)
    used: set[int] = set()
    for i in range(len(rows) - 1):
        row_i = rows[i]
        row_j = rows[i + 1]
        to_track = row_j.track_stable_id
        if to_track is None:
            continue
        chosen, idx = _find_transition_to_track(
            transitions,
            used,
            to_track=to_track,
            min_start_s=row_i.start_s,
        )
        if chosen is None:
            next_end = rows[i + 2].start_s if i + 2 < len(rows) else math.inf
            chosen, idx = _find_transition_in_window(
                transitions,
                used,
                window_start=row_j.start_s,
                window_end=next_end,
            )
        if idx is not None:
            used.add(idx)
        labels[i] = _transition_predicted_class(chosen)
    return labels


def _played_seconds(
    value: dict[str, Any],
    start_s: float,
    *,
    next_start: float | None,
    session_duration: float | None,
) -> float | None:
    audible = _numeric(value.get("audible_s"))
    if audible is not None and audible >= 0:
        return audible
    if next_start is not None:
        return max(0.0, next_start - start_s)
    if session_duration is not None and session_duration > start_s:
        return session_duration - start_s
    return None


def build_tracklist(
    session_id: str,
    *,
    sets_root: Path | None = None,
    state: SetsState | None = None,
    track_lookup: TrackLookup | None = None,
    hidden_stable_ids: frozenset[str] = frozenset(),
) -> Tracklist:
    session = get_session(session_id, sets_root=sets_root)
    if session is None:
        raise SessionNotFound(f"session {session_id!r} not found")
    root = sets_paths.resolve_sets_root(sets_root)
    state = state or SetsState(db_path=root / "sets.db")
    raw_events = _collect_events(session_id, sets_root=sets_root, state=state)
    kept = list(_iter_kept_track_events(iter(raw_events)))

    stable_ids = [sid for e in kept if (sid := _stable_id(e.get("track_stable_id"))) is not None]
    library: Mapping[str, LibraryTrack] = {}
    if track_lookup is not None and stable_ids:
        library = dict(track_lookup(stable_ids))

    session_duration = session.summary.duration_s
    partial_rows: list[TracklistRow] = []

    for position, event in enumerate(kept, start=1):
        raw_value = event.get("value")
        value: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
        start_s = _timestamp_s(event)
        stable_id = _stable_id(event.get("track_stable_id"))
        title = _optional_string(value.get("title"))
        artist = _optional_string(value.get("artist"))
        lib = library.get(stable_id) if stable_id else None
        if lib is not None:
            if artist is None and lib.artist:
                artist = lib.artist
            if title is None and lib.title:
                title = lib.title
        hidden = stable_id is not None and stable_id in hidden_stable_ids
        if hidden:
            artist, title = HIDDEN_LABEL, HIDDEN_LABEL
        display = f"{HIDDEN_LABEL} - {HIDDEN_LABEL}" if hidden else _display_name(artist, title, stable_id)
        key = lib.key if lib else None
        duration_ms = _duration_ms_from_value(value)
        if lib is not None:
            if duration_ms is None and lib.duration_ms is not None:
                duration_ms = lib.duration_ms
            if lib.key and key is None:
                key = lib.key
        file_path = lib.file_path if lib else None
        next_start = _timestamp_s(kept[position]) if position < len(kept) else None
        played_s = _played_seconds(
            value,
            start_s,
            next_start=next_start,
            session_duration=session_duration,
        )
        played_label = format_soundcloud_timestamp(played_s) if played_s is not None else None
        partial_rows.append(
            TracklistRow(
                position=position,
                start_s=start_s,
                start_label=format_start_label(start_s),
                artist=artist,
                title=title,
                display_name=display,
                key=key,
                played_s=played_s,
                played_label=played_label,
                transition_label=None,
                hidden=hidden,
                track_stable_id=stable_id,
                source=event.get("source") if isinstance(event.get("source"), str) else None,
                deck=event.get("deck") if isinstance(event.get("deck"), str) else None,
                duration_ms=duration_ms,
                file_path=file_path,
            )
        )

    transitions = read_transitions(session_id, sets_root=sets_root)
    trans_labels = _transition_labels(partial_rows, transitions)
    rows = [TracklistRow(**{**asdict(row), "transition_label": trans_labels[i]}) for i, row in enumerate(partial_rows)]

    segments = [(s.name, s.start_t_s) for s in list_segments(session_id, sets_root=sets_root)]
    return Tracklist(session_id=session.summary.session_id, rows=rows, segments=segments)


def tracklist_media_type(fmt: TracklistFormat) -> str:
    return _FORMAT_MEDIA[fmt][0]


def tracklist_filename(session_id: str, fmt: TracklistFormat) -> str:
    return f"{session_id}{_FORMAT_MEDIA[fmt][1]}"


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _render_txt(tracklist: Tracklist) -> str:
    if not tracklist.rows:
        return ""
    lines = [f"{row.position}. {row.display_name}" for row in tracklist.rows]
    return "\n".join(lines) + "\n"


def _render_timestamped(tracklist: Tracklist) -> str:
    if not tracklist.rows:
        return ""
    lines = [f"[{row.start_label}] {row.display_name}" for row in tracklist.rows]
    return "\n".join(lines) + "\n"


def _render_soundcloud(tracklist: Tracklist) -> str:
    if not tracklist.rows:
        return ""
    lines = [f"{format_soundcloud_timestamp(row.start_s)} {row.display_name}" for row in tracklist.rows]
    return "\n".join(lines) + "\n"


def _render_md(tracklist: Tracklist) -> str:
    if not tracklist.rows:
        return ""
    header = "| # | Start | Artist | Title | Key | Played | Transition |"
    sep = "| --- | --- | --- | --- | --- | --- | --- |"
    body: list[str] = [header, sep]
    for row in tracklist.rows:
        artist = _escape_md_cell(row.artist or "")
        title = _escape_md_cell(row.title or "")
        key = _escape_md_cell(row.key or "")
        played = _escape_md_cell(row.played_label or "")
        trans = _escape_md_cell(row.transition_label or "")
        body.append(f"| {row.position} | {row.start_label} | {artist} | {title} | {key} | {played} | {trans} |")
    return "\n".join(body) + "\n"


def _render_csv(tracklist: Tracklist) -> str:
    if not tracklist.rows:
        return ""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["position", "start", "artist", "title", "key", "played", "transition"])
    for row in tracklist.rows:
        writer.writerow(
            [
                row.position,
                row.start_label,
                row.artist or "",
                row.title or "",
                row.key or "",
                row.played_label or "",
                row.transition_label or "",
            ]
        )
    return buf.getvalue()


def _cue_quote(text: str) -> str:
    return text.replace('"', "'")


def _cue_index_time(seconds_from_segment: float) -> str:
    total_frames = max(0, round(seconds_from_segment * 75))
    frames = total_frames % 75
    total_secs = total_frames // 75
    minutes, secs = divmod(total_secs, 60)
    return f"{minutes:02d}:{secs:02d}:{frames:02d}"


def _segment_for_row(segments: list[tuple[str, float]], start_s: float) -> str | None:
    name: str | None = None
    for seg_name, seg_start in segments:
        if seg_start <= start_s:
            name = seg_name
        else:
            break
    return name


def _render_cue(tracklist: Tracklist) -> str:
    lines: list[str] = [f'TITLE "{_cue_quote(tracklist.session_id)}"']
    if not tracklist.rows:
        return "\n".join(lines) + "\n"

    if not tracklist.segments:
        lines.append('REM COMMENT "no recorded audio"')
        lines.append(f'FILE "{_cue_quote(tracklist.session_id)}.wav" WAVE')
        track_no = 1
        base = 0.0
        for row in tracklist.rows:
            lines.append(f"  TRACK {track_no:02d} AUDIO")
            title = row.title or row.display_name
            lines.append(f'    TITLE "{_cue_quote(title)}"')
            if row.artist:
                lines.append(f'    PERFORMER "{_cue_quote(row.artist)}"')
            rel = row.start_s - base
            lines.append(f"    INDEX 01 {_cue_index_time(rel)}")
            track_no += 1
        return "\n".join(lines) + "\n"

    by_segment: dict[str, list[TracklistRow]] = {}
    for row in tracklist.rows:
        seg = _segment_for_row(tracklist.segments, row.start_s)
        if seg is None:
            continue
        by_segment.setdefault(seg, []).append(row)

    track_no = 1
    for seg_name, seg_start in tracklist.segments:
        rows_in_seg = by_segment.get(seg_name, [])
        if not rows_in_seg:
            continue
        first_path = next((r.file_path for r in rows_in_seg if r.file_path), None)
        ext = "MP3"
        if first_path and first_path.lower().endswith((".wav", ".wave")):
            ext = "WAVE"
        lines.append(f'FILE "{_cue_quote(seg_name)}" {ext}')
        for row in rows_in_seg:
            lines.append(f"  TRACK {track_no:02d} AUDIO")
            title = row.title or row.display_name
            lines.append(f'    TITLE "{_cue_quote(title)}"')
            if row.artist:
                lines.append(f'    PERFORMER "{_cue_quote(row.artist)}"')
            rel = row.start_s - seg_start
            lines.append(f"    INDEX 01 {_cue_index_time(rel)}")
            track_no += 1

    return "\n".join(lines) + "\n"


def _render_m3u8(tracklist: Tracklist) -> str:
    entries: list[M3u8Entry] = []
    comments: list[str] = []
    for row in tracklist.rows:
        if row.file_path:
            dur = 0
            if row.duration_ms is not None:
                dur = max(0, round(row.duration_ms / 1000))
            artist = row.artist or ""
            title = row.title or row.display_name
            entries.append(
                M3u8Entry(
                    duration_s=dur,
                    artist=artist,
                    title=title,
                    location=row.file_path,
                )
            )
        else:
            comments.append(f"# not in the library: {row.display_name}")
    body = render_m3u8(entries)
    if comments:
        body = body.rstrip("\n") + "\n" + "\n".join(comments) + "\n"
    if not tracklist.rows:
        return ""
    return body


def render_tracklist(tracklist: Tracklist, fmt: TracklistFormat) -> str:
    if fmt == "txt":
        return _render_txt(tracklist)
    if fmt == "md":
        return _render_md(tracklist)
    if fmt == "csv":
        return _render_csv(tracklist)
    if fmt == "timestamped":
        return _render_timestamped(tracklist)
    if fmt == "soundcloud":
        return _render_soundcloud(tracklist)
    if fmt == "cue":
        return _render_cue(tracklist)
    if fmt == "m3u8":
        return _render_m3u8(tracklist)
    raise ValueError(f"unknown format {fmt!r}")


__all__ = [
    "FILE_FORMATS",
    "HIDDEN_LABEL",
    "LICENSING_REMINDER",
    "TEXT_FORMATS",
    "LibraryTrack",
    "SessionNotFound",
    "TrackLookup",
    "Tracklist",
    "TracklistFormat",
    "TracklistRow",
    "build_tracklist",
    "format_start_label",
    "render_tracklist",
    "tracklist_filename",
    "tracklist_media_type",
]
