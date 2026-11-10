"""Another DJ app's library -> state.db ingest (DJIMPORT-01..04).

The Serato and Traktor adapters have read their own formats into an
:class:`~apps.open_dj.OpenDjLibrary` since phase 16, but only the
``open-dj-tool`` CLI used the result, so a DJ coming from either app reached an
Open DJ library with none of their cues, keys or crates (CUES-01 "Not yet").
This module is the missing step: it writes any ``OpenDjLibrary`` into the
state DB, so every reader that produces one (Serato and Traktor today, more
later) shares one ingest.

The rules, each one tested:

* **Same file, same track.** An entry whose file is already a library track
  (from rekordbox, a folder scan or an earlier import) lands on THAT row. A
  second app never duplicates a track the library already has. Paths are
  compared in Unicode NFC, because macOS hands out NFD names and the apps
  store whichever they saw.
* **Fill, never fight.** BPM, key, rating, comments and genre are written only where
  the track has no value yet, or where the value already came from this same
  app (a re-import refreshes its own copy). A value from rekordbox, Mixed In
  Key, analysis or an edit in Open DJ is never replaced by an import.
* **Cues follow the cue store's ownership rule.** They go through
  :func:`cue_store.import_vendor_cues`, which copies them only when the track
  has no cue set or its set came from this same app.
* **A shared label never refreshes.** A rekordbox XML import writes with
  ``source="rekordbox"``, the same label as the master.db ingest, so "this
  same app" cannot be told apart from rekordbox itself. It only fills empty
  values and empty cue sets, and never refreshes anything.
* **Deletes stay deleted** (LIBM-140): a track the user removed is not brought
  back by an import, and is left out of every imported playlist.
* **Dry-run by default**, inside one discarded
  :meth:`StateWriter.transaction` (``commit=False``) so queued bus events
  are dropped, exactly like the rekordbox and folder ingests.

What it does not carry yet, by design and reported rather than hidden:
beatgrids (the state ``beatgrid`` field has no importer contract yet), track
colors and play counts (no state field holds them), and cue colors (rekordbox's
cue color table is unpublished, so an RGB cannot be mapped onto an index
honestly). A rekordbox XML export's label, remixer, composer, grouping, year,
date added, mix, bit rate, sample rate and size are read but not stored (no state
field holds them yet); they are counted in the summary as "carried, not stored".

Run it as ``python -m apps.adapters.library_import serato|traktor|rekordbox-xml|djay|virtualdj|mixxx
[--source P] [--write]``. VirtualDJ's BPM, key, rating, comments, genre and cues are counted
as read, not stored, until DJIMPORT-10056b widens ``track_fields.source``.
``serato-history [--source <_Serato_>] [--write]`` appends Serato's played history to the play
log (DJIMPORT-10066, :func:`import_serato_history`); ``traktor-history [--source <Traktor dir>]
[--zone IANA] [--write]`` appends Traktor's session history the same way (DJIMPORT-10069,
:func:`import_traktor_history`). It lives in ``apps.adapters`` rather than ``apps.shared.state``
because it calls the readers, and ``apps.shared`` imports nothing else from
``apps`` (the ``shared-is-the-stable-core`` contract in ``.importlinter``).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import os
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apps.adapters.serato import history as serato_history
from apps.open_dj import AdapterReport, CuePoint, OpenDjLibrary, Playlist, Track
from apps.shared import hashing
from apps.shared.state import cue_store, deleted_tracks, play_log
from apps.shared.state import db as state_db
from apps.shared.state import ids as state_ids
from apps.shared.state import paths as state_paths
from apps.shared.state import provenance as state_provenance
from apps.shared.state.sync_stamp import canonical_from
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_common import compute_playlist_id

Vendor = Literal["serato", "traktor", "rekordbox-xml", "djay", "virtualdj", "mixxx"]
VENDORS: tuple[Vendor, ...] = ("serato", "traktor", "rekordbox-xml", "djay", "virtualdj", "mixxx")
#: Imports that append to the play log rather than the library.
HISTORY_COMMANDS: tuple[str, ...] = ("serato-history", "traktor-history")
#: Kept for callers of the Traktor-only name; the same commands.
PLAY_SOURCES: tuple[str, ...] = HISTORY_COMMANDS

#: The ``track_fields.source`` each import writes. A rekordbox XML export is
#: labeled with the format it is in, ``rekordbox``, whichever app wrote it.
#: Mixxx uses ``open-dj-tool`` because the state source CHECK has no ``mixxx``;
#: the import is fill-only, like rekordbox-xml on a shared label.
_SOURCE: dict[str, str] = {
    "serato": "serato",
    "traktor": "traktor",
    "rekordbox-xml": "rekordbox",
    "djay": "djay",
    "mixxx": "open-dj-tool",
}

#: Vendors whose field/cue provenance label is not in ``track_fields`` yet (DJIMPORT-10056b).
#: Fields and cues are counted, not written, and never under another app's label.
_SOURCE_PENDING: frozenset[str] = frozenset({"virtualdj"})

#: Maps a path as the app stored it to a local absolute path.
PathResolver = Callable[[str], str]
_ClockFn = Callable[[], _dt.datetime]

#: Fields an import fills. Each maps to a ``track_fields`` name.
_FILLED_FIELDS = ("bpm", "key", "rating", "comments", "genre")
#: Reader extensions with no state field yet: counted in the summary, not written.
_CARRIED_EXTENSIONS = (
    ("label", "x_label"),
    ("remixer", "x_remixer"),
    ("composer", "x_composer"),
    ("grouping", "x_grouping"),
    ("year", "x_year"),
    ("date_added", "x_date_added"),
    ("mix", "x_mix"),
    ("bitrate", "x_bitrate"),
    ("sample_rate", "x_sample_rate"),
    ("size", "x_size"),
)


@dataclasses.dataclass
class VendorIngestReport:
    """What the import found and wrote. Denominators are named."""

    vendor: str
    source: str
    #: Entries in the app's library that reached the import. THE denominator
    #: for the counts below; the source total adds ``entries_dropped_by_reader``.
    entries_seen: int = 0
    #: Whole entries the app's reader dropped before the import saw them
    #: (no file location, or streaming). Counted in the source total.
    entries_dropped_by_reader: int = 0
    #: Entries whose file was already a library track (merged onto that row).
    tracks_matched: int = 0
    #: Entries that became new library tracks.
    tracks_inserted: int = 0
    #: Entries pointing at a file that is not on this machine. Still imported,
    #: as rekordbox ingest does, so a relink can find them later.
    files_missing: int = 0
    #: Entries that ARE a track the user removed (LIBM-140). Left removed.
    tracks_skipped_deleted: int = 0
    #: Entries with no usable path, or a second entry for the same file.
    tracks_skipped: int = 0
    #: Fields written, per field name.
    fields_written: dict[str, int] = dataclasses.field(default_factory=dict)
    #: Fields left alone because another source already owns the value.
    fields_kept: dict[str, int] = dataclasses.field(default_factory=dict)
    cue_sets_written: int = 0
    #: Cues the store has no kind for (Traktor grid, load and fade markers).
    cues_skipped: int = 0
    playlists_written: int = 0
    playlist_entries_unresolved: int = 0
    duration_s: float = 0.0
    dry_run: bool = True
    #: What the app's reader dropped or changed before the import saw it,
    #: counted per kind (see :class:`VendorRead`).
    reader_notes: dict[str, int] = dataclasses.field(default_factory=dict)
    #: Values the reader carried that no state field stores yet, per name (label, year, ...).
    extensions_carried: dict[str, int] = dataclasses.field(default_factory=dict)
    #: Values read but not written because the vendor has no provenance label in track_fields yet.
    fields_not_stored: dict[str, int] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------- path resolution -------------------------------------------------


def nfc(path: str) -> str:
    """The comparison form of a path: Unicode NFC, forward slashes kept."""
    return unicodedata.normalize("NFC", path)


def serato_path_resolver(
    serato_dir: Path, *, is_mount: Callable[[Path], bool] = os.path.ismount
) -> PathResolver:
    """Serato stores ``pfil`` relative to the root of the drive ``_Serato_``
    lives on: ``Users/dj/Music/a.mp3`` for the system drive, ``Music/a.mp3``
    for an external one."""
    serato_dir = Path(serato_dir).expanduser()
    root = serato_volume_root(serato_dir, is_mount=is_mount)

    def resolve(stored: str) -> str:
        raw = stored.replace("\\", "/")
        if Path(raw).is_absolute() or _is_windows_drive(raw):
            return raw
        return str(root / raw)

    return resolve


def serato_volume_root(
    serato_dir: Path, *, is_mount: Callable[[Path], bool] = os.path.ismount
) -> Path:
    """The drive root Serato's relative paths hang from: the nearest mount
    point at or above the folder holding ``_Serato_``.

    ``/Volumes/Stick/_Serato_`` and ``/Volumes/Stick/Music/_Serato_`` both
    mean ``/Volumes/Stick``; ``~/Music/_Serato_`` walks up to the system
    drive. With no mount found, the path's own anchor is the root.
    """
    for candidate in (serato_dir.parent, *serato_dir.parent.parents):
        if is_mount(candidate):
            return candidate
    return Path(serato_dir.anchor or "/")


def traktor_path_resolver(*, volumes_root: Path = Path("/Volumes")) -> PathResolver:
    """Traktor stores ``VOLUME`` + ``DIR`` + ``FILE``, so a decoded location
    reads ``Macintosh HD/Users/dj/a.mp3`` on macOS and ``C:/Music/a.mp3`` on
    Windows. The named volume, mounted under ``/Volumes``, is tried first, so
    a file on an external drive is never swapped for a same-named one on the
    system drive; then the system volume ``/``. A named volume that is the
    system drive itself (macOS links ``/Volumes/Macintosh HD`` to ``/``)
    reads as ``/``, the spelling every other source uses for that file. With
    neither on disk, the system-volume reading is kept so a relink can find
    it."""

    def resolve(stored: str) -> str:
        raw = stored.replace("\\", "/")
        if raw.startswith("/") or not raw:
            return raw
        volume, sep, rest = raw.partition("/")
        if not sep:
            return raw
        if _is_windows_drive(volume):
            return f"{volume}/{rest}"
        system = f"/{rest}"
        volume_dir = volumes_root / volume
        if os.path.realpath(volume_dir) == os.path.realpath("/"):
            return system
        for candidate in (str(volume_dir / rest), system):
            if os.path.exists(candidate):
                return candidate
        return system

    return resolve


def rekordbox_xml_path_resolver(stored: str) -> str:
    """Spell a decoded XML location as this platform stores a path.

    Folder ingests persist ``str(Path)``, so a Windows ``C:/Music/a.mp3``
    becomes ``C:\\Music\\a.mp3`` here; on macOS and Linux the spelling is
    unchanged. Without this, an XML import never matches a track already in
    the library and inserts a duplicate.
    """
    return str(Path(stored))


def _is_windows_drive(text: str) -> bool:
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


# ---------- cue mapping -----------------------------------------------------


def store_cues(cues: Iterable[CuePoint], report: VendorIngestReport) -> list[dict[str, Any]]:
    """Map open-dj cues onto the cue store's kinds.

    A hot cue keeps its pad (slot A-H from its index); a hot cue whose pad is
    past H, a memory cue, and a loop off the pads become slot-less memory cues
    and loops. Grid, load and fade markers have no kind in the store and are
    counted, not invented.
    """
    out: list[dict[str, Any]] = []
    for cue in cues:
        if cue.position_ms < 0:
            report.cues_skipped += 1
            continue
        out_ms = (
            cue.position_ms + cue.length_ms
            if cue.length_ms is not None and cue.length_ms > 0
            else None
        )
        if cue.type == "hot" and 0 <= cue.index < len(cue_store.HOT_CUE_SLOTS):
            kind, slot = "hot_cue", cue_store.HOT_CUE_SLOTS[cue.index]
        elif cue.type in ("hot", "memory"):
            kind, slot, out_ms = "memory", None, None
        elif cue.type == "loop" and out_ms is not None:
            kind, slot = "loop", None
        else:
            report.cues_skipped += 1
            continue
        out.append(
            {
                "kind": kind,
                "slot": slot,
                "in_ms": cue.position_ms,
                "out_ms": out_ms if kind != "memory" else None,
                "active_loop": False,
                "beat_loop_size": None,
                "color_table_index": None,
                "comment": cue.name or None,
            }
        )
    return out


# ---------- ingest ----------------------------------------------------------


def ingest_library(  # noqa: PLR0913 - one keyword per import option, all optional but vendor and source.
    writer: StateWriter,
    library: OpenDjLibrary,
    *,
    vendor: Vendor,
    source: str,
    resolve_path: PathResolver,
    dry_run: bool = True,
    modified_at: str | None = None,
    clock: _ClockFn | None = None,
    write_cues: bool = True,
) -> VendorIngestReport:
    """Write ``library`` (read from ``vendor``'s files at ``source``) into state.db.

    ``modified_at`` stamps every field and cue set written: when the app last
    saved its library (:func:`source_modified_at`). A stamp that moved on
    every run would rewrite every unchanged value as new history, so callers
    pass the source's own time, and only a test may leave it to the clock.

    ``write_cues=False`` leaves every track's cue set alone. It is for a reader
    that did not read cues, where an empty ``cues`` means unknown, not cleared.
    """
    if vendor not in VENDORS:
        raise ValueError(f"unknown vendor {vendor!r}; expected one of {VENDORS}")
    field_source = _SOURCE.get(vendor)
    fill_only = field_source is not None and field_source != vendor
    pending_fields = vendor in _SOURCE_PENDING
    start = time.perf_counter()
    now_fn = clock or (lambda: _dt.datetime.now(_dt.UTC))
    report = VendorIngestReport(vendor=vendor, source=source, dry_run=dry_run)
    conn = writer.raw_conn
    try:
        with writer._join_or_begin(commit=not dry_run):
            by_path = live_tracks_by_path(conn)
            stamp = modified_at or now_fn().astimezone(_dt.UTC).isoformat()
            entry_to_sid: dict[str, str] = {}
            deleted_entries: set[str] = set()
            seen: set[str] = set()
            for track in library.tracks:
                report.entries_seen += 1
                sid = _ingest_track(writer, track, vendor, resolve_path, by_path, report)
                if sid is None:
                    deleted_entries.add(track.track_id)
                    continue
                if sid in seen:
                    report.tracks_skipped += 1
                    entry_to_sid[track.track_id] = sid
                    continue
                seen.add(sid)
                entry_to_sid[track.track_id] = sid
                if pending_fields:
                    _count_not_stored(track, report)
                else:
                    assert field_source is not None
                    _write_fields(writer, sid, track, field_source, fill_only, stamp, report, write_cues)
                _count_carried(track, report)
            _write_playlists(writer, library.playlists, vendor, entry_to_sid, deleted_entries, report)
            writer.register_adapter(
                vendor,
                last_run_at=now_fn().isoformat(),
                last_ok=True,
                notes=(
                    f"entries={report.entries_seen} matched={report.tracks_matched} "
                    f"inserted={report.tracks_inserted} missing={report.files_missing} "
                    f"dry_run={dry_run}"
                ),
            )
    finally:
        report.duration_s = round(time.perf_counter() - start, 3)
    return report


def live_tracks_by_path(conn: Any) -> dict[str, str]:
    """Live library tracks keyed by NFC file path."""
    rows = conn.execute(
        "SELECT stable_id, file_path FROM tracks "
        "WHERE deleted_at IS NULL AND file_path IS NOT NULL AND file_path != '' "
        "ORDER BY stable_id"
    ).fetchall()
    out: dict[str, str] = {}
    for stable_id, file_path in rows:
        out.setdefault(nfc(str(file_path)), str(stable_id))
    return out


def _ingest_track(
    writer: StateWriter,
    track: Track,
    vendor: str,
    resolve_path: PathResolver,
    by_path: dict[str, str],
    report: VendorIngestReport,
) -> str | None:
    """Return the entry's stable_id, or None when it is a removed track.

    A path-less entry also returns None after counting it as skipped.
    """
    if not track.file_path:
        report.tracks_skipped += 1
        return None
    path = resolve_path(track.file_path)
    key = nfc(path)
    existing = by_path.get(key)
    if existing is not None:
        report.tracks_matched += 1
        writer.set_vendor_id(existing, vendor, track.track_id)
        return existing

    exists = os.path.isfile(path)
    if not exists:
        report.files_missing += 1
    mtime = 0.0
    size: int | None = None
    if exists:
        stat = os.stat(path)
        mtime, size = stat.st_mtime, stat.st_size
    try:
        sid, tier = state_ids.stable_id(
            isrc=track.isrc,
            fingerprint=None,
            duration_ms=track.duration_ms,
            size_bytes=size,
            abs_path=path,
            mtime=mtime,
        )
    except ValueError:
        report.tracks_skipped += 1
        return None
    audio_hash = _audio_hash(path) if exists else None
    if (
        deleted_tracks.find_deleted_match(
            writer.raw_conn, stable_id=sid, content_hash=None, audio_hash=audio_hash
        )
        is not None
    ):
        report.tracks_skipped_deleted += 1
        return None
    writer.upsert_track(
        stable_id=sid,
        stable_id_tier=tier,
        title=track.title or PurePosixPath(path.replace("\\", "/")).stem or None,
        artists=list(track.artists),
        album=track.album or None,
        isrc=track.isrc,
        duration_ms=track.duration_ms,
        file_path=path,
        content_hash=None,
        audio_hash=audio_hash,
    )
    writer.set_vendor_id(sid, vendor, track.track_id)
    by_path[key] = sid
    report.tracks_inserted += 1
    return sid


def _audio_hash(path: str) -> str:
    """The payload hash of a file known to exist. A read failure stops the
    import naming the file: a hashless track would slip past the removed-track
    check and could bring back a track the user deleted."""
    try:
        return hashing.sha256_audio_payload(path)
    except OSError as exc:
        raise OSError(f"{path}: cannot read audio to hash it: {exc}") from exc


def _field_values(track: Track) -> Iterator[tuple[str, Any]]:
    if track.bpm is not None and track.bpm > 0:
        yield "bpm", round(float(track.bpm), 2)
    if track.key_camelot:
        yield "key", track.key_camelot
    if track.rating is not None:
        yield "rating", int(track.rating)
    comment = track.extensions.get("x_comment")
    if isinstance(comment, str) and comment.strip():
        yield "comments", comment.strip()
    genre = track.extensions.get("x_genre")
    if isinstance(genre, str) and genre.strip():
        yield "genre", genre.strip()


def _count_not_stored(track: Track, report: VendorIngestReport) -> None:
    for name, _ in _field_values(track):
        report.fields_not_stored[name] = report.fields_not_stored.get(name, 0) + 1
    if track.cues:
        report.fields_not_stored["cues"] = report.fields_not_stored.get("cues", 0) + 1
    if track.beats:
        report.fields_not_stored["beats"] = report.fields_not_stored.get("beats", 0) + 1


def _count_carried(track: Track, report: VendorIngestReport) -> None:
    for name, key in _CARRIED_EXTENSIONS:
        value = track.extensions.get(key)
        if value is None or value == "":
            continue
        report.extensions_carried[name] = report.extensions_carried.get(name, 0) + 1


def _write_fields(
    writer: StateWriter,
    sid: str,
    track: Track,
    source: str,
    fill_only: bool,
    modified_at: str,
    report: VendorIngestReport,
    write_cues: bool = True,
) -> None:
    conn = writer.raw_conn
    for name, value in _field_values(track):
        current = state_provenance.read_field(conn, sid, name)
        if current is not None and (fill_only or current.source != source):
            # Every protected value is kept and counted, equal or not (ADR: fill, never fight).
            report.fields_kept[name] = report.fields_kept.get(name, 0) + 1
            continue
        if current is not None and current.value == value:
            continue
        if writer.set_field(sid, name, value, source=source, modified_at=modified_at):  # type: ignore[arg-type]
            report.fields_written[name] = report.fields_written.get(name, 0) + 1
    if not write_cues:
        return
    cues = store_cues(track.cues, report)
    if fill_only and cue_store.read_row(conn, sid) is not None:
        return
    report.cue_sets_written += int(
        cue_store.import_vendor_cues(
            writer.set_field, conn, sid, cues, source=source, modified_at=modified_at
        )
    )


def _flatten(playlists: Iterable[Playlist], prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Playlist]]:
    for playlist in playlists:
        path = (*prefix, playlist.name)
        if playlist.track_ids or not playlist.children:
            yield path, playlist
        yield from _flatten(playlist.children, path)


def _playlist_key(vendor: str, path: tuple[str, ...]) -> str:
    """The folder path as one string.

    For rekordbox-xml, a name's own ``\\`` and ``/`` are escaped first. Joining
    the raw names is ambiguous: folder ``A`` holding ``B/C`` and folder ``A/B``
    holding ``C`` would both become ``A/B/C``. Other vendors keep the plain join,
    so their existing playlist ids do not change; their collision is still open.
    """
    if vendor != "rekordbox-xml":
        return "/".join(path)
    return "/".join(part.replace("\\", "\\\\").replace("/", "\\/") for part in path)


def _write_playlists(
    writer: StateWriter,
    playlists: Iterable[Playlist],
    vendor: str,
    entry_to_sid: dict[str, str],
    deleted_entries: set[str],
    report: VendorIngestReport,
) -> None:
    """One library playlist per app playlist or crate, members in app order.

    The id is the folder path inside the app, so two crates both called
    "Warmup" in different folders stay two playlists, and a re-import lands
    on the same rows.
    """
    for path, playlist in _flatten(playlists):
        key = _playlist_key(vendor, path)
        playlist_id = compute_playlist_id(vendor, key)
        writer.insert_playlist(
            playlist_id=playlist_id,
            name=playlist.name,
            vendor=vendor,
            vendor_pl_id=key,
        )
        members: list[str] = []
        for entry_id in playlist.track_ids:
            if entry_id in deleted_entries:
                continue
            sid = entry_to_sid.get(entry_id)
            if sid is None:
                report.playlist_entries_unresolved += 1
                continue
            members.append(sid)
        writer.set_playlist_memberships(playlist_id, members)
        report.playlists_written += 1


# ---------- readers and CLI -------------------------------------------------


@dataclasses.dataclass(frozen=True)
class VendorRead:
    """An app's library as its reader saw it."""

    library: OpenDjLibrary
    resolve_path: PathResolver
    #: What the reader could not carry over, counted per kind
    #: ("bpm dropped: <reason>" -> 12). Never empty because nothing was
    #: checked: each reader reports what it skipped.
    reader_notes: dict[str, int]
    #: Whole entries the reader dropped, so absent from ``library.tracks``.
    entries_dropped: int = 0


def read_vendor(vendor: Vendor, source: Path) -> VendorRead:
    """Read ``source`` with ``vendor``'s reader: the library, its path
    resolver, and a count of everything the reader dropped or changed."""
    source = Path(source).expanduser()
    if vendor == "serato":
        from apps.adapters.serato.adapter import SeratoAdapter, SeratoAdapterOptions

        adapter = SeratoAdapter(
            options=SeratoAdapterOptions(audio_root=serato_volume_root(source), absolute_paths_are_logical=False)
        )
        library, adapter_report = adapter.read(source)
        dropped = _location_entries_dropped(adapter_report)
        return VendorRead(library, serato_path_resolver(source), _count_warnings(adapter_report, _SERATO_READER_LOSSES), dropped)
    if vendor == "traktor":
        from apps.adapters.traktor.adapter import TraktorAdapter

        library, adapter_report = TraktorAdapter().read(source)
        dropped = _location_entries_dropped(adapter_report)
        return VendorRead(library, traktor_path_resolver(), _count_warnings(adapter_report, _TRAKTOR_READER_LOSSES), dropped)
    if vendor == "rekordbox-xml":
        from apps.adapters.rekordbox_xml import read_rekordbox_xml

        xml = read_rekordbox_xml(source)
        # Locations are absolute file URLs; the reader already decoded them.
        return VendorRead(
            xml.library,
            rekordbox_xml_path_resolver,
            _nonzero({"location dropped: streaming or blank": xml.tracks_without_location}),
            xml.tracks_without_location,
        )
    if vendor == "djay":
        from apps.adapters.djay import read_djay_library

        djay = read_djay_library(source)
        # djay stores file URLs; the reader already decoded them to paths.
        return VendorRead(
            djay.library,
            _decoded_path,
            _nonzero({
                "location dropped: streaming, no local file": djay.tracks_streaming,
                "cues dropped: djay cue or loop list could not be read": djay.cue_arrays_unreadable,
            }),
            djay.tracks_streaming,
        )
    if vendor == "virtualdj":
        from apps.adapters.virtualdj import read_virtualdj_library, virtualdj_path_resolver

        vdj = read_virtualdj_library(source)
        return VendorRead(
            vdj.library,
            virtualdj_path_resolver(),
            _nonzero({
                "location dropped: streaming, no local file": vdj.songs_streaming,
                "bpm changed: read as seconds per beat (60 / value)": vdj.bpm_as_period,
                "bpm dropped: outside a beat period and a BPM range": vdj.bpm_dropped,
                "key dropped: VirtualDJ key text not recognized": vdj.keys_unrecognized,
                "location unresolved: Windows drive path kept verbatim": vdj.windows_paths_unresolved,
                "playlist entry unresolved: path not in database.xml": vdj.playlist_entries_unresolved,
                "playlist dropped: VirtualDJ filter folder (smart list)": vdj.filter_folders_skipped,
                **{f"poi dropped: {kind}": n for kind, n in vdj.pois_skipped.items()},
            }),
            vdj.songs_streaming,
        )
    if vendor == "mixxx":
        from apps.adapters.mixxx import read_mixxx_library

        mixxx = read_mixxx_library(source)
        notes = {
            "location dropped: deleted in Mixxx": mixxx.tracks_fs_deleted,
            "track dropped: hidden in Mixxx": mixxx.tracks_hidden,
            **mixxx.notes,
        }
        return VendorRead(
            mixxx.library,
            _decoded_path,
            _nonzero(notes),
            mixxx.tracks_fs_deleted
            + mixxx.tracks_hidden
            + mixxx.notes.get("location dropped: no file path in Mixxx", 0),
        )
    raise ValueError(f"unknown vendor {vendor!r}")


def read_vendor_library(vendor: Vendor, source: Path) -> tuple[OpenDjLibrary, PathResolver]:
    """Read ``source`` with ``vendor``'s adapter, plus its path resolver."""
    read = read_vendor(vendor, source)
    return read.library, read.resolve_path


def _decoded_path(path: str) -> str:
    return path


# (field, action) pairs DJIMPORT-6039 counts as reader losses. Any other warning
# (a playlist entry) is not counted here yet.
_SERATO_READER_LOSSES = frozenset({("location", "dropped"), ("cues", "dropped"), ("bpm", "dropped")})
_TRAKTOR_READER_LOSSES = frozenset({
    ("location", "dropped"),
    ("bpm", "dropped"),
    ("key", "dropped"),
    ("rating", "dropped"),
    ("duration", "dropped"),
    ("play_count", "dropped"),
    ("cue_points.position", "dropped"),
    ("cue_points.color", "downgraded"),
})


def _location_entries_dropped(adapter_report: AdapterReport) -> int:
    """Whole library entries a reader dropped for want of a file path."""
    return sum(
        1
        for warning in adapter_report.warnings
        if warning.field == "location" and warning.action == "dropped"
    )


def _count_warnings(adapter_report: AdapterReport, losses: frozenset[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for warning in adapter_report.warnings:
        if (warning.field, warning.action) not in losses:
            continue
        key = f"{warning.field} {warning.action}: {warning.reason}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _nonzero(counts: dict[str, int]) -> dict[str, int]:
    return {key: n for key, n in counts.items() if n}


def source_modified_at(source: Path, *, library_file: str = "database V2") -> str:
    """When the app last saved its library, as an RFC 3339 UTC stamp.

    For a ``_Serato_`` folder that is its ``database V2`` file.
    """
    target = source / library_file if source.is_dir() else source
    mtime = target.stat().st_mtime
    return _dt.datetime.fromtimestamp(mtime, _dt.UTC).isoformat()


def default_source(vendor: Vendor) -> Path | None:
    """Where the app keeps its library on this machine, when it exists."""
    home = Path.home()
    if vendor == "serato":
        candidate = home / "Music" / "_Serato_"
        return candidate if (candidate / "database V2").exists() else None
    if vendor == "traktor":
        root = home / "Documents" / "Native Instruments"
        found = sorted(root.glob("Traktor */collection.nml")) if root.is_dir() else []
        return found[-1] if found else None
    if vendor == "djay":
        from apps.shared.platform_paths import DJAY_LIVE_DB

        return DJAY_LIVE_DB if DJAY_LIVE_DB.is_file() else None
    if vendor == "virtualdj":
        candidate = home / "Documents" / "VirtualDJ"
        return candidate if (candidate / "database.xml").is_file() else None
    if vendor == "mixxx":
        from apps.adapters.mixxx import DEFAULT_LIBRARY_CANDIDATES

        found: list[Path] = []
        for rel in DEFAULT_LIBRARY_CANDIDATES:
            candidate = home / rel
            if candidate.is_file():
                found.append(candidate)
        if not found:
            return None
        # Prefer the sandboxed container path when both exist.
        return found[0]
    return None


def _print_summary(report: VendorIngestReport) -> None:
    print(f"{report.vendor} import ({'dry-run' if report.dry_run else 'write'})")
    print(f"  source:               {report.source}")
    print(f"  entries in source:    {report.entries_seen + report.entries_dropped_by_reader}")
    print(f"  dropped by reader:    {report.entries_dropped_by_reader}")
    print(f"  entries in library:   {report.entries_seen}")
    print(f"  matched to tracks:    {report.tracks_matched}")
    print(f"  new tracks:           {report.tracks_inserted}")
    print(f"  files not on disk:    {report.files_missing}")
    print(f"  skipped (removed):    {report.tracks_skipped_deleted}")
    print(f"  skipped (other):      {report.tracks_skipped}")
    for name in _FILLED_FIELDS:
        print(
            f"  {name:<9} written {report.fields_written.get(name, 0)}, "
            f"kept another source's {report.fields_kept.get(name, 0)}"
        )
    carried = [f"{name} {report.extensions_carried[name]}" for name, _ in _CARRIED_EXTENSIONS if report.extensions_carried.get(name)]
    print(f"  carried, not stored:  {', '.join(carried) if carried else 'none'}")
    if report.fields_not_stored:
        parts = [f"{name} {report.fields_not_stored[name]}" for name in sorted(report.fields_not_stored)]
        print(f"  read, not stored yet: {', '.join(parts)}")
    print(f"  cue sets written:     {report.cue_sets_written}")
    print(f"  cues with no kind:    {report.cues_skipped}")
    print(f"  playlists written:    {report.playlists_written}")
    print(f"  playlist entries unresolved: {report.playlist_entries_unresolved}")
    print(f"  not carried over by the reader: {sum(report.reader_notes.values())}")
    for note, count in sorted(report.reader_notes.items(), key=lambda item: (-item[1], item[0])):
        print(f"    {count:>6}  {note}")
    print(f"  duration:             {report.duration_s:.3f}s")


def run_cli(args: argparse.Namespace, vendor: Vendor) -> int:
    """Import one ``vendor`` library as ``args`` (``source``, ``write``, ``db``) say."""
    source = Path(args.source).expanduser() if args.source else default_source(vendor)
    if source is None or not source.exists():
        where = args.source or "the default location"
        print(f"error: no {vendor} library found at {where}", file=sys.stderr)
        return 1
    read = read_vendor(vendor, source)
    state_path = Path(args.db) if args.db else state_paths.STATE_DB
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor=f"ingest-{vendor}")
    try:
        report = ingest_library(
            writer,
            read.library,
            vendor=vendor,
            # djay's cues stay gated behind DJIMPORT-5956b and this path never reads
            # them, so an empty cue list here means unread, not cleared.
            write_cues=vendor != "djay",
            source=str(source),
            resolve_path=read.resolve_path,
            dry_run=not args.write,
            modified_at=source_modified_at(
                source, library_file="database.xml" if vendor == "virtualdj" else "database V2"
            ),
        )
        report.reader_notes = dict(read.reader_notes)
        report.entries_dropped_by_reader = read.entries_dropped
        _print_summary(report)
    finally:
        writer.close()
        conn.close()
    return 0


# ---------- Serato history -> play log ---------------------------------------


@dataclasses.dataclass
class HistoryImportReport:
    source: str
    dry_run: bool
    sessions_read: int = 0
    entries_seen: int = 0
    plays_written: int = 0
    plays_already_logged: int = 0
    skipped: dict[str, int] = dataclasses.field(default_factory=dict)
    unmatched: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _skip_reason(entry: serato_history.HistoryEntry) -> str | None:
    if not entry.played:
        return "not played"
    if entry.start_s is None:
        return "no start time"
    if not entry.path:
        return "no path"
    return None


def _imported_play(entry: serato_history.HistoryEntry, stable_id: str) -> play_log.ImportedPlay:
    assert entry.start_s is not None
    if entry.end_s is not None and entry.end_s >= entry.start_s:
        audible: float | None = float(entry.end_s - entry.start_s)
    elif entry.playtime_s is not None:
        audible = float(entry.playtime_s)
    else:
        audible = None
    return play_log.ImportedPlay(
        stable_id=stable_id,
        play_id=entry.play_id,
        ts=canonical_from(_dt.datetime.fromtimestamp(entry.start_s, _dt.UTC)),
        audible_s=audible,
        deck=entry.deck,
        detail={"session": entry.session_file, "entry": entry.index},
    )


def import_serato_history(
    conn: Any, serato_dir: Path, *, dry_run: bool, actor: str = "ingest-serato-history"
) -> HistoryImportReport:
    """Append one ``track_played`` event per played, library-matched Serato entry."""
    start = time.perf_counter()
    report = HistoryImportReport(source=str(serato_dir), dry_run=dry_run)
    resolve = serato_path_resolver(serato_dir)
    by_path = live_tracks_by_path(conn)
    plays: list[play_log.ImportedPlay] = []
    for file in serato_history.session_files(serato_dir):
        read = serato_history.read_session(file)
        report.sessions_read += 1
        if read.error is not None:
            report.errors.append(read.error)
        for entry in read.entries:
            report.entries_seen += 1
            reason = _skip_reason(entry)
            if reason is not None:
                report.skipped[reason] = report.skipped.get(reason, 0) + 1
                continue
            assert entry.path is not None
            key = nfc(resolve(entry.path))
            stable_id = by_path.get(key)
            if stable_id is None:
                report.unmatched.append(key)
                continue
            plays.append(_imported_play(entry, stable_id))
    conn.execute("BEGIN IMMEDIATE")
    try:
        written = play_log.import_plays(conn, plays, source="serato", actor=actor)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    report.plays_written = written
    report.plays_already_logged = len(plays) - written
    report.duration_s = round(time.perf_counter() - start, 3)
    return report


def _print_history_summary(report: HistoryImportReport) -> None:
    mode = "dry-run (rolled back)" if report.dry_run else "written"
    print(f"Serato history import: {mode}")
    print(f"  source:               {report.source}")
    print(f"  sessions read:        {report.sessions_read}")
    print(f"  entries seen:         {report.entries_seen}")
    print(f"  plays written:        {report.plays_written}")
    print(f"  plays already logged: {report.plays_already_logged}")
    for reason, count in sorted(report.skipped.items()):
        print(f"  skipped ({reason}): {count}")
    print(f"  unmatched:            {len(report.unmatched)}")
    for path in report.unmatched:
        print(f"    {path}")
    for error in report.errors:
        print(f"  error: {error}")
    print(f"  duration:             {report.duration_s:.3f}s")


def run_history_cli(args: argparse.Namespace) -> int:
    """Import Serato history as ``args`` (``source``, ``write``, ``db``) say."""
    source = Path(args.source).expanduser() if args.source else Path.home() / "Music" / "_Serato_"
    if not (source / "History" / "Sessions").is_dir():
        print(f"error: no Serato history found at {source}", file=sys.stderr)
        return 1
    conn = state_db.open_rw(Path(args.db) if args.db else state_paths.STATE_DB)
    try:
        report = import_serato_history(conn, source, dry_run=not args.write)
    finally:
        conn.close()
    _print_history_summary(report)
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if report.errors else 0


# ---------- traktor history -> play log ------------------------------------


@dataclasses.dataclass
class TraktorHistoryImportReport:
    """What a Traktor history import read and appended. ``entries_seen`` is the denominator."""

    source: str
    zone: str
    dry_run: bool
    files_read: list[str]
    files_rejected: list[tuple[str, str]]
    entries_seen: int
    entries_not_public: int
    entries_unreadable: int
    plays_matched: int
    plays_unmatched: int
    plays_added: int
    plays_already_logged: int
    added_stable_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def import_traktor_history(
    conn: Any,
    traktor_dir: Path,
    *,
    zone: _dt.tzinfo | None,
    dry_run: bool,
    actor: str,
    resolve_path: PathResolver | None = None,
) -> TraktorHistoryImportReport:
    """Append the PLAYEDPUBLIC entries of ``<traktor_dir>/History`` to the play log.

    Raises FileNotFoundError when the folder has no History/. Rolled back when ``dry_run``.
    """
    from apps.adapters.traktor import history

    read = history.read_history_dir(history.history_dir_for(traktor_dir), zone)
    resolver = resolve_path if resolve_path is not None else traktor_path_resolver()
    conn.execute("BEGIN IMMEDIATE")
    try:
        matched, unmatched = history.match_plays(read.plays, live_tracks_by_path(conn), resolver)
        logged = play_log.imported_play_ids(conn, history.PLAY_SOURCE)
        fresh = [play for play in matched if play.play_id not in logged]
        added = play_log.import_plays(conn, fresh, actor=actor)
        if dry_run:
            conn.rollback()
        elif not dry_run:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return TraktorHistoryImportReport(
        source=str(traktor_dir),
        zone="local" if zone is None else str(zone),
        dry_run=dry_run,
        files_read=read.files_read,
        files_rejected=read.files_rejected,
        entries_seen=read.entries_seen,
        entries_not_public=read.entries_not_public,
        entries_unreadable=read.entries_unreadable,
        plays_matched=len(matched),
        plays_unmatched=unmatched,
        plays_added=added,
        plays_already_logged=len(matched) - len(fresh),
        added_stable_ids=sorted({play.stable_id for play in fresh}),
    )


def _print_traktor_history_summary(report: TraktorHistoryImportReport) -> None:
    print(f"traktor-history import ({'dry-run' if report.dry_run else 'write'})")
    print(f"  source:               {report.source}")
    print(f"  zone:                 {report.zone}")
    print(f"  files read:           {len(report.files_read)}")
    for name, reason in report.files_rejected:
        print(f"  rejected: {name} ({reason})")
        print(f"warning: skipped {name}: {reason}", file=sys.stderr)
    print(f"  entries seen:         {report.entries_seen}")
    print(f"  not played public:    {report.entries_not_public}")
    print(f"  unreadable:           {report.entries_unreadable}")
    print(f"  matched to tracks:    {report.plays_matched}")
    print(f"  unmatched:            {report.plays_unmatched}")
    print(f"  plays added:          {report.plays_added}")
    print(f"  already logged:       {report.plays_already_logged}")


def run_traktor_history_cli(args: argparse.Namespace) -> int:
    """``traktor-history`` as ``args`` (``source``, ``zone``, ``write``, ``db``) say."""
    from apps.adapters.traktor import history

    source = Path(args.source).expanduser() if args.source else history.default_traktor_dir()
    if source is None or not (source / history.HISTORY_DIRNAME).is_dir():
        where = args.source or "the default location"
        print(f"error: no Traktor history found at {where}", file=sys.stderr)
        return 1
    zone = ZoneInfo(args.zone) if args.zone else None
    conn = state_db.open_rw(Path(args.db) if args.db else state_paths.STATE_DB)
    try:
        report = import_traktor_history(
            conn, source, zone=zone, dry_run=not args.write, actor="ingest-traktor-history"
        )
    finally:
        conn.close()
    _print_traktor_history_summary(report)
    return 0


_WHERE: dict[str, str] = {
    "serato": "the _Serato_ folder (default ~/Music/_Serato_)",
    "traktor": "collection.nml (default: the newest Traktor folder in ~/Documents/Native Instruments)",
    "rekordbox-xml": "a rekordbox XML export (from rekordbox, Lexicon, Engine DJ or VirtualDJ); required",
    "djay": "djay's MediaLibrary.db (default ~/Music/djay/djay Media Library.djayMediaLibrary/MediaLibrary.db)",
    "virtualdj": "the VirtualDJ folder holding database.xml and MyLists (default ~/Documents/VirtualDJ)",
    "mixxx": (
        "Mixxx's mixxxdb.sqlite (default: sandboxed "
        "~/Library/Containers/org.mixxx.mixxx/... path, then "
        "~/Library/Application Support/Mixxx/)"
    ),
    "traktor-history": "the Traktor folder holding History/ (default: the newest Traktor folder in ~/Documents/Native Instruments)",
    "serato-history": "the _Serato_ folder holding History/Sessions (default ~/Music/_Serato_)",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.adapters.library_import",
        description=(
            "Import another DJ app's library into state.db. A file already in "
            "the library is merged onto its track; BPM, key, rating, comments "
            "and genre only fill values no other source set; cues follow the "
            "cue store's ownership rule; removed tracks stay removed."
        ),
    )
    parser.add_argument(
        "vendor",
        choices=(*VENDORS, *HISTORY_COMMANDS),
        help="which app's library to read, or serato-history / traktor-history to import plays",
    )
    parser.add_argument(
        "--source", default=None, help="; ".join(f"{v}: {w}" for v, w in _WHERE.items())
    )
    parser.add_argument(
        "--write", action="store_true", help="persist changes (default: dry-run, rolled back)"
    )
    parser.add_argument("--db", default=None, help="state.db path (default: the app's state DB)")
    parser.add_argument(
        "--zone", default=None, help="traktor-history only: IANA zone of the history times (default: this machine's)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """``python -m apps.adapters.library_import <vendor> [--source P] [--write]``."""
    args = _build_parser().parse_args(argv)
    if args.vendor == "serato-history":
        return run_history_cli(args)
    if args.vendor == "traktor-history":
        return run_traktor_history_cli(args)
    if args.vendor in VENDORS:
        return run_cli(args, args.vendor)
    raise ValueError(f"unhandled import {args.vendor!r}")


__all__ = [
    "HISTORY_COMMANDS",
    "PLAY_SOURCES",
    "VENDORS",
    "HistoryImportReport",
    "PathResolver",
    "TraktorHistoryImportReport",
    "Vendor",
    "VendorIngestReport",
    "VendorRead",
    "default_source",
    "import_serato_history",
    "import_traktor_history",
    "ingest_library",
    "live_tracks_by_path",
    "main",
    "nfc",
    "read_vendor",
    "read_vendor_library",
    "run_cli",
    "run_history_cli",
    "run_traktor_history_cli",
    "serato_path_resolver",
    "serato_volume_root",
    "source_modified_at",
    "store_cues",
    "traktor_path_resolver",
]


if __name__ == "__main__":
    raise SystemExit(main())
