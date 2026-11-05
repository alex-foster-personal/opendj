"""VUSB-01..08 virtual USB, pure half: ids, the lender's shares file, and the shapes.

The lender marks playlists as shared (a JSON file under its data dir, never state.db) and
exports them read-only. The borrower re-serves that export as a virtual stick in the Play
from USB wire shape (``UsbStickLibraryOut``), so the stick browser, panes and deck-load
path work unchanged. Spec: docs/b2b-link/VIRTUAL-USB.md.

Ids (all derived, none invented):

- ``lender_id``: 12 random hex, created once per data dir and kept in
  ``state/peer-usb-lender-id`` (never derived from the renamable machine label).
- volume id ``peer:<lender_id>``, volume uuid ``peer-<lender_id>``.
- track id ``usb-peer-<lender_id>-<lender_stable_id>``. Stick ids are
  ``usb-<UPPERCASE-UUID>-<n>`` and library ids are lowercase sha1 hex, so neither can
  start ``usb-peer-``.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The lender's shares file, relative to its data dir.
SHARES_RELPATH = Path("state") / "peer-usb-shares.json"
#: This laptop's lender id, relative to its data dir.
LENDER_ID_RELPATH = Path("state") / "peer-usb-lender-id"
SHARES_VERSION = 1
LENDER_ID_HEX = 12
PEER_VOLUME_PREFIX = "peer:"
PEER_UUID_PREFIX = "peer-"
PEER_TRACK_PREFIX = "usb-peer-"
_LENDER_ID_RE = re.compile(rf"^[0-9a-f]{{{LENDER_ID_HEX}}}$")
#: A lender stable id as it may appear inside a borrowed track id or a URL path.
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TRACK_ID_RE = re.compile(rf"^usb-peer-([0-9a-f]{{{LENDER_ID_HEX}}})-([A-Za-z0-9_-]{{1,128}})$")
#: Serializes every read-modify-write of the lender's files (shares, lender id) in this engine.
_FILES_LOCK = threading.Lock()


class PeerUsbError(ValueError):
    """A typed refusal: ``code`` is the wire ``detail.code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- ids


