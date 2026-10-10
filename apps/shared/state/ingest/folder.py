"""Folder -> state.db ingest: a library for someone with no rekordbox.

The rekordbox adapter is the rich path. This is the honest poor one: walk a
directory, read what the tags say, and write tracks with NO analysis at all.
It does not invent a BPM, a key or a beatgrid, and it does not pretend the
result is an analysed library -- the report says how many tracks carry no
analysed field, and that number is the whole point. A BPM or key the FILE'S
OWN TAGS carry is recorded (``source="inferred"``) only where the track has
no value yet, so a rescan can never overwrite an analysed or edited one.

Three things it refuses to be vague about:

* A DENIED folder is not an empty one. macOS answers a TCC-blocked listing
  with an empty listing, so a walk alone cannot tell forty thousand tracks
  from none. Every root is probed with :func:`fs_access.probe_readable`
  first and a denial is reported as a denial.
* An EVICTED iCloud placeholder is not a local file. Those are counted and
  skipped, never opened -- opening one asks iCloud to download it.
* The denominators are named. ``files_seen`` counts readable, materialised
  audio files; ``files_dataless``, ``files_rejected_unplayable`` and
  ``unreadable_roots`` are what that number does not cover.
* Corrupt or non-audio payloads with an allowlisted extension are counted in
  ``files_rejected_unplayable`` and never become track rows.

Safety mirrors the rekordbox adapter exactly: dry-run by default via an
outer write unit (:func:`apps.shared.state.db.write_unit`) that is
discarded, and the event bus swapped for a neutral silent drop-in from
:mod:`apps.shared.state.events` while it is, so a rolled-back run publishes
no phantom events. This module must not import any Rekordbox code; the
no-rekordbox onboarding path depends on it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from apps.shared import audio_files, audio_playable, fs_access, fs_residency, hashing
from apps.shared.audio_playable import UnplayableAudioError
from apps.shared.scan_mass_missing import MassMissingError, guard_roots
from apps.shared.state import db as state_db
from apps.shared.state import deleted_tracks
from apps.shared.state import ids as state_ids
from apps.shared.state import paths as state_paths
from apps.shared.state import provenance as state_provenance
from apps.shared.state.events import _DryRunSilentBus
from apps.shared.state.ingest.path_collisions import (
    PathCollisionError,
    assert_no_path_collisions,
    path_collision_key,
)
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import DELETED_FILE_MISSING
from apps.shared.stem_outputs import is_stem_output

ADAPTER_ID: str = "folder"

_ClockFn = Callable[[], _dt.datetime]
#: ``(done, total, message)``. Total is known before the first write because
#: the walk completes first -- a bar that cannot say "of what" is a spinner.
ProgressFn = Callable[[int, int, str], None]


@dataclasses.dataclass
class FolderIngestReport:
    """What the walk found and what was written. Denominators are named."""

    roots: list[str] = dataclasses.field(default_factory=list)
    #: Folders macOS refused to list. Everything below covers what was left.
    unreadable_roots: list[str] = dataclasses.field(default_factory=list)
    #: Readable, materialised audio files. THE denominator.
    files_seen: int = 0
    #: iCloud placeholders: present, sized, no local bytes. Never opened.
    files_dataless: int = 0
    #: Files whose tags could not be read at all (tag reader absent, or the file
    #: is not parseable). They are still imported, titled from the filename.
    files_without_tags: int = 0
    #: Tracks whose file tags carried a BPM / a key that was recorded.
    tracks_with_tag_bpm: int = 0
    tracks_with_tag_key: int = 0
    #: Allowlisted files that failed the playable-audio probe. Never imported.
    files_rejected_unplayable: int = 0
    #: Stem-separation outputs (LIBM-170), e.g. ``vocals/Song - vocals.mp3``.
    #: Never imported: they carry the song's title and play as an acapella.
    files_skipped_stem_output: int = 0
    tracks_inserted: int = 0
    tracks_updated: int = 0
    tracks_unchanged: int = 0
    tracks_skipped: int = 0
    #: Files that ARE a track the user removed, by id or by audio identity
    #: (LIBM-140). Left removed; restore one with ``undelete``.
    tracks_skipped_deleted: int = 0
    #: Files the user picked to add again that ARE a track they removed
    #: (``restore_removed``): restored, not skipped (LIBM-141).
    tracks_restored: int = 0
    tier_counts: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"isrc": 0, "fingerprint": 0, "inferred": 0}
    )
    #: Always equal to the tracks written. A folder import analyses nothing,
    #: so every track it creates has no bpm, no key and no beatgrid, and the
    #: number is reported rather than left for someone to discover.
    tracks_without_analysis: int = 0
    duration_s: float = 0.0
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def collect_audio(
    roots: Iterable[Path],
) -> tuple[list[audio_files.AudioFile], list[str], int]:
    """Walk the READABLE roots. Returns ``(files, denied_roots, dataless)``.

    Denied roots are excluded from the walk rather than walked and silently
    yielding nothing, so the caller can report them instead of reporting a
    smaller library.
    """
    probes = fs_access.probe_all(list(roots))
    denied = fs_access.denied_roots(probes)
    readable = [
        Path(probe.path) for probe in probes if probe.readable
    ]

    found: list[audio_files.AudioFile] = []
    dataless = 0
    for entry in audio_files.scan_music_files(readable):
        try:
            stat = entry.path.stat()
        except OSError:
            continue
        if fs_residency.is_dataless_stub(stat):
            # Counted, never opened: reading one asks iCloud to fetch it.
            dataless += 1
            continue
        found.append(entry)
    return found, denied, dataless


def ingest_folder(
    writer: StateWriter,
    roots: Iterable[Path],
    *,
    dry_run: bool = True,
    limit: int | None = None,
    clock: _ClockFn | None = None,
    on_progress: ProgressFn | None = None,
    allow_mass_missing: bool = False,
    restore_removed: bool = False,
) -> FolderIngestReport:
    """Ingest every audio file under ``roots`` into the state DB.

    ``restore_removed`` is for a caller adding files the user picked by hand
    (a folder drop, the import modal), never for a rescan of a library root.
    Those files were chosen to be added, so one the user removed earlier
    comes back instead of being skipped (LIBM-141). Without it, a removed
    track stays removed (LIBM-140).
    """
    start = time.perf_counter()
    root_list = [Path(root).expanduser() for root in roots]
    now_fn = clock or (lambda: _dt.datetime.now(_dt.UTC))
    report = FolderIngestReport(
        roots=[str(root) for root in root_list], dry_run=dry_run
    )

    files, denied, dataless = collect_audio(root_list)
    report.unreadable_roots = denied
    report.files_dataless = dataless
    report.files_seen = len(files)

    conn = writer.raw_conn
    _guard_folder_scan(conn, root_list, files, allow_mass_missing)
    assert_no_path_collisions(str(entry.path) for entry in files)
    if limit is not None:
        files = files[:limit]
    original_bus = writer.bus
    if dry_run:
        writer.bus = _DryRunSilentBus()

    try:
        with state_db.write_unit(conn, "setup_ingest_folder", keep=not dry_run):
            _write_tracks(writer, files, report, on_progress, restore_removed=restore_removed)
            writer.register_adapter(
                ADAPTER_ID,
                last_run_at=now_fn().isoformat(),
                last_ok=True,
                notes=(
                    f"roots={len(root_list)} files={report.files_seen} "
                    f"dataless={report.files_dataless} "
                    f"unreadable_roots={len(report.unreadable_roots)} "
                    f"dry_run={dry_run}"
                ),
            )
    finally:
        writer.bus = original_bus
        report.duration_s = round(time.perf_counter() - start, 3)

    return report


def _guard_folder_scan(
    conn: Any,
    roots: list[Path],
    files: list[audio_files.AudioFile],
    allow_mass_missing: bool,
) -> None:
    """LIBM-41: refuse an empty (or mass-dropped) walk of a populated root."""
    prior_paths = [
        row[0]
        for row in conn.execute(
            "SELECT file_path FROM tracks "
            "WHERE deleted_at IS NULL AND file_path IS NOT NULL"
        )
    ]
    guard_roots(
        roots,
        [str(entry.path) for entry in files],
        prior_paths,
        allow_mass_missing=allow_mass_missing,
    )



#: Tiers a stronger source than a file walk wrote. The folder writer keeps
#: their identity and their metadata.
_STRONG_TIERS = frozenset({"isrc", "fingerprint"})


def _live_rows_by_collision_key(conn: Any) -> dict[str, list[tuple[Any, ...]]]:
    """Every live row at each canonical path, strongest identity first.

    Exact ``file_path`` equality misses an NFC/NFD or case spelling of the
    same file, and the rescan would then insert a second id beside the old
    row. The strongest identity (ISRC, then fingerprint, then inferred) is
    the row whose tags are refreshed. Every other live row at that path stays:
    a rewrite still updates its audio hash and drops its analysis, because
    those rows point at the same bytes.
    """
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in conn.execute(
        "SELECT stable_id, stable_id_tier, isrc, audio_hash, file_path, "
        "title, artists_json, album, duration_ms, content_hash FROM tracks "
        "WHERE deleted_at IS NULL AND file_path IS NOT NULL"
    ):
        grouped.setdefault(path_collision_key(str(row[4])), []).append(tuple(row))
    rank = {"isrc": 0, "fingerprint": 1}
    for group in grouped.values():
        group.sort(key=lambda item: (rank.get(str(item[1]), 2), str(item[0])))
    return grouped


def _file_missing_strong_rows_by_collision_key(conn: Any) -> dict[str, tuple[Any, ...]]:
    """The strongest ISRC or fingerprint row a scan tombstoned at each canonical path.

    A rescan tombstones every live row whose file is gone, an ISRC or
    fingerprint row included. A tier-3 id is minted from path and mtime, so
    it can never equal that row's id: without this lookup a returning file
    would get a second, inferred row while the stronger identity stayed
    removed. Only ``DELETED_FILE_MISSING`` rows are read, the one tombstone
    ``upsert_track`` lifts. A row the user removed is never brought back here.
    """
    rows = conn.execute(
        "SELECT stable_id, stable_id_tier, isrc, audio_hash, file_path, "
        "title, artists_json, album, duration_ms, content_hash FROM tracks "
        "WHERE deleted_at IS NOT NULL AND deleted_reason = ? "
        "AND file_path IS NOT NULL AND stable_id_tier IN ('isrc', 'fingerprint') "
        "ORDER BY CASE stable_id_tier WHEN 'isrc' THEN 0 ELSE 1 END, stable_id",
        (DELETED_FILE_MISSING,),
    ).fetchall()
    strongest: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        strongest.setdefault(path_collision_key(str(row[4])), tuple(row))
    return strongest


def _rows_to_write_onto(
    live_by_key: dict[str, list[tuple[Any, ...]]],
    returned_by_key: dict[str, tuple[Any, ...]],
    key: str,
) -> list[tuple[Any, ...]]:
    """The rows a file at ``key`` is written onto, strongest first.

    Live rows win. With none, the file is back at a path whose ISRC or
    fingerprint row a scan tombstoned, and that row is the one written:
    ``upsert_track`` lifts a file-missing tombstone, and the caller's hash
    check still drops analysis that describes other bytes. The row is
    popped so a second spelling of the path in the same batch finds it live.
    """
    rows = list(live_by_key.get(key, []))
    if not rows and key in returned_by_key:
        rows = [returned_by_key.pop(key)]
    return rows


def _write_tracks(
    writer: StateWriter,
    files: list[audio_files.AudioFile],
    report: FolderIngestReport,
    on_progress: ProgressFn | None,
    *,
    restore_removed: bool = False,
) -> None:
    """One row per file, with a tier-3 collision guard.

    A tier-3 id is ``sha1(abs_path|mtime)``, so two distinct files cannot
    collide in practice -- but the guard is kept because the rekordbox
    adapter needs it and a folder importer that quietly overwrote a row
    would be far harder to notice than one that reports a skip.
    """
    total = len(files)
    seen: set[str] = set()
    live_by_key = _live_rows_by_collision_key(writer.raw_conn)
    returned_by_key = _file_missing_strong_rows_by_collision_key(writer.raw_conn)
    for index, entry in enumerate(files, start=1):
        identified = _identify(entry, report)
        if identified is None:
            continue
        stable_id, tier, duration_ms, metadata = identified
        # Same canonical path (NFC + casefold), new mtime: tier-3 would mint
        # a second id. Keep the row the library already has, including its
        # tier and ISRC, even when the walked spelling differs. A fresh
        # inferred id must not relabel an ISRC row or clear its ISRC.
        # NATIVE-24 still does not merge rows that already exist. When more
        # than one live row shares the canonical path, the strongest identity
        # receives the tag refresh. The others keep their ids and, when the
        # bytes changed, receive the new hash below.
        key = path_collision_key(str(entry.path))
        rows = _rows_to_write_onto(live_by_key, returned_by_key, key)
        existing = rows[0] if rows else None
        kept_isrc: str | None = None
        prior_hash: str | None = None
        if existing is not None:
            stable_id = str(existing[0])
            tier = str(existing[1])
            kept_isrc = existing[2]
            prior_hash = existing[3]
            rows[0] = (
                stable_id,
                tier,
                kept_isrc,
                prior_hash,
                str(entry.path),
                *existing[5:],
            )
        else:
            # Remember the id this batch just minted so a second spelling of
            # the same path is skipped instead of inserted beside it.
            rows = [(
                stable_id,
                tier,
                kept_isrc,
                prior_hash,
                str(entry.path),
                None,
                "[]",
                None,
                duration_ms,
                None,
            )]
        live_by_key[key] = rows
        if stable_id in seen:
            report.tracks_skipped += 1
            continue
        seen.add(stable_id)

        audio_hash = hashing.sha256_audio_payload(entry.path)
        removed = deleted_tracks.find_deleted_match(
            writer.raw_conn,
            stable_id=stable_id,
            content_hash=None,
            audio_hash=audio_hash,
        )
        if removed is not None and not restore_removed:
            report.tracks_skipped_deleted += 1
            continue
        if removed is not None:
            report.tracks_restored += 1
            if removed == stable_id:
                # Same row: the explicit restore, then the upsert refreshes it.
                writer.undelete_track(stable_id)
            # Same audio under another id (a new path): the file is written as
            # the new row below and the old tombstone is left as it is.

        title, artists, album, row_duration_ms = _metadata_to_write(
            existing, metadata, entry, duration_ms
        )
        changed = writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier=tier,
            title=title,
            artists=artists,
            album=album,
            isrc=kept_isrc,
            duration_ms=row_duration_ms,
            file_path=str(entry.path),
            content_hash=existing[9] if existing is not None else None,
            audio_hash=audio_hash,
        )
        if changed:
            report.tracks_inserted += 1
        else:
            report.tracks_unchanged += 1
        # A missing prior hash is not proof the stored analysis describes
        # these bytes. Drop it on a refresh of an existing row. A new row
        # has no analysis to drop.
        if existing is not None and audio_hash:
            if prior_hash != audio_hash:
                _drop_analysis_for_replaced_bytes(writer.raw_conn, stable_id)
            _refresh_other_live_rows(writer, rows[1:], audio_hash)
        report.tier_counts[tier] = report.tier_counts.get(tier, 0) + 1
        write_file_tag_metadata(writer, stable_id, metadata, report)
        # A folder import analyses nothing. A tag BPM / key is the file's own
        # claim, recorded with "inferred" provenance, not an analysis result.
        report.tracks_without_analysis += 1

        if on_progress is not None:
            on_progress(index, total, f"{index} of {total}: {entry.path.name}")


# Literal DELETE statements. These tables are machine-local analysis, not
# synced entity tables. An f-string target is what the soft-delete guard
# rejects, because it cannot see that the name is outside the synced set.
_ANALYSIS_DROP_SQL: tuple[tuple[str, str], ...] = (
    ("analysis", "DELETE FROM analysis WHERE stable_id = ?"),
    ("analysis_canonical", "DELETE FROM analysis_canonical WHERE stable_id = ?"),
    ("analysis_projection", "DELETE FROM analysis_projection WHERE stable_id = ?"),
    ("analysis_stale", "DELETE FROM analysis_stale WHERE stable_id = ?"),
)


def _refresh_other_live_rows(
    writer: StateWriter, rows: list[tuple[Any, ...]], audio_hash: str
) -> None:
    """Give every other live row at this path the rewritten bytes' hash.

    The strongest identity already took the tag refresh. These rows are not
    merged and not deleted. A hash that still describes the previous bytes
    is replaced, and that row's analysis is dropped, or a deck keeps serving
    the old BPM from the duplicate.
    """
    for row in rows:
        if row[3] == audio_hash:
            continue
        writer.upsert_track(
            stable_id=str(row[0]),
            stable_id_tier=str(row[1]),
            title=row[5],
            artists=_stored_artists(row),
            album=row[7],
            isrc=row[2],
            duration_ms=row[8],
            file_path=str(row[4]),
            content_hash=row[9],
            audio_hash=audio_hash,
        )
        _drop_analysis_for_replaced_bytes(writer.raw_conn, str(row[0]))


def _stored_artists(row: tuple[Any, ...]) -> list[str]:
    """The artist list a stored row carries. A value that is not a JSON list raises."""
    parsed = json.loads(row[6]) if row[6] else []
    if not isinstance(parsed, list):
        raise ValueError(f"track {row[0]} artists_json must be a JSON list")
    return [str(name) for name in parsed]


def _metadata_to_write(
    existing: tuple[Any, ...] | None,
    metadata: audio_files.AudioMetadata | None,
    entry: audio_files.AudioFile,
    duration_ms: int | None,
) -> tuple[Any, list[str], Any, Any]:
    """Title, artists, album and duration for the row a file is written onto.

    An ISRC or fingerprint row was written by a stronger source than a file
    walk. When the folder writer adopts one, only its path and hash follow
    the file: the stored title, artists, album and duration stay, or an
    untagged file would retitle it from its filename on every rescan. A new
    row and an inferred row take the file's tags.
    """
    if existing is not None and str(existing[1]) in _STRONG_TIERS:
        return existing[5], _stored_artists(existing), existing[7], existing[8]
    album = metadata.album if metadata is not None else None
    return _title(metadata, entry), _artists(metadata), album, duration_ms


def _drop_analysis_for_replaced_bytes(conn: Any, stable_id: str) -> None:
    """Forget analysis of the previous bytes so the drain runs the new ones.

    The tables are created by the analysis store, not by every state file, so
    a missing table is a library that has never analysed anything.
    """
    for table, sql in _ANALYSIS_DROP_SQL:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if present is not None:
            conn.execute(sql, (stable_id,))


def _identify(
    entry: audio_files.AudioFile, report: FolderIngestReport
) -> tuple[str, str, int | None, audio_files.AudioMetadata | None] | None:
    """Read the tags and mint the id, or count the file as skipped."""
    if is_stem_output(entry.path):
        report.files_skipped_stem_output += 1
        return None
    try:
        audio_playable.probe_playable_audio(entry.path)
    except UnplayableAudioError:
        report.files_rejected_unplayable += 1
        return None
    metadata = audio_files.read_metadata(entry.path)
    if not _has_file_tags(metadata):
        report.files_without_tags += 1
    duration_ms = (
        int(metadata.duration_s * 1000)
        if metadata is not None and metadata.duration_s
        else None
    )
    try:
        stable_id, tier = state_ids.stable_id(
            isrc=None,
            fingerprint=None,
            duration_ms=duration_ms,
            size_bytes=entry.size_bytes,
            abs_path=str(entry.path),
            mtime=entry.mtime,
        )
    except ValueError:
        report.tracks_skipped += 1
        return None
    return stable_id, tier, duration_ms, metadata


def _has_file_tags(metadata: audio_files.AudioMetadata | None) -> bool:
    """True when the tag reader (or a stub) supplied a user-facing tag, not just duration."""
    if metadata is None:
        return False
    return any(
        bool(value)
        for value in (
            metadata.title,
            metadata.artist,
            metadata.album,
            metadata.genre,
            metadata.comment,
        )
    )


def _title(
    metadata: audio_files.AudioMetadata | None, entry: audio_files.AudioFile
) -> str:
    """A tag title, else the filename. Never an empty row nobody can find."""
    tagged = metadata.title if metadata is not None else None
    return tagged or entry.path.stem


def _artists(metadata: audio_files.AudioMetadata | None) -> list[str]:
    artist = metadata.artist if metadata is not None else None
    return [artist] if artist else []


def write_file_tag_metadata(
    writer: StateWriter,
    stable_id: str,
    metadata: audio_files.AudioMetadata | None,
    report: FolderIngestReport,
) -> None:
    """Persist the file's own tags that unmapped browser rows display.

    genre / comments follow the tag on every import. bpm / key are written
    only when the track has NO value for that field yet: those fields are also
    written by analysis, rekordbox and webui edits, and a rescan of the same
    folder must never replace one of those with the file's tag.
    """
    if metadata is None:
        return
    modified_at = _dt.datetime.now(_dt.UTC).isoformat()
    for field_name, value in (
        ("genre", metadata.genre),
        ("comments", metadata.comment),
    ):
        if value is None:
            continue
        writer.set_field(
            stable_id, field_name, value, source="inferred",
            confidence=0.7, modified_at=modified_at,
        )
    analysed: tuple[tuple[str, float | str | None], ...] = (
        ("bpm", metadata.bpm),
        ("key", metadata.key),
    )
    for field_name, tag_value in analysed:
        if tag_value is None:
            continue
        if state_provenance.read_field(writer.raw_conn, stable_id, field_name) is not None:
            continue
        writer.set_field(
            stable_id, field_name, tag_value, source="inferred",
            confidence=0.7, modified_at=modified_at,
        )
        if field_name == "bpm":
            report.tracks_with_tag_bpm += 1
        elif field_name == "key":
            report.tracks_with_tag_key += 1


# ----- CLI ---------------------------------------------------------------
def _print_summary(report: FolderIngestReport) -> None:
    print(f"Folder ingest ({'dry-run' if report.dry_run else 'write'})")
    print(f"  roots:              {', '.join(report.roots)}")
    for root in report.unreadable_roots:
        print(f"  UNREADABLE ROOT:    {root}")
    print(f"  duration:           {report.duration_s:.3f}s")
    print(f"  audio files seen:   {report.files_seen}")
    print(f"  icloud placeholders skipped: {report.files_dataless}")
    print(f"  files without tags: {report.files_without_tags}")
    print(f"  tracks with tag bpm: {report.tracks_with_tag_bpm}")
    print(f"  tracks with tag key: {report.tracks_with_tag_key}")
    print(f"  files rejected (unplayable): {report.files_rejected_unplayable}")
    print(f"  skipped: stem separation outputs: {report.files_skipped_stem_output}")
    print(f"  tracks inserted:    {report.tracks_inserted}")
    print(f"  tracks unchanged:   {report.tracks_unchanged}")
    print(f"  tracks skipped:     {report.tracks_skipped}")
    print(f"  skipped: deleted by user: {report.tracks_skipped_deleted}")
    print(f"  restored: picked again by user: {report.tracks_restored}")
    print(f"  tracks with NO analysis: {report.tracks_without_analysis}")
    for tier, count in report.tier_counts.items():
        print(f"  tier {tier:<12} {count}")
    if report.unreadable_roots:
        print(f"\n{fs_access.GRANT_INSTRUCTIONS}", file=sys.stderr)


def run_cli(args: argparse.Namespace) -> int:
    """Entry point called from ``apps.shared.state.cli ingest-folder``."""
    state_path = Path(args.db) if args.db else state_paths.STATE_DB
    roots = [Path(root).expanduser() for root in args.root]
    missing = [str(root) for root in roots if not root.exists()]
    if missing:
        print(f"error: no such folder: {', '.join(missing)}", file=sys.stderr)
        return 1

    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="ingest-folder")
    try:
        report = ingest_folder(
            writer,
            roots,
            dry_run=not args.write,
            limit=args.limit,
            allow_mass_missing=bool(
                getattr(args, "allow_mass_missing", False)
            ),
        )
        _print_summary(report)
    except MassMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PathCollisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        writer.close()
        conn.close()
    return 0


__all__ = [
    "ADAPTER_ID",
    "FolderIngestReport",
    "ProgressFn",
    "collect_audio",
    "ingest_folder",
    "run_cli",
    "write_file_tag_metadata",
]
