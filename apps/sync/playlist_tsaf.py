"""TSAF playlist-blob + YapDatabase page-data primitives for Phase 3.

These helpers let ``apps.sync.playlist_apply`` write into djay's
``MediaLibrary.db`` without having the live leaf-playlist fixture captured
yet. The page-data side is *exact* (simple int64-LE pack) and round-trip
tested. The playlist-blob side is best-effort against
``docs/djay_db_schema_v2.md`` section 3.4, with the ``leaf`` type enum byte
marked as a capture-me-live TODO.

Contract:

* :func:`build_page_data` / :func:`parse_page_data` are exact inverses and
  are the sole supported serialisation for ``view_mediaItemPlaylistView_page.data``.
* :func:`new_page_key` mints a fresh opaque YapDatabase identifier.
* :func:`build_playlist_blob` emits a minimal ``ADCMediaItemPlaylist`` TSAF
  blob. ``kind="root"`` uses the observed ``0x2d 0x00`` enum; ``kind="leaf"``
  uses ``0x2d <PLAYLIST_TYPE_LEAF>`` where the constant is currently set to
  a best guess (``0x01``) and MUST be replaced by the value harvested from
  a live djay fixture before production use. Unit tests monkeypatch the
  constant to validate the byte layout around it.

See also ``scripts/capture_djay_playlist_fixture.py`` which emits a live
blob into ``tests/fixtures/djay/leaf_playlist.blob`` + a sidecar for the
matching ``view_mediaItemPlaylistView_page.data`` row.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import struct
import uuid as _uuid
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)


# ----- Page-data packer --------------------------------------------------


def build_page_data(rowids: list[int]) -> bytes:
    """Pack ``rowids`` as a little-endian int64 array (schema v2 section 4)."""
    if not rowids:
        return b""
    return struct.pack(f"<{len(rowids)}q", *rowids)


def parse_page_data(data: bytes | None) -> list[int]:
    """Inverse of :func:`build_page_data`. Returns ``[]`` on malformed input."""
    if not data:
        return []
    n = len(data)
    if n % 8 != 0:
        return []
    count = n // 8
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}q", data))


def new_page_key() -> str:
    """Mint a fresh opaque YapDatabase ``pageKey`` (uuid4 hex)."""
    return _uuid.uuid4().hex


# ----- Playlist TSAF blob builder ---------------------------------------


_TSAF_MAGIC = b"TSAF"
_PLAYLIST_CLASS = b"ADCMediaItemPlaylist"

#: Observed root-folder ``type`` enum byte (schema v2 section 3.4).
PLAYLIST_TYPE_ROOT = 0x00

#: Leaf playlist type -- best-effort placeholder. Replace with the byte
#: harvested from a live djay fixture (see
#: ``scripts/capture_djay_playlist_fixture.py``). Until then
#: :func:`apps.sync.playlist_apply._apply_single_op` refuses live
#: ``create`` ops unless the caller overrides via ``--leaf-type-byte``
#: (enforced when ``apply_plan`` is invoked with ``live=True``).
PLAYLIST_TYPE_LEAF = 0x01


def _emit_string(s: str) -> bytes:
    """Emit ``0x08 <utf8> 0x00``."""
    return b"\x08" + s.encode("utf-8") + b"\x00"


def _emit_enum_byte(value: int) -> bytes:
    """Emit ``0x2d <byte>`` (2 bytes)."""
    return b"\x2d" + bytes([value & 0xFF])


def _emit_tsaf_header(field_count: int) -> bytes:
    """Emit a 16-byte TSAF header.

    Matches the offset :func:`apps.shared.djay_db._tsaf_kv` uses when it
    skips past the magic/version/flags block (index 16) before scanning
    for ``0x08``-prefixed string pairs.
    """
    return (
        _TSAF_MAGIC
        + bytes([0x03, 0x03, field_count & 0xFF, 0x00])
        + b"\x00" * 8
    )


def build_playlist_blob(
    uuid_hex: str,
    name: str,
    kind: Literal["root", "leaf"] = "leaf",
    *,
    leaf_type_byte: int | None = None,
) -> bytes:
    """Build an ``ADCMediaItemPlaylist`` TSAF blob (uuid / name / type)."""
    if not uuid_hex:
        raise ValueError("uuid_hex must be non-empty")
    if kind == "root":
        type_byte = PLAYLIST_TYPE_ROOT
    else:
        type_byte = PLAYLIST_TYPE_LEAF if leaf_type_byte is None else leaf_type_byte & 0xFF

    class_marker = b"\x2b" + _emit_string(_PLAYLIST_CLASS.decode("utf-8"))
    pairs = (
        _emit_string(uuid_hex) + _emit_string("uuid")
        + _emit_string(name) + _emit_string("name")
        + _emit_enum_byte(type_byte) + _emit_string("type")
    )
    body = class_marker + pairs + b"\x00"
    return _emit_tsaf_header(field_count=3) + body


def parse_playlist_blob(blob: bytes) -> dict[str, object]:
    """Minimal round-trip reader for :func:`build_playlist_blob` output."""
    from apps.shared.djay_db import _tsaf_kv

    if len(blob) < 16 or blob[:4] != _TSAF_MAGIC:
        return {}

    out: dict[str, object] = {}
    kv = _tsaf_kv(blob)
    if "uuid" in kv:
        out["uuid"] = kv["uuid"]
    if "name" in kv:
        out["name"] = kv["name"]

    needle = b"\x08type\x00"
    p = blob.find(needle)
    if p >= 2 and blob[p - 2] == 0x2D:
        out["type"] = blob[p - 1]
    return out


# ----- Startup validation ------------------------------------------------


class TSAFLeafTypeMismatch(RuntimeError):
    """Raised when ``PLAYLIST_TYPE_LEAF`` disagrees with a live djay fixture."""


def _discover_leaf_type_from_db(db_path: Path) -> int | None:
    """Scan ``djayMediaLibrary_playlist`` blobs for an observed leaf type byte.

    Returns the most common ``0x2d <byte>`` enum found adjacent to the ``type``
    key in non-root playlist blobs, or ``None`` if no candidate rows exist
    (empty library, schema mismatch, etc.). Read-only; never mutates the DB.
    """
    if not db_path.exists():
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE '%laylist%'"
            )
            tables = [r[0] for r in cur.fetchall()]
            if not tables:
                return None
            needle = b"\x08type\x00"
            counts: dict[int, int] = {}
            for tbl in tables:
                try:
                    rows = conn.execute(
                        f'SELECT data FROM "{tbl}" WHERE data IS NOT NULL LIMIT 500'
                    ).fetchall()
                except sqlite3.DatabaseError:
                    continue
                for (blob,) in rows:
                    if not isinstance(blob, (bytes, bytearray)):
                        continue
                    p = bytes(blob).find(needle)
                    if p >= 2 and blob[p - 2] == 0x2D:
                        b = blob[p - 1]
                        if b == PLAYLIST_TYPE_ROOT:
                            continue
                        counts[b] = counts.get(b, 0) + 1
            if not counts:
                return None
            return max(counts.items(), key=lambda kv: kv[1])[0]
    except sqlite3.DatabaseError as exc:
        _log.warning("TSAF validation: could not open %s: %s", db_path, exc)
        return None


def validate_leaf_type_byte(
    db_path: Path | str | None = None,
    *,
    skip: bool | None = None,
) -> int | None:
    """Assert ``PLAYLIST_TYPE_LEAF`` matches the observed value in a live DB.

    Parameters
    ----------
    db_path:
        Path to a live djay ``MediaLibrary.db`` / Rekordbox ``master.db``
        (whichever exposes playlist TSAF blobs). When ``None``, no validation
        is attempted and the function logs a warning.
    skip:
        When truthy, skip validation entirely. When ``None``, read the
        ``MDJ_SKIP_TSAF_VALIDATION`` env var as a fallback for CLIs that pass
        ``--skip-tsaf-validation``.

    Returns
    -------
    The observed byte when validation ran and agreed, otherwise ``None``.

    Raises
    ------
    TSAFLeafTypeMismatch
        If a live DB was readable AND exposed at least one non-root playlist
        blob AND that blob's type byte disagrees with ``PLAYLIST_TYPE_LEAF``.
    """
    if skip is None:
        skip = bool(os.environ.get("MDJ_SKIP_TSAF_VALIDATION"))
    if skip:
        _log.info("TSAF validation: skipped via flag/env")
        return None
    if db_path is None:
        _log.warning(
            "TSAF validation: no db_path supplied; PLAYLIST_TYPE_LEAF=0x%02x "
            "remains a best-guess until a live fixture is available",
            PLAYLIST_TYPE_LEAF,
        )
        return None
    observed = _discover_leaf_type_from_db(Path(db_path))
    if observed is None:
        _log.warning(
            "TSAF validation: no non-root playlist blobs found in %s; "
            "PLAYLIST_TYPE_LEAF=0x%02x unverified",
            db_path,
            PLAYLIST_TYPE_LEAF,
        )
        return None
    if observed != PLAYLIST_TYPE_LEAF:
        raise TSAFLeafTypeMismatch(
            f"PLAYLIST_TYPE_LEAF constant (0x{PLAYLIST_TYPE_LEAF:02x}) "
            f"disagrees with observed value 0x{observed:02x} in "
            f"{db_path}. Update apps/sync/playlist_tsaf.py or pass "
            f"--skip-tsaf-validation for offline/dev."
        )
    return observed


__all__ = [
    "PLAYLIST_TYPE_ROOT",
    "PLAYLIST_TYPE_LEAF",
    "TSAFLeafTypeMismatch",
    "build_page_data",
    "parse_page_data",
    "new_page_key",
    "build_playlist_blob",
    "parse_playlist_blob",
    "validate_leaf_type_byte",
]
