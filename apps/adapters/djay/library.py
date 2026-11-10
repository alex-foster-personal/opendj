"""Read a djay Pro ``MediaLibrary.db`` into an ``OpenDjLibrary``.

This module adds no parsing. It assembles what :mod:`apps.shared.djay_db`
already reads (tracks, user data, cues, analysis, playlists) into the shape
the shared library import takes.

* **Tracks.** Only local files are read. Streaming entries (Spotify,
  TIDAL, ...) have no file to put in the library, so they are counted.
* **BPM and key.** BPM is the user's manual BPM when set, otherwise djay's
  analyzed BPM. The key comes from the analysis, as Camelot.
* **Cues.** The cue byte layout follows ``docs/djay_db_schema_v2_addendum_cues.md``,
  which was written against djay's own writer and has not been checked
  against a large corpus of real libraries. A blob the parser cannot read
  yields no cues, so a parse gap shows up as missing cues and never as
  wrong ones.
* **Playlists.** djay nests playlists under parents; the root playlist (no
  parent) is the library itself and is not imported as a playlist.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from apps.adapters.rekordbox.models import VendorUsbMention
from apps.open_dj import CuePoint, OpenDjLibrary, Playlist, Track
from apps.open_dj.provenance import utc_mtime
from apps.shared import djay_db
from apps.shared.normalised import NormalisedAnalysis, NormalisedCue
from apps.sync.usb.stick_model import is_canonical_volume_uuid


@dataclass(frozen=True)
class DjayLibrary:
    library: OpenDjLibrary
    #: Entries with no local file (streaming services), not imported.
    tracks_streaming: int
    #: Cue or loop arrays djay saved that could not be read in full; those
    #: cues are missing, so a track is never reported as simply having none.
    cue_arrays_unreadable: int = 0


def read_djay_library(db_path: Path, *, unverified_fields: bool = False) -> DjayLibrary:
    """Assemble ``db_path`` (djay's ``MediaLibrary.db``) into an ``OpenDjLibrary``.

    By default only what the committed fixture proves is carried: local
    tracks with their path, title, artists and duration (DJIMPORT-5956).
    ISRC, rating, play count, BPM, key, cues and playlists are read only with
    ``unverified_fields=True`` (DJIMPORT-5956b), which the import CLI never
    passes, until a sanitized capture holding those rows is committed.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"djay library not found at {db_path}")
    # Stat the live library before snapshotting: the temp copy's mtime is
    # not the source time.
    stamp = utc_mtime(db_path)
    with _snapshot(db_path) as snapshot:
        return _read_snapshot(
            snapshot, unverified_fields=unverified_fields, source_modified_at=stamp
        )


@contextmanager
def _snapshot(db_path: Path) -> Iterator[Path]:
    """A consistent copy of ``db_path``, including what djay has only written
    to its ``-wal`` file.

    The ``djay_db`` readers open with ``immutable=1``, which ignores the WAL,
    so reading the live library while djay is open would silently miss its
    recent changes. SQLite's backup API on a WAL-aware read-only connection
    copies the committed state; a library it cannot open raises.
    """
    with tempfile.TemporaryDirectory(prefix="djay-snapshot-") as tmp:
        copy = Path(tmp) / "MediaLibrary.db"
        source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        yield copy


def _read_snapshot(
    db_path: Path, *, unverified_fields: bool, source_modified_at: datetime
) -> DjayLibrary:
    analysis = {a.uuid_or_id: a for a in djay_db.iter_analysis(db_path)} if unverified_fields else {}
    cues_by_uuid, cue_arrays_unreadable = _cues_by_uuid(db_path) if unverified_fields else ({}, 0)
    tracks: list[Track] = []
    streaming = 0
    for item in djay_db.iter_tracks(db_path):
        if not item.is_local or item.file_path is None:
            streaming += 1
            continue
        tracks.append(
            _track(
                item,
                analysis.get(item.uuid),
                cues_by_uuid.get(item.uuid, ()),
                unverified_fields=unverified_fields,
                source_modified_at=source_modified_at,
            )
        )
    return DjayLibrary(
        library=OpenDjLibrary(
            version="0.1",
            tracks=tuple(tracks),
            playlists=(tuple(playlist_tree(djay_db.iter_playlists(db_path))) if unverified_fields else ()),
        ),
        tracks_streaming=streaming,
        cue_arrays_unreadable=cue_arrays_unreadable,
    )