def lender_id(data_dir: Path) -> str:
    """This laptop's lender id: random, created on first use and kept in the data dir, so
    two laptops with one label never share a borrower's cache namespace and a rename of
    the DJ name keeps every borrower's cache. A malformed file raises, never regenerates."""
    path = data_dir / LENDER_ID_RELPATH
    with _FILES_LOCK:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if not _LENDER_ID_RE.fullmatch(value):
                raise PeerUsbError("VUSB_LENDER_ID_MALFORMED", f"{path} does not hold {LENDER_ID_HEX} lowercase hex")
            return value
        value = secrets.token_hex(LENDER_ID_HEX // 2)
        atomic_write_text(path, value + "\n")
        return value


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a unique temp file in the same directory, then rename into place, so two
    concurrent writers never share (or move) one temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # newline="\n": the same bytes on every OS (no \r\n translation on Windows).
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def peer_volume_id(lender_id: str) -> str:
    return f"{PEER_VOLUME_PREFIX}{lender_id}"


def peer_volume_uuid(lender_id: str) -> str:
    return f"{PEER_UUID_PREFIX}{lender_id}"


def lender_id_from_volume_id(volume_id: str) -> str:
    lender_id = volume_id.removeprefix(PEER_VOLUME_PREFIX)
    if not volume_id.startswith(PEER_VOLUME_PREFIX) or not _LENDER_ID_RE.fullmatch(lender_id):
        raise PeerUsbError("VUSB_VOLUME_ID_INVALID", f"{volume_id!r} is not a peer volume id (peer:<12 hex>)")
    return lender_id


def peer_track_id(lender_id: str, stable_id: str) -> str:
    require_stable_id(stable_id)
    return f"{PEER_TRACK_PREFIX}{lender_id}-{stable_id}"


def parse_peer_track_id(track_id: str) -> tuple[str, str]:
    """``(lender_id, lender_stable_id)``, or ``VUSB_TRACK_ID_INVALID``."""
    match = _TRACK_ID_RE.fullmatch(track_id)
    if match is None:
        raise PeerUsbError("VUSB_TRACK_ID_INVALID", f"{track_id!r} is not a peer track id (usb-peer-<12 hex>-<id>)")
    return match.group(1), match.group(2)


def is_lendable_stable_id(stable_id: str) -> bool:
    """Only ids that are safe as a cache directory and inside a peer track id are lent (a
    ``spotify-pending:<sha1>`` row, with its colon, is not)."""
    return bool(_STABLE_ID_RE.fullmatch(stable_id))


def require_stable_id(stable_id: str) -> str:
    if not _STABLE_ID_RE.fullmatch(stable_id):
        raise PeerUsbError("VUSB_TRACK_ID_INVALID", f"{stable_id!r} is not a safe stable id")
    return stable_id


def normalize_hash(value: str) -> str:
    """``sha256:<hex>`` and bare hex compare equal."""
    return value.removeprefix("sha256:").lower()


# ---------------------------------------------------------------- shares file


@dataclass(frozen=True)
class Share:
    playlist_id: str
    shared_at: str


def shares_path(data_dir: Path) -> Path:
    return data_dir / SHARES_RELPATH


def read_shares(data_dir: Path) -> list[Share]:
    """Shared playlists in the order they were shared. A missing file is "none shared";
    a malformed one raises (a lender must never export a guess)."""
    path = shares_path(data_dir)
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != SHARES_VERSION or not isinstance(doc.get("shares"), list):
        raise PeerUsbError("VUSB_SHARES_MALFORMED", f"{path} is not a version {SHARES_VERSION} shares file")
    shares: list[Share] = []
    for entry in doc["shares"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("playlist_id"), str) or not isinstance(entry.get("shared_at"), str):
            raise PeerUsbError("VUSB_SHARES_MALFORMED", f"{path} has a malformed entry: {entry!r}")
        shares.append(Share(entry["playlist_id"], entry["shared_at"]))
    return shares


def _write_shares(data_dir: Path, shares: list[Share]) -> None:
    body = {"version": SHARES_VERSION, "shares": [{"playlist_id": s.playlist_id, "shared_at": s.shared_at} for s in shares]}
    atomic_write_text(shares_path(data_dir), json.dumps(body, indent=2, sort_keys=True))


def add_share(data_dir: Path, playlist_id: str) -> list[Share]:
    """Idempotent: sharing a shared playlist keeps its original ``shared_at``. The whole
    read-modify-write holds ``_FILES_LOCK``, so concurrent lends never lose one."""
    with _FILES_LOCK:
        shares = read_shares(data_dir)
        if all(s.playlist_id != playlist_id for s in shares):
            shares.append(Share(playlist_id, datetime.now(UTC).isoformat()))
            _write_shares(data_dir, shares)
        return shares


def remove_share(data_dir: Path, playlist_id: str) -> list[Share]:
    with _FILES_LOCK:
        shares = read_shares(data_dir)
        kept = [s for s in shares if s.playlist_id != playlist_id]
        if len(kept) == len(shares):
            raise PeerUsbError("VUSB_NOT_SHARED", f"playlist {playlist_id} is not shared")
        _write_shares(data_dir, kept)
        return kept


# ---------------------------------------------------------------- borrower shape


def _require(doc: dict[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, kind):
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where}.{key} is {value!r}, expected {kind}")
    return value


def _nullable(doc: dict[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    """A wire field that must be PRESENT and may be null: an absent key is a drifted lender,
    never silently read as null (that would quietly turn off verification and dedup)."""
    if key not in doc:
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where}.{key} is missing")
    if doc[key] is None:
        return None
    return _require(doc, key, kind, where)


_ROW_FIELDS: dict[str, type | tuple[type, ...]] = {
    "title": str, "artist": str, "album": str, "genre": str, "key": str, "bpm": (int, float),
    "duration_s": (int, float), "rating": int, "content_hash": str, "audio_hash": str, "size_bytes": int,
}


def validate_track_row(track: Any, where: str) -> dict[str, Any]:
    """One lender export row, every field present and type-checked (a bad row is refused
    before it is cached, so it can never poison later loads). Unknown fields are dropped."""
    if not isinstance(track, dict):
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where} is not an object")
    row: dict[str, Any] = {"stable_id": require_stable_id(_require(track, "stable_id", str, where))}
    for key, kind in _ROW_FIELDS.items():
        row[key] = _nullable(track, key, kind, where)
    if row["duration_s"] is not None:
        row["duration_s"] = float(row["duration_s"])
    if row["size_bytes"] is not None and row["size_bytes"] < 0:
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where}.size_bytes is negative")
    # The ranges the stick wire enforces (usb-row-wire.ts): a value it would refuse is
    # refused here, before the export is saved, so it can never make the stick unopenable.
    if row["rating"] is not None and not 0 <= row["rating"] <= 5:
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where}.rating {row['rating']} is not 0-5")
    for key in ("bpm", "duration_s"):
        if row[key] is not None and not (math.isfinite(row[key]) and row[key] >= 0):
            raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where}.{key} {row[key]} is not a finite non-negative number")
    return row


def _stick_track(lender_id: str, i: int, row: dict[str, Any]) -> dict[str, Any]:
    """One validated export row in the stick track shape (``UsbStickTrackOut``)."""
    return {
        "id": peer_track_id(lender_id, row["stable_id"]),
        "pdb_id": i + 1,
        "title": row["title"] or "",
        "artist": row["artist"],
        "album": row["album"],
        "genre": row["genre"],
        "key": row["key"],
        "bpm": row["bpm"],
        "duration_s": row["duration_s"],
        "rating": row["rating"] or 0,
        "play_count": 0,
        "file_path": row["stable_id"],
        "file_present": True,
        "has_analysis": True,
        "has_artwork": False,
        "date_added": None,
        # USBPLAY-18 wire: the lender's own analysis travels with the track (the anlz
        # route), like a stick's own ANLZ, so the browser treats it as "stick".
        "analysis_state": "stick",
        "analysis_reason": None,
    }


