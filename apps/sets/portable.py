"""Portable set document export and import (issue #10387).

A recorded set normally lives as a session directory plus rows in ``sets.db``.
This module builds one JSON document (manifest, timeline events, tracklist,
transitions, labels) with no audio and no host paths, and can import it as a
new private session.

Events are read from ``SetsState(db_path=<sets_root>/sets.db).fetch_events``.
Only when the DB has no ``sets`` row for the session do we fall back to
``timeline.jsonl`` (a directory copied without its DB row).

The hidden-track flag is resolved through an optional ``track_lookup`` that
reads ``tracklist_hidden`` when issue #10337 lands on the main library model.
"""

from __future__ import annotations

import functools
import getpass
import json
import re
import shutil
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from apps.shared.process_liveness import pid_is_alive

from . import paths as sets_paths
from .classify import read_transitions
from .manifest import Manifest, now_iso, read_manifest, write_manifest
from .record import _SESSION_ID_PATTERN, TimelineJsonl
from .soundcloud_export import iter_kept_plays
from .state import Event, SetsState

SET_DOCUMENT_KIND = "open_dj.set_document"
SET_DOCUMENT_SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schemas" / "set_document.schema.json"

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")
_PATHY_KEYS = frozenset(
    {
        "path",
        "file_path",
        "filepath",
        "location",
        "file",
        "folder",
        "directory",
        "host",
        "hostname",
        "user",
        "username",
        "pid",
    }
)
_TRACK_VALUE_KEYS = frozenset({"title", "artist", "album"})


@dataclass(frozen=True)
class TrackInfo:
    artist: str | None
    title: str | None
    hidden: bool = False


TrackLookup = Callable[[Sequence[str]], Mapping[str, TrackInfo]]


class SetDocumentError(ValueError):
    """Base error with a stable machine code."""

    code: str = "SET_DOCUMENT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SessionRecording(SetDocumentError):
    code = "SESSION_RECORDING"


class SchemaVersionUnsupported(SetDocumentError):
    code = "SCHEMA_VERSION_UNSUPPORTED"

    def __init__(
        self,
        message: str,
        *,
        document_version: int,
        supported_version: int,
    ) -> None:
        super().__init__(message)
        self.document_version = document_version
        self.supported_version = supported_version


class SetDocumentInvalid(SetDocumentError):
    code = "SET_DOCUMENT_INVALID"

    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(message)
        self.path = path


@dataclass(frozen=True)
class ImportResult:
    session_id: str
    requested_session_id: str
    renamed: bool
    event_count: int
    message: str


def machine_identity() -> tuple[str, ...]:
    out: list[str] = []
    try:
        host = socket.gethostname()
        if len(host) >= 3:
            out.append(host)
            short = host.split(".", 1)[0]
            if short != host and len(short) >= 3:
                out.append(short)
    except OSError:
        pass
    try:
        user = getpass.getuser()
        if len(user) >= 3:
            out.append(user)
    except (OSError, KeyError):
        pass
    return tuple(out)


def is_recording(session_dir: Path) -> bool:
    pid_path = session_dir / "recorder.pid"
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid > 0 and pid_is_alive(pid)


@functools.cache
def _set_document_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_set_document(document: object) -> dict[str, Any]:
    """Schema-check a portable document; raise :class:`SetDocumentError` on failure."""
    if not isinstance(document, dict):
        raise SetDocumentInvalid("set document must be a JSON object", path="")
    raw_version = document.get("schema_version")
    if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version > SET_DOCUMENT_SCHEMA_VERSION:
        supported = SET_DOCUMENT_SCHEMA_VERSION
        raise SchemaVersionUnsupported(
            f"set document schema_version {raw_version} is newer than this reader's {supported}",
            document_version=raw_version,
            supported_version=supported,
        )
    schema = _set_document_schema()
    errors = list(Draft202012Validator(schema).iter_errors(document))
    if errors:
        err = best_match(errors)
        path = "/" + "/".join(str(p) for p in err.absolute_path)
        raise SetDocumentInvalid(err.message, path=path)
    return document


