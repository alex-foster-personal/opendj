"""Read a Pioneer USB export tree and emit a JSON-friendly dict.

Usage
-----

>>> from apps.sync.usb.pioneer import read_usb_export
>>> data = read_usb_export(Path("/Volumes/MYUSB/PIONEER"))
>>> data["metadata"]["total_tracks"]
199

Design notes
------------

* The reader is **read-only**. It never writes to the source tree.
* The return value is a plain ``dict`` of plain types (``int``, ``str``,
  ``list``, ``dict``) so it round-trips through :mod:`json` without
  custom encoders.
* OneLibrary (``exportLibrary.db``) is detected but not decrypted —
  pyrekordbox 0.4.4 has no ``DeviceLibraryPlus`` / ``OneLibrary`` class,
  and the SQLCipher key for OneLibrary is not publicly documented (it
  differs from the desktop ``master.db`` key and must be recovered via
  reverse engineering). Tracked under CAT-06 follow-up.
* Per-track key/grid/loudness presence for write-back verify lives in
  :func:`apps.sync.usb.pioneer.value_verify.verify_stick_values`.

Requirement: CAT-06.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kaitaistruct import KaitaiStream
from pyrekordbox.anlz import AnlzFile, walk_anlz_paths

from ._vendor.rekordbox_pdb import RekordboxPdb

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PDB helpers
# ---------------------------------------------------------------------------

_PT = RekordboxPdb.PageType


def _open_pdb(path: Path) -> RekordboxPdb:
    """Load an ``export.pdb`` (non-ext) into a parsed tree.

    The caller is responsible for keeping the underlying file handle
    alive for as long as the parser is used — the Kaitai parser uses
    *lazy* properties that re-read from ``_io``. We stash the handle
    onto the parser instance as ``_src`` to tie its lifetime to the GC.
    """
    f = path.open("rb")
    ks = KaitaiStream(f)
    pdb = RekordboxPdb(False, ks)
    pdb._src = f  # pin file handle to parser lifetime
    return pdb


def _iter_pages(table: Any) -> Iterator[Any]:
    """Walk a table's page chain.

    The PDB format stores tables as singly-linked lists of fixed-size
    pages; ``first_page`` → ``next_page`` → ... → ``last_page``. We
    stop at ``last_page`` (inclusive) and also guard against self-loops
    in corrupt exports.
    """
    page_ref = table.first_page
    last_index = table.last_page.index
    seen: set[int] = set()
    while True:
        page = page_ref.body
        if page.page_index in seen:
            # Cycle guard — never happens on well-formed exports.
            break
        seen.add(page.page_index)
        yield page
        if page.page_index == last_index:
            break
        page_ref = page.next_page


def _iter_rows(table: Any) -> Iterator[Any]:
    """Yield every *present* row body in a table, skipping tombstones."""
    for page in _iter_pages(table):
        if not page.is_data_page:
            # Index pages have no rows.
            continue
        for group in page.row_groups:
            for ref in group.rows:
                if ref.present:
                    body = ref.body
                    if body is not None:
                        yield body


def _find_table(pdb: RekordboxPdb, page_type: Any) -> Any | None:
    """Return the ``Table`` entry with matching ``page_type`` enum value."""
    target = page_type.value if hasattr(page_type, "value") else int(page_type)
    for t in pdb.tables:
        # ``t.type`` is a raw int in Kaitai 0.11 (enum resolution happens lazily).
        t_val = t.type.value if hasattr(t.type, "value") else int(t.type)
        if t_val == target:
            return t
    return None


def _dsql_str(field: Any) -> str | None:
    """Decode a DeviceSQL string field into a plain ``str`` (or ``None``)."""
    if field is None:
        return None
    body = getattr(field, "body", None)
    if body is None:
        return None
    text = getattr(body, "text", None)
    if text is None:
        return None
    # DeviceSqlLongAscii / DeviceSqlLongUtf16le store bytes; Short strings
    # already decoded. Normalize to ``str``.
    if isinstance(text, bytes):
        try:
            return text.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            return text.decode("latin-1", errors="replace")
    return str(text).rstrip("\x00") or None


# ---------------------------------------------------------------------------
# Table extractors
# ---------------------------------------------------------------------------


def _read_lookup(
    pdb: RekordboxPdb, page_type: Any, name_attr: str = "name"
) -> dict[int, str]:
    """Extract a simple ``{id: name}`` lookup from a table.

    Used for artists, albums, genres, keys, colors, labels.
    """
    table = _find_table(pdb, page_type)
    if table is None:
        return {}
    out: dict[int, str] = {}
    for row in _iter_rows(table):
        rid = getattr(row, "id", None)
        if rid is None:
            continue
        name = _dsql_str(getattr(row, name_attr, None))
        out[int(rid)] = name or ""
    return out


def _read_tracks(pdb: RekordboxPdb) -> list[dict[str, Any]]:
    """Extract the ``tracks`` table into a list of flat dicts."""
    table = _find_table(pdb, _PT.tracks)
    if table is None:
        return []
    tracks: list[dict[str, Any]] = []
    for row in _iter_rows(table):
        tracks.append(
            {
                "id": int(row.id),
                "title": _dsql_str(row.title),
                "artist_id": int(row.artist_id),
                "album_id": int(row.album_id),
                "genre_id": int(row.genre_id),
                "key_id": int(row.key_id),
                "color_id": int(row.color_id),
                "label_id": int(row.label_id),
                "artwork_id": int(row.artwork_id),
                # Tempo is stored as BPM × 100 (e.g. 12800 => 128.00 BPM).
                "bpm": row.tempo / 100.0 if row.tempo else None,
                "bpm_raw": int(row.tempo),
                "rating": int(row.rating),
                "duration_s": int(row.duration),
                "bitrate": int(row.bitrate),
                "sample_rate": int(row.sample_rate),
                "sample_depth": int(row.sample_depth),
                "file_size": int(row.file_size),
                "track_number": int(row.track_number),
                "disc_number": int(row.disc_number),
                "play_count": int(row.play_count),
                "year": int(row.year),
                "path": _dsql_str(row.file_path),
                "filename": _dsql_str(row.filename),
                "analyze_path": _dsql_str(row.analyze_path),
                "analyze_date": _dsql_str(row.analyze_date),
                "date_added": _dsql_str(row.date_added),
                "release_date": _dsql_str(row.release_date),
                "comment": _dsql_str(row.comment),
                "mix_name": _dsql_str(row.mix_name),
                "isrc": _dsql_str(row.isrc),
            }
        )
    return tracks


def _read_playlist_tree(pdb: RekordboxPdb) -> list[dict[str, Any]]:
    """Extract playlists, excluding entries (see :func:`_read_playlist_entries`)."""
    table = _find_table(pdb, _PT.playlist_tree)
    if table is None:
        return []
    playlists: list[dict[str, Any]] = []
    for row in _iter_rows(table):
        playlists.append(
            {
                "id": int(row.id),
                "parent_id": int(row.parent_id),
                "name": _dsql_str(row.name) or "",
                "is_folder": bool(row.is_folder) if hasattr(row, "is_folder") else False,
                "sort_order": int(row.sort_order) if hasattr(row, "sort_order") else 0,
                "track_ids": [],  # populated below
            }
        )
    return playlists


def _read_playlist_entries(pdb: RekordboxPdb) -> list[dict[str, Any]]:
    """Extract every (playlist_id, track_id, entry_index) tuple."""
    table = _find_table(pdb, _PT.playlist_entries)
    if table is None:
        return []
    entries: list[dict[str, Any]] = []
    for row in _iter_rows(table):
        entries.append(
            {
                "playlist_id": int(row.playlist_id),
                "track_id": int(row.track_id),
                "entry_index": int(row.entry_index),
            }
        )
    return entries


# ---------------------------------------------------------------------------
# ANLZ helpers
# ---------------------------------------------------------------------------


def _anlz_summary(anlz_root: Path) -> dict[str, Any]:
    """Walk ``USBANLZ/`` and summarize tag coverage.

    Returns ``{"total_dirs": N, "tag_counts": {"PQTZ": K, ...},
    "dirs_by_track": {track_id_hex: paths}}``.
    ``track_id_hex`` is the parent directory name (8-char uppercase hex)
    — this matches ``TrackRow.analyze_path``'s trailing component after
    stripping the ``/PIONEER/USBANLZ/P0xx/`` prefix.
    """
    if not anlz_root.exists():
        return {"total_dirs": 0, "tag_counts": {}, "dirs_by_track": {}}

    tag_counts: Counter[str] = Counter()
    dirs_by_track: dict[str, dict[str, str]] = {}
    total = 0
    for root, paths in walk_anlz_paths(anlz_root):
        total += 1
        key = Path(root).name  # e.g. "000252A0"
        dirs_by_track[key] = {
            ext: str(p.relative_to(anlz_root.parent))
            for ext, p in paths.items()
        }
        # Peek at .DAT tag types (cheap, parses header + tag headers).
        dat = paths.get("DAT")
        if dat is not None:
            try:
                f = AnlzFile.parse_file(dat)
                for t in f.tag_types:
                    tag_counts[t] += 1
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("failed to parse %s: %s", dat, exc)
    return {
        "total_dirs": total,
        "tag_counts": dict(tag_counts),
        "dirs_by_track": dirs_by_track,
    }


def read_anlz_dir(anlz_dir: Path) -> dict[str, Any]:
    """Parse a single ANLZ directory into a summary dict.

    Used by the ``--validate`` path and by focused tests. Returns:

    .. code-block:: python

        {
            "path": "relative/path",
            "tag_types": ["PPTH", "PVBR", "PQTZ", "PWAV", "PWV2", "PCOB", "PCOB"],
            "beat_grid": [{"beat": 1, "bpm": 128.0, "time_ms": 336}, ...],
            "cue_points": [{"type": "hotcue", "hot_cue": 1, "time_ms": 336,
                            "label": None, "color_id": None}, ...],
        }

    Only ``.DAT`` is parsed (beat grid + basic cues). ``.EXT`` / ``.2EX``
    carry extended waveforms and colour cue labels that we don't surface
    in Prototype A.
    """
    dat = anlz_dir / "ANLZ0000.DAT"
    if not dat.exists():
        raise FileNotFoundError(f"ANLZ0000.DAT not found under {anlz_dir}")

    f = AnlzFile.parse_file(dat)
    # Beat grid — PQTZ.
    beats: list[dict[str, Any]] = []
    pqtz = f.get_tag("PQTZ")
    if pqtz is not None and hasattr(pqtz.content, "entries"):
        for entry in pqtz.content.entries:
            beats.append(
                {
                    # entry.beat is 1..4 (beat within the 4-beat bar),
                    # not a cumulative beat index.
                    "beat": int(entry.beat),
                    # tempo is BPM × 100
                    "bpm": float(entry.tempo) / 100.0,
                    "time_ms": int(entry.time),
                }
            )

    # Cues — PCOB (one per cue_type: hotcue, memory).
    cues: list[dict[str, Any]] = []
    for pcob in f.getall_tags("PCOB"):
        c = pcob.content
        cue_type = str(c.cue_type)
        for entry in c.entries:
            cues.append(
                {
                    "type": cue_type,
                    "entry_type": str(entry.type),
                    "hot_cue": int(entry.hot_cue),
                    "time_ms": int(entry.time),
                    "loop_time_ms": (
                        int(entry.loop_time)
                        if entry.loop_time != 0xFFFFFFFF
                        else None
                    ),
                    "label": None,  # stored in .EXT PCO2, out of scope
                    "color_id": None,
                }
            )

    return {
        "path": str(anlz_dir),
        "tag_types": list(f.tag_types),
        "beat_grid": beats,
        "cue_points": cues,
    }


def grid_summary_from_anlz(anlz_dir: Path) -> dict[str, Any] | None:
    """Return ``{beat_count, first_bpm, first_time_ms}`` or ``None`` if no PQTZ."""
    try:
        parsed = read_anlz_dir(anlz_dir)
    except (FileNotFoundError, OSError):
        return None
    beats = parsed.get("beat_grid") or []
    if not beats:
        return None
    first = beats[0]
    return {
        "beat_count": len(beats),
        "first_bpm": float(first["bpm"]),
        "first_time_ms": int(first["time_ms"]),
    }


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def _resolve_pioneer_root(pioneer_path: Path) -> Path:
    """Accept either ``.../PIONEER`` or its parent and return the PIONEER dir."""
    pioneer_path = pioneer_path.resolve()
    if pioneer_path.name.upper() == "PIONEER" and pioneer_path.is_dir():
        return pioneer_path
    child = pioneer_path / "PIONEER"
    if child.is_dir():
        return child
    return pioneer_path


def read_usb_export(pioneer_path: Path) -> dict[str, Any]:
    """Read a Pioneer export tree into a JSON-friendly dict.

    Parameters
    ----------
    pioneer_path
        Path to either a USB root that contains ``PIONEER/`` or the
        ``PIONEER`` directory itself.

    Returns
    -------
    dict
        See module docstring for shape. Key top-level fields:

        * ``tracks`` — list of track dicts (``id``, ``title``, ``artist``,
          ``album``, ``bpm``, ``key``, ``rating``, ``path``, ``anlz_path``, ...).
        * ``playlists`` — list of playlist dicts with nested ``track_ids``.
        * ``artists`` / ``albums`` / ``genres`` / ``keys`` / ``colors`` /
          ``labels`` — ``{id: name}`` lookup tables.
        * ``metadata`` — counts + coverage flags.
    """
    pioneer = _resolve_pioneer_root(pioneer_path)
    rekordbox_dir = pioneer / "rekordbox"
    anlz_dir = pioneer / "USBANLZ"

    pdb_path = rekordbox_dir / "export.pdb"
    ext_pdb_path = rekordbox_dir / "exportExt.pdb"
    one_lib_path = rekordbox_dir / "exportLibrary.db"

    if not pdb_path.exists():
        raise FileNotFoundError(f"export.pdb not found at {pdb_path}")

    pdb = _open_pdb(pdb_path)

    artists = _read_lookup(pdb, _PT.artists)
    albums = _read_lookup(pdb, _PT.albums)
    genres = _read_lookup(pdb, _PT.genres)
    keys = _read_lookup(pdb, _PT.keys)
    colors = _read_lookup(pdb, _PT.colors)
    labels = _read_lookup(pdb, _PT.labels)

    tracks = _read_tracks(pdb)
    playlists = _read_playlist_tree(pdb)
    entries = _read_playlist_entries(pdb)

    # Denormalize: attach artist/album/genre/key names and anlz path onto
    # each track; attach sorted track_ids onto each playlist.
    for t in tracks:
        t["artist"] = artists.get(t["artist_id"])
        t["album"] = albums.get(t["album_id"])
        t["genre"] = genres.get(t["genre_id"])
        t["key"] = keys.get(t["key_id"])
        # ``analyze_path`` points to ``.../P0xx/<hex>/ANLZ0000.DAT`` —
        # expose the directory portion so consumers can locate ANLZ files.
        ap = t.get("analyze_path")
        t["anlz_path"] = str(Path(ap).parent) if ap else None

    by_playlist: dict[int, list[tuple[int, int]]] = {}
    for e in entries:
        by_playlist.setdefault(e["playlist_id"], []).append(
            (e["entry_index"], e["track_id"])
        )
    for pl in playlists:
        ordered = by_playlist.get(pl["id"], [])
        ordered.sort(key=lambda x: x[0])
        pl["track_ids"] = [tid for _, tid in ordered]

    # ANLZ coverage summary.
    anlz_meta = _anlz_summary(anlz_dir)

    return {
        "tracks": tracks,
        "playlists": playlists,
        "playlist_entries": entries,
        "artists": {str(k): v for k, v in artists.items()},
        "albums": {str(k): v for k, v in albums.items()},
        "genres": {str(k): v for k, v in genres.items()},
        "keys": {str(k): v for k, v in keys.items()},
        "colors": {str(k): v for k, v in colors.items()},
        "labels": {str(k): v for k, v in labels.items()},
        "metadata": {
            "pioneer_path": str(pioneer),
            "total_tracks": len(tracks),
            "total_playlists": len(playlists),
            "total_playlist_entries": len(entries),
            "total_artists": len(artists),
            "total_albums": len(albums),
            "total_genres": len(genres),
            "has_pdb": pdb_path.exists(),
            "has_extended_pdb": ext_pdb_path.exists(),
            "has_onelibrary": one_lib_path.exists(),
            "onelibrary_decrypted": False,  # see module docstring
            "onelibrary_blocker": (
                "exportLibrary.db is SQLCipher-encrypted with a key not "
                "shipped in pyrekordbox 0.4.4; skipped in Prototype A."
                if one_lib_path.exists()
                else None
            ),
            "anlz_total_dirs": anlz_meta["total_dirs"],
            "anlz_tag_coverage": anlz_meta["tag_counts"],
        },
    }


# ---------------------------------------------------------------------------
# Validation invariants (used by CLI --validate and tests)
# ---------------------------------------------------------------------------


def validate_invariants(data: dict[str, Any]) -> list[str]:
    """Return a list of invariant failure messages (empty = all pass).

    Invariants checked (CAT-06):

    1. Every playlist entry references a real track ID.
    2. Every non-folder playlist's ``track_ids`` are all real track IDs.
    3. Every track's ``rating`` is in ``0..=5``.
    4. Every track's ``bpm`` (if set) is in ``30..=300``.
    5. Every track has a corresponding ANLZ directory (parity check
       between ``total_tracks`` and ``anlz_total_dirs``).
    6. If OneLibrary exists, PDB counts must still be non-zero (else the
       export would be empty).
    """
    errors: list[str] = []
    track_ids = {t["id"] for t in data["tracks"]}

    # 1. + 2.
    missing_entries = [
        e for e in data["playlist_entries"] if e["track_id"] not in track_ids
    ]
    if missing_entries:
        errors.append(
            f"{len(missing_entries)} playlist entries reference unknown track IDs "
            f"(first: {missing_entries[0]})"
        )
    for pl in data["playlists"]:
        if pl.get("is_folder"):
            continue
        unknown = [tid for tid in pl["track_ids"] if tid not in track_ids]
        if unknown:
            errors.append(
                f"playlist {pl['id']} ({pl['name']!r}) has {len(unknown)} unknown track IDs"
            )

    # 3. + 4.
    for t in data["tracks"]:
        if not (0 <= t["rating"] <= 5):
            errors.append(f"track {t['id']} has out-of-range rating {t['rating']}")
        if t["bpm"] is not None and not (30.0 <= t["bpm"] <= 300.0):
            errors.append(f"track {t['id']} has out-of-range bpm {t['bpm']}")

    # 5.
    coverage = data["metadata"]["anlz_tag_coverage"]
    if coverage:  # only enforce when USBANLZ/ is present
        total_anlz = data["metadata"]["anlz_total_dirs"]
        if total_anlz < len(data["tracks"]):
            errors.append(
                f"ANLZ directory count {total_anlz} < track count "
                f"{len(data['tracks'])}; some tracks missing analysis"
            )

    # 6.
    if data["metadata"]["has_onelibrary"] and not data["tracks"]:
        errors.append("OneLibrary present but no tracks parsed from PDB")

    return errors