def _stick_playlist(lender_id: str, i: int, playlist: Any, known: set[str]) -> dict[str, Any]:
    """One export playlist in the stick playlist shape; every id must be an exported track."""
    where = f"export.playlists[{i}]"
    if not isinstance(playlist, dict):
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where} is not an object")
    members = _require(playlist, "track_ids", list, where)
    if not all(isinstance(sid, str) for sid in members):
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where}.track_ids holds a non-string member")
    track_ids = [peer_track_id(lender_id, require_stable_id(sid)) for sid in members]
    stray = [tid for tid in track_ids if tid not in known]
    if stray:
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"{where} lists {stray[0]}, not in the export's tracks")
    return {
        "id": f"pl-{i + 1}",
        "pdb_id": i + 1,
        "name": _require(playlist, "name", str, where),
        "parent_id": None,
        "is_folder": False,
        "sort_order": i,
        "track_ids": track_ids,
    }


def stick_library_from_export(export: dict[str, Any], *, in_library: set[str], read_ms: float) -> dict[str, Any]:
    """The lender's export as a ``UsbStickLibraryOut`` document for the virtual stick.

    ``in_library`` holds the normalized hashes (bare lowercase hex) the borrower already
    owns; a track whose content or audio hash is among them counts as ``in_your_library``.
    Validates every field the stick browser relies on: a guessed row would load the wrong
    audio onto a deck.
    """
    lender_id = _require(export, "lender_id", str, "export")
    if not _LENDER_ID_RE.fullmatch(lender_id):
        raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"export.lender_id {lender_id!r} is not 12 hex")
    name = _require(export, "name", str, "export")
    rows = [validate_track_row(track, f"export.tracks[{i}]") for i, track in enumerate(_require(export, "tracks", list, "export"))]
    seen: set[str] = set()
    for row in rows:
        if row["stable_id"] in seen:
            raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"export.tracks lists {row['stable_id']} twice")
        seen.add(row["stable_id"])
    owned = sum(any(normalize_hash(h) in in_library for h in (row["content_hash"], row["audio_hash"]) if h) for row in rows)
    tracks_out = [_stick_track(lender_id, i, row) for i, row in enumerate(rows)]
    known = {t["id"] for t in tracks_out}
    playlists_out = [_stick_playlist(lender_id, i, playlist, known) for i, playlist in enumerate(_require(export, "playlists", list, "export"))]
    return {
        "volume_id": peer_volume_id(lender_id),
        "volume_uuid": peer_volume_uuid(lender_id),
        "name": name,
        "mount_path": f"peer://{lender_id}",
        "tracks": tracks_out,
        "playlists": playlists_out,
        "history": [],
        "counts": {
            "tracks": len(tracks_out),
            "playlists": len(playlists_out),
            "playlist_entries": sum(len(p["track_ids"]) for p in playlists_out),
            "history_playlists": 0,
        },
        "read_ms": read_ms,
        "cache_hit": False,
        # The lender's library export, never a folder walk: nothing is cut short.
        "source": "export_pdb",
        "truncated": None,
        "in_your_library": owned,
    }


def validate_export(export: dict[str, Any]) -> None:
    """The whole export, validated as the stick library would read it (lender id, every
    track row, every playlist and its memberships, and counts that match them), or
    ``VUSB_EXPORT_MALFORMED``. Run
    before an export is saved, so a malformed one never replaces the last good one."""
    doc = stick_library_from_export(export, in_library=set(), read_ms=0.0)
    _require(export, "revision", str, "export")
    counts = _require(export, "counts", dict, "export")
    for key in ("playlists", "tracks"):
        stated = _require(counts, key, int, "export.counts")
        if stated != doc["counts"][key]:
            raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"export.counts.{key} is {stated}, but the export carries {doc['counts'][key]}")


def validate_export_summary(head: dict[str, Any]) -> None:
    """The summary export's required fields (name, counts.playlists, counts.tracks), or
    ``VUSB_EXPORT_MALFORMED``: a drifted lender is refused, never shown as a plausible stick."""
    _require(head, "name", str, "export")
    _require(head, "revision", str, "export")
    counts = _require(head, "counts", dict, "export")
    for key in ("playlists", "tracks"):
        if _require(counts, key, int, "export.counts") < 0:
            raise PeerUsbError("VUSB_EXPORT_MALFORMED", f"export.counts.{key} is negative")


def export_track(export: dict[str, Any], stable_id: str) -> dict[str, Any]:
    """One validated track row of an export, or ``VUSB_NOT_SHARED``."""
    for i, track in enumerate(_require(export, "tracks", list, "export")):
        if isinstance(track, dict) and track.get("stable_id") == stable_id:
            return validate_track_row(track, f"export.tracks[{i}]")
    raise PeerUsbError("VUSB_NOT_SHARED", f"the peer does not lend track {stable_id}")