def _string_is_private(text: str, *, sets_root_text: str, identity: tuple[str, ...]) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if stripped.startswith(("/", "~", "\\")) or stripped.lower().startswith("file:"):
        return True
    if _DRIVE_PREFIX.match(stripped):
        return True
    for marker in ("/Users/", "/home/", "\\Users\\"):
        if marker in stripped:
            return True
    if sets_root_text and sets_root_text in stripped:
        return True
    lower = stripped.lower()
    return any(len(token) >= 3 and re.search(rf"\b{re.escape(token.lower())}\b", lower) for token in identity)


def _scrub_value(
    value: Any,
    *,
    sets_root_text: str,
    identity: tuple[str, ...],
) -> Any:
    if isinstance(value, str):
        return None if _string_is_private(value, sets_root_text=sets_root_text, identity=identity) else value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _PATHY_KEYS:
                continue
            cleaned = _scrub_value(item, sets_root_text=sets_root_text, identity=identity)
            if cleaned is None and isinstance(item, str):
                continue
            if cleaned is not None:
                out[key] = cleaned
        return out
    if isinstance(value, list):
        out_list: list[Any] = []
        for item in value:
            cleaned = _scrub_value(item, sets_root_text=sets_root_text, identity=identity)
            if isinstance(item, str) and cleaned is None:
                continue
            if cleaned is not None:
                out_list.append(cleaned)
        return out_list
    return value


def _scrub_capture_device(device: str, *, sets_root_text: str, identity: tuple[str, ...]) -> str:
    if _string_is_private(device, sets_root_text=sets_root_text, identity=identity):
        return "unknown"
    return device