def _track(
    item: djay_db.DjayTrack,
    found: NormalisedAnalysis | None,
    cues: Iterable[NormalisedCue],
    *,
    unverified_fields: bool,
    source_modified_at: datetime,
) -> Track:
    bpm = None
    key = None
    if found is not None:
        bpm = found.manual_bpm or found.bpm
        key = found.key_camelot
    return Track(
        # djay's own uuid (database2.key), so the vendor mapping holds the
        # id djay's writers look up, not a prefixed copy of it.
        track_id=item.uuid,
        file_path=str(item.file_path),
        title=item.title,
        artists=split_artists(item.artist),
        bpm=bpm if bpm and bpm > 0 else None,
        key_camelot=key or None,
        rating=item.rating if unverified_fields and item.rating > 0 else None,
        duration_ms=round(item.duration_s * 1000) if item.duration_s else None,
        play_count=item.play_count if unverified_fields else 0,
        isrc=(item.isrc or None) if unverified_fields else None,
        cues=tuple(to_cue_points(cues)),
        source_modified_at=source_modified_at,
    )


def to_cue_points(cues: Iterable[NormalisedCue]) -> list[CuePoint]:
    """``NormalisedCue`` -> open-dj ``CuePoint``. A hot cue keeps its pad."""
    out: list[CuePoint] = []
    for order, cue in enumerate(cues):
        if cue.kind == "hot" and cue.index is not None:
            index, cue_type = cue.index, "hot"
        elif cue.kind == "loop":
            index, cue_type = order, "loop"
        elif cue.kind in ("memory", "load"):
            index, cue_type = order, cue.kind
        else:
            continue
        rgb = None
        if cue.color_rgb is not None:
            r, g, b = cue.color_rgb
            rgb = (r << 16) | (g << 8) | b
        out.append(
            CuePoint(
                index=index,
                position_ms=cue.position_msec,
                type=cue_type,  # type: ignore[arg-type]
                name=cue.name or "",
                color_rgb=rgb,
                length_ms=cue.loop_length_msec,
            )
        )
    return out


#: djay's whole-library root playlist (``apps/sync/playlist_plan.py`` uses the same sentinel).
ROOT_PLAYLIST_UUID = "mediaItemPlaylist-root"


def split_artists(raw: str | None) -> tuple[str, ...]:
    """Split djay's single artist string into names.

    djay joins collaborators with commas (``"A,B"`` or ``"A, B"``); the other
    djay adapter splits the same way, so one recording groups as one set of
    artists whichever path read it.
    """
    return tuple(name.strip() for name in (raw or "").split(",") if name.strip())


def playlist_tree(playlists: Iterable[djay_db.DjayPlaylist]) -> list[Playlist]:
    """Nest djay's flat playlist rows by ``parent_uuid``.

    djay's whole-library root (uuid ``mediaItemPlaylist-root``, see
    ``docs/djay_db_schema_v2.md`` section 3.4) is never a playlist, even when
    it has no children; its children become top-level playlists. Any other
    row with no parent is a top-level playlist or folder. A parent that is
    not in the list makes its children top-level rather than lost.
    """
    rows = list(playlists)
    by_uuid = {row.uuid: row for row in rows}
    children: dict[str | None, list[djay_db.DjayPlaylist]] = defaultdict(list)
    for row in rows:
        parent = row.parent_uuid if row.parent_uuid in by_uuid else None
        children[parent].append(row)

    def build(row: djay_db.DjayPlaylist, seen: frozenset[str]) -> Playlist:
        kids = [build(kid, seen | {row.uuid}) for kid in children.get(row.uuid, []) if kid.uuid not in seen]
        return Playlist(
            name=row.name,
            track_ids=tuple(row.track_uuids),
            children=tuple(kids),
        )

    out: list[Playlist] = []
    for root in children.get(None, []):
        if root.uuid == ROOT_PLAYLIST_UUID:
            out.extend(build(kid, frozenset({root.uuid})) for kid in children.get(root.uuid, []))
        else:
            out.append(build(root, frozenset()))
    return out