def _scrub_text(value: str | None, *, sets_root_text: str, identity: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    return None if _string_is_private(value, sets_root_text=sets_root_text, identity=identity) else value


def _read_labels_file(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "labels.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SetDocumentError(f"labels.jsonl line is not valid JSON: {exc}") from exc
    return rows


def _fetch_export_events(session_id: str, *, root: Path, state: SetsState) -> list[dict[str, Any]]:
    # DB is authoritative; timeline.jsonl is the fallback when no sets row exists.
    if state.get_session(session_id) is None:
        session_dir = sets_paths.session_dir(session_id, root=root)
        return list(TimelineJsonl(session_dir / "timeline.jsonl").iter_events())
    db_events = state.fetch_events(session_id)
    return [
        {
            "timestamp_s": ev.timestamp_s,
            "wall_clock": ev.wall_clock,
            "action": ev.action,
            "source": ev.source,
            "deck": ev.deck,
            "track_stable_id": ev.track_stable_id,
            "value": dict(ev.value),
        }
        for ev in db_events
    ]


def _build_tracklist(
    events: list[dict[str, Any]],
    *,
    track_lookup: TrackLookup | None,
) -> tuple[list[dict[str, Any]], set[str]]:
    hidden_ids: set[str] = set()
    lookup_map: dict[str, TrackInfo] = {}
    if track_lookup is not None:
        ids = [sid for ev in events if (sid := ev.get("track_stable_id")) is not None and isinstance(sid, str) and sid]
        if ids:
            lookup_map = dict(track_lookup(ids))

    rows: list[dict[str, Any]] = []
    for row in iter_kept_plays(iter(events)):
        stable_id = row.track_stable_id
        artist = row.artist
        title = row.title
        hidden = False
        if stable_id and stable_id in lookup_map:
            info = lookup_map[stable_id]
            hidden = info.hidden
            if hidden:
                hidden_ids.add(stable_id)
            else:
                if artist is None:
                    artist = info.artist
                if title is None:
                    title = info.title
        if hidden:
            artist = None
            title = None
        rows.append(
            {
                "timestamp_s": row.timestamp_s,
                "stable_id": stable_id,
                "artist": artist,
                "title": title,
                "hidden": hidden,
                "deck": row.deck,
                "source": row.source,
            }
        )
    return rows, hidden_ids


def _strip_hidden_from_events(events: list[dict[str, Any]], hidden_ids: set[str]) -> list[dict[str, Any]]:
    if not hidden_ids:
        return events
    out: list[dict[str, Any]] = []
    for ev in events:
        ev_copy = dict(ev)
        sid = ev_copy.get("track_stable_id")
        if isinstance(sid, str) and sid in hidden_ids:
            value = ev_copy.get("value")
            if isinstance(value, dict):
                scrubbed = {k: v for k, v in value.items() if k not in _TRACK_VALUE_KEYS}
                ev_copy["value"] = scrubbed
        out.append(ev_copy)
    return out


def build_set_document(
    session_id: str,
    *,
    sets_root: Path | None = None,
    track_lookup: TrackLookup | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    root = sets_paths.resolve_sets_root(sets_root)
    session_dir = sets_paths.session_dir(session_id, root=root)
    if not (session_dir / "manifest.json").is_file():
        raise LookupError(f"session {session_id!r} not found")
    if is_recording(session_dir):
        raise SessionRecording(f"session {session_id!r} is still recording")

    state = SetsState(db_path=root / "sets.db")
    events = _fetch_export_events(session_id, root=root, state=state)
    identity = machine_identity()
    sets_root_text = str(root.resolve())

    tracklist, hidden_ids = _build_tracklist(events, track_lookup=track_lookup)
    tracklist = [
        {
            **row,
            "artist": _scrub_text(row["artist"], sets_root_text=sets_root_text, identity=identity),
            "title": _scrub_text(row["title"], sets_root_text=sets_root_text, identity=identity),
        }
        for row in tracklist
    ]
    events = _strip_hidden_from_events(events, hidden_ids)

    scrubbed_events = [
        {
            "timestamp_s": ev["timestamp_s"],
            "wall_clock": ev["wall_clock"],
            "action": ev["action"],
            "source": ev["source"],
            "deck": ev.get("deck"),
            "track_stable_id": ev.get("track_stable_id"),
            "value": _scrub_value(ev.get("value", {}), sets_root_text=sets_root_text, identity=identity),
        }
        for ev in events
    ]

    manifest_obj = read_manifest(session_dir)
    db_row = state.get_session(session_id)
    notes = _scrub_text(
        db_row.notes if db_row is not None else None,
        sets_root_text=sets_root_text,
        identity=identity,
    )

    manifest_payload: dict[str, Any] = {
        "session_id": manifest_obj.session_id,
        "started_at": manifest_obj.started_at,
        "ended_at": manifest_obj.ended_at,
        "capture_device": _scrub_capture_device(
            manifest_obj.capture_device,
            sets_root_text=sets_root_text,
            identity=identity,
        ),
        "share_state": "private",
        "event_count": len(scrubbed_events),
        "deck_sources": list(manifest_obj.deck_sources),
        "watermark": manifest_obj.watermark,
        "notes": notes,
        "audio": "absent",
    }

    transitions = read_transitions(session_id, sets_root=root)
    transitions = [
        _scrub_value(row, sets_root_text=sets_root_text, identity=identity)  # type: ignore[arg-type]
        for row in transitions
    ]
    labels = [
        _scrub_value(row, sets_root_text=sets_root_text, identity=identity)  # type: ignore[arg-type]
        for row in _read_labels_file(session_dir)
    ]

    document: dict[str, Any] = {
        "kind": SET_DOCUMENT_KIND,
        "schema_version": SET_DOCUMENT_SCHEMA_VERSION,
        "exported_at": now or now_iso(),
        "audio": "absent",
        "manifest": manifest_payload,
        "events": scrubbed_events,
        "tracklist": tracklist,
        "transitions": transitions,
        "labels": labels,
    }
    validate_set_document(document)
    return document


def free_session_id(
    requested: str,
    *,
    sets_root: Path,
    state: SetsState,
) -> str:
    sets_paths.session_dir(requested, root=sets_root)

    def _free(candidate: str) -> bool:
        return not (sets_root / candidate).exists() and state.get_session(candidate) is None

    if _free(requested):
        return requested
    base = requested
    if _SESSION_ID_PATTERN.match(requested):
        base = re.sub(r"_\d+$", "", requested)
    for n in range(2, 10_000):
        candidate = f"{base}_{n}"
        sets_paths.session_dir(candidate, root=sets_root)
        if _free(candidate):
            return candidate
    raise SetDocumentError(f"could not find a free session id near {requested!r}")


def import_set_document(
    document: dict[str, Any],
    *,
    sets_root: Path | None = None,
) -> ImportResult:
    validate_set_document(document)
    root = sets_paths.resolve_sets_root(sets_root)
    state = SetsState(db_path=root / "sets.db")
    requested = str(document["manifest"]["session_id"])
    target = free_session_id(requested, sets_root=root, state=state)
    session_dir = sets_paths.session_dir(target, root=root)
    session_dir.mkdir(parents=True, exist_ok=False)

    manifest_data = dict(document["manifest"])
    events_data = document["events"]
    transitions_data = document.get("transitions") or []
    labels_data = document.get("labels") or []

    db_written = False
    try:
        manifest = Manifest(
            session_id=target,
            started_at=str(manifest_data["started_at"]),
            ended_at=manifest_data.get("ended_at"),
            capture_device=str(manifest_data["capture_device"]),
            share_state="private",
            event_count=len(events_data),
            deck_sources=list(manifest_data.get("deck_sources") or []),
            mp3_segments=[],
            watermark=str(manifest_data.get("watermark", "")),
            schema_version=int(manifest_data.get("schema_version", 1)),
            audio="absent",
        )
        write_manifest(session_dir, manifest)

        timeline = TimelineJsonl(session_dir / "timeline.jsonl")
        events: list[Event] = []
        for row in events_data:
            ev = Event(
                session_id=target,
                timestamp_s=float(row["timestamp_s"]),
                wall_clock=str(row["wall_clock"]),
                action=str(row["action"]),
                source=str(row["source"]),
                deck=row.get("deck"),
                track_stable_id=row.get("track_stable_id"),
                value=dict(row.get("value") or {}),
            )
            timeline.append(ev)
            events.append(ev)

        if transitions_data:
            trans_path = session_dir / "transitions.jsonl"
            lines = [json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in transitions_data]
            trans_path.write_text("".join(lines), encoding="utf-8", newline="\n")

        if labels_data:
            labels_path = session_dir / "labels.jsonl"
            lines = [json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in labels_data]
            labels_path.write_text("".join(lines), encoding="utf-8", newline="\n")

        state.import_session(
            target,
            started_at=manifest.started_at,
            ended_at=manifest.ended_at,
            capture_device=manifest.capture_device,
            notes=manifest_data.get("notes"),
            events=events,
        )
        db_written = True
    except Exception:
        shutil.rmtree(session_dir, ignore_errors=True)
        if db_written:
            state.delete_session(target)
        raise

    renamed = target != requested
    message = f"imported as {target}: {requested} already exists" if renamed else f"imported as {target}"
    return ImportResult(
        session_id=target,
        requested_session_id=requested,
        renamed=renamed,
        event_count=len(events_data),
        message=message,
    )


__all__ = [
    "SET_DOCUMENT_KIND",
    "SET_DOCUMENT_SCHEMA_VERSION",
    "ImportResult",
    "SchemaVersionUnsupported",
    "SessionRecording",
    "SetDocumentError",
    "SetDocumentInvalid",
    "TrackInfo",
    "TrackLookup",
    "build_set_document",
    "free_session_id",
    "import_set_document",
    "is_recording",
    "machine_identity",
    "pid_is_alive",
    "validate_set_document",
]