def _cues_by_uuid(db_path: Path) -> tuple[dict[str, list[NormalisedCue]], int]:
    """Every track's cues in one read-only pass (not one connection per track),
    plus how many cue or loop arrays could not be read in full."""
    # immutable=1, as djay_db reads it: never takes a lock djay could trip on.
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT key, data FROM database2 WHERE collection = 'mediaItemUserData'").fetchall()
    finally:
        con.close()
    out: dict[str, list[NormalisedCue]] = {}
    unreadable = 0
    for key, data in rows:
        if not data:
            continue
        cues, lost = djay_db.parse_cues_with_loss(data)
        unreadable += lost
        if cues:
            out[str(key)] = cues
    return out, unreadable


_DJAY_USB_COLLECTIONS = (
    "removableMediaVolumes",
    "removableVolumes",
    "externalVolumes",
)

_VOLUME_PATH_MARKERS = ("/Volumes/", "file:///Volumes/")


def _djay_tsaf_field(data: bytes, field: str) -> str | None:
    marker = f"\x08{field}\x00".encode()
    idx = data.find(marker)
    if idx < 1:
        return None
    start = idx - 1
    while start > 0 and data[start] != 0x08:
        start -= 1
    if start <= 0 or data[start] != 0x08:
        return None
    try:
        return data[start + 1 : idx].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def _mention_from_djay_blob(data: bytes) -> VendorUsbMention | None:
    kv = djay_db._tsaf_kv(data)
    name = (
        (kv.get("name") or kv.get("volumeName") or kv.get("title") or "").strip()
    )
    raw_uuid = (kv.get("volumeUUID") or kv.get("uuid") or "").strip().upper()
    volume_uuid = raw_uuid if raw_uuid and is_canonical_volume_uuid(raw_uuid) else None
    if not name and volume_uuid is None:
        name = _djay_tsaf_field(data, "volumeName") or _djay_tsaf_field(data, "name")
        if not name:
            return None
    return VendorUsbMention(
        name=name,
        source="djay",
        volume_uuid=volume_uuid,
    )


def _mention_from_djay_path(file_uri: str) -> VendorUsbMention | None:
    path = djay_db._file_uri_to_path(file_uri)
    if path is None:
        return None
    parts = path.parts
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "Volumes":
        return None
    label = parts[2].strip()
    if not label:
        return None
    return VendorUsbMention(name=label, source="djay", volume_uuid=None)


def list_djay_usb_mentions(db_path: Path) -> list[VendorUsbMention]:
    """Read-only USB stick names remembered in djay's MediaLibrary.db."""
    path = Path(db_path)
    if not path.is_file():
        return []
    con = sqlite3.connect(f"file:{path.resolve().as_uri()}?mode=ro", uri=True)
    by_uuid: dict[str, VendorUsbMention] = {}
    by_label: dict[str, VendorUsbMention] = {}
    try:
        for collection in _DJAY_USB_COLLECTIONS:
            try:
                rows = con.execute(
                    "SELECT data FROM database2 WHERE collection = ?", (collection,)
                ).fetchall()
            except sqlite3.Error:
                continue
            for (data,) in rows:
                if not data:
                    continue
                mention = _mention_from_djay_blob(data)
                if mention is None:
                    continue
                if mention.volume_uuid is not None:
                    by_uuid.setdefault(mention.volume_uuid, mention)
                else:
                    key = mention.name.casefold()
                    by_label.setdefault(key, mention)

        for (_key, data) in con.execute(
            "SELECT key, data FROM database2 "
            "WHERE collection = 'localMediaItemLocations'"
        ):
            if not data:
                continue
            uri = djay_db._tsaf_kv(data).get("sourceURIs", "")
            if not uri or not any(marker in uri for marker in _VOLUME_PATH_MARKERS):
                continue
            mention = _mention_from_djay_path(uri)
            if mention is None:
                continue
            key = mention.name.casefold()
            by_label.setdefault(key, mention)
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [*by_uuid.values(), *by_label.values()]
