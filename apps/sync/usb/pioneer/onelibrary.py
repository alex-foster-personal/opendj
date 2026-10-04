"""Own OneLibrary (``exportLibrary.db``) handle over SQLCipher.

Rekordbox 7 writes a *Device Library Plus* / OneLibrary database next to the
classic ``export.pdb`` on CDJ-3000X / OPUS-QUAD / XDJ-AZ sticks. It is an
ordinary SQLite schema inside SQLCipher 4 with a fixed, publicly documented
key. This module opens it with ``sqlcipher3`` (zlib license, already in the
dependency tree through pyrekordbox) and exposes the small surface the USB
export, differ and value-verify code needs.

It replaces the ``rbox`` Rust wheel, which relicensed from MIT/Apache-2.0 to
GPL-3.0-only at 0.1.6. Nothing here is derived from rbox's GPL releases. The
table and column names come from the database itself (``PRAGMA table_info``)
and from pyrekordbox's MIT-licensed ``devicelib_plus`` models; the key blob
is pyrekordbox's (MIT, Copyright (c) 2022-2025 Dylan Jones), decoded with
the ``deobfuscate`` helper of the pyrekordbox release we already depend on.

Row dicts use short snake_case keys so callers stay schema-agnostic:
``<table>_id`` becomes ``id``, ``sequenceNo`` becomes ``seq``,
``playlist_id_parent`` becomes ``parent_id``, ``artist_id_<role>`` becomes
``<role>_id`` (``artist_id_artist`` stays ``artist_id``), and every other
camelCase column becomes snake_case (``djComment`` -> ``dj_comment``,
``fileName`` -> ``file_name``). Columns the module does not know about are
still returned, under the same rule.

Sequence numbers order siblings per parent (playlists) or per playlist
(playlist contents), 1-based unless the file already numbers from 0.
``seq=None`` appends at ``max + 1``; any other value inserts at that
position and shifts later siblings down by one, and a value past the end is
rejected rather than leaving a gap. rbox 0.1.7 appended at ``count``, which
repeats the last sibling's number in a 1-based file (measured on the
synthetic export in ``tests/sync/usb/onelibrary_synth.py``).
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

try:  # pragma: no cover - import guard exercised by the availability flag.
    from sqlcipher3 import dbapi2 as _sqlcipher

    SQLCIPHER_AVAILABLE = True
    SQLCIPHER_IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - any import failure means unavailable.
    _sqlcipher = None  # type: ignore[assignment]
    SQLCIPHER_AVAILABLE = False
    SQLCIPHER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


__all__ = [
    "BACKEND",
    "SQLCIPHER_AVAILABLE",
    "SQLCIPHER_IMPORT_ERROR",
    "OneLibrary",
    "OneLibraryError",
    "onelibrary_key",
]

#: Reported in write receipts and CLI JSON where ``rbox_version`` used to be.
BACKEND = "odj-onelibrary-sqlcipher"

# pyrekordbox (MIT) ``devicelib_plus.database.BLOB``: the OneLibrary key,
# obfuscated the same way pyrekordbox ships its master.db key.
_KEY_BLOB = (
    b"PN_1dH8$oLJY)16j_RvM6qphWw`476>;C1cWmI#se(PG`j}~xAjlufj?`#0i{;=glh"
    b"(SkW)y0>n?YEiD`l%t("
)

_ARTIST_ROLE = re.compile(r"^artist_id_(\w+)$")
_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")


class OneLibraryError(RuntimeError):
    """Raised when a OneLibrary database cannot be opened, read or written."""


def onelibrary_key() -> str:
    """Return the SQLCipher key every Rekordbox OneLibrary export uses."""
    from pyrekordbox.utils import deobfuscate

    return deobfuscate(_KEY_BLOB)


def _key_for(table: str, column: str) -> str:
    if column == f"{table}_id":
        return "id"
    if column == "sequenceNo":
        return "seq"
    if table == "playlist" and column == "playlist_id_parent":
        return "parent_id"
    role = _ARTIST_ROLE.match(column)
    if role:
        name = role.group(1)
        return "artist_id" if name == "artist" else f"{_snake(name)}_id"
    return _snake(column)


def _snake(name: str) -> str:
    return _CAMEL.sub(r"_\1", name).lower()


class OneLibrary:
    """A read/write handle on one ``exportLibrary.db``.

    Open it on a scratch copy: writes commit immediately, and a plain open
    may checkpoint a ``-wal`` sidecar into the main file. ``readonly=True``
    opens with ``mode=ro`` so a probe cannot write at all.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        key: str | None = None,
        readonly: bool = False,
    ) -> None:
        if not SQLCIPHER_AVAILABLE:
            raise OneLibraryError(f"sqlcipher3 is unavailable: {SQLCIPHER_IMPORT_ERROR}")
        db_path = Path(path)
        if not db_path.is_file():
            raise OneLibraryError(f"OneLibrary not found: {db_path}")
        self.path = db_path
        uri = db_path.resolve().as_uri() + ("?mode=ro" if readonly else "")
        try:
            conn = _sqlcipher.connect(uri, uri=True)
            escaped = (key or onelibrary_key()).replace("'", "''")
            conn.execute(f"PRAGMA key = '{escaped}'")
            # Fails with "file is not a database" on a wrong key or a
            # non-SQLCipher file, which is the signal we want at open time.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception as exc:
            raise OneLibraryError(f"cannot open OneLibrary {db_path}: {exc}") from exc
        self.conn = conn
        self._columns: dict[str, list[str]] = {}
        for table in ("content", "playlist", "playlist_content"):
            if not self._table_columns(table):
                self.close()
                raise OneLibraryError(
                    f"{db_path} is not a OneLibrary export: table {table!r} is missing"
                )

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()
            self.conn = None  # type: ignore[assignment]

    def __enter__(self) -> OneLibrary:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - GC timing is not testable.
        with contextlib.suppress(Exception):
            self.close()

    # -- schema helpers ------------------------------------------------

    def _table_columns(self, table: str) -> list[str]:
        if table not in self._columns:
            rows = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            self._columns[table] = [str(r[1]) for r in rows]
        return self._columns[table]

    def has_table(self, table: str) -> bool:
        return bool(self._table_columns(table))

    def _rows(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        columns = self._table_columns(table)
        if not columns:
            raise OneLibraryError(f"table {table!r} is not in {self.path}")
        quoted = ", ".join(f'"{c}"' for c in columns)
        order = f'"{table}_id"' if f"{table}_id" in columns else "rowid"
        sql = f'SELECT {quoted} FROM "{table}" {where} ORDER BY {order}'
        keys = [_key_for(table, c) for c in columns]
        return [dict(zip(keys, row, strict=True)) for row in self.conn.execute(sql, params)]

    def _one(self, table: str, row_id: int) -> dict[str, Any] | None:
        rows = self._rows(table, f'WHERE "{table}_id" = ?', (int(row_id),))
        return rows[0] if rows else None

    # -- reads ---------------------------------------------------------

    def get_contents(self) -> list[dict[str, Any]]:
        return self._rows("content")

    def get_content_by_id(self, content_id: int) -> dict[str, Any] | None:
        return self._one("content", content_id)

    def get_playlists(self) -> list[dict[str, Any]]:
        return self._rows("playlist")

    def get_playlist_by_id(self, playlist_id: int) -> dict[str, Any] | None:
        return self._one("playlist", playlist_id)

    def get_playlist_contents(self, playlist_id: int) -> list[dict[str, Any]]:
        """Content rows of one playlist, in playlist order."""
        ids = [
            int(r[0])
            for r in self.conn.execute(
                "SELECT content_id FROM playlist_content WHERE playlist_id = ? "
                'ORDER BY "sequenceNo"',
                (int(playlist_id),),
            )
        ]
        out: list[dict[str, Any]] = []
        for cid in ids:
            row = self.get_content_by_id(cid)
            if row is not None:
                out.append(row)
        return out

    def get_artists(self) -> list[dict[str, Any]]:
        return self._rows("artist")

    def get_albums(self) -> list[dict[str, Any]]:
        return self._rows("album")

    def get_genres(self) -> list[dict[str, Any]]:
        return self._rows("genre")

    def get_keys(self) -> list[dict[str, Any]]:
        return self._rows("key")

    def get_labels(self) -> list[dict[str, Any]]:
        return self._rows("label")

    def get_colors(self) -> list[dict[str, Any]]:
        return self._rows("color")

    def iter_table(self, table: str) -> Iterator[dict[str, Any]]:
        yield from self._rows(table)

    # -- writes --------------------------------------------------------

    def update_content(self, content: Mapping[str, Any]) -> None:
        """Write every known field of ``content`` back to its row."""
        if "id" not in content:
            raise OneLibraryError("update_content needs the row's 'id'")
        by_key = {_key_for("content", c): c for c in self._table_columns("content")}
        unknown = sorted(k for k in content if k not in by_key)
        if unknown:
            raise OneLibraryError(f"unknown content field(s): {', '.join(unknown)}")
        sets = [(by_key[k], v) for k, v in content.items() if k != "id"]
        if not sets:
            return
        assignments = ", ".join(f'"{col}" = ?' for col, _ in sets)
        with self.conn:
            cur = self.conn.execute(
                f'UPDATE content SET {assignments} WHERE "content_id" = ?',
                [v for _, v in sets] + [int(content["id"])],
            )
        if cur.rowcount != 1:
            raise OneLibraryError(f"content id={content['id']} not present")

    def _seq_base(self, table: str) -> int:
        """1, unless the file already numbers this table from 0."""
        low = self.conn.execute(f'SELECT min("sequenceNo") FROM "{table}"').fetchone()[0]
        return 0 if low == 0 else 1

    def _place(self, table: str, scope_col: str, scope: int, seq: int | None) -> int:
        """Return the slot for a new row, shifting later siblings if inserting.

        Appending takes ``max + 1`` within the scope, so it never repeats an
        existing number even when the file's numbering has gaps.
        """
        base = self._seq_base(table)
        top = self.conn.execute(
            f'SELECT max("sequenceNo") FROM "{table}" WHERE "{scope_col}" = ?', (scope,)
        ).fetchone()[0]
        end = base if top is None else int(top) + 1
        if seq is None or (seq == 0 and base == 1):
            return end
        if seq < base or seq > end:
            raise OneLibraryError(
                f"invalid sequence number {seq} for {table} {scope_col}={scope} "
                f"(valid: {base}..{end}, or None to append)"
            )
        self.conn.execute(
            f'UPDATE "{table}" SET "sequenceNo" = "sequenceNo" + 1 '
            f'WHERE "{scope_col}" = ? AND "sequenceNo" >= ?',
            (scope, seq),
        )
        return seq

    def create_playlist(
        self,
        name: str,
        parent_id: int | None = None,
        seq: int | None = None,
        *,
        folder: bool = False,
    ) -> dict[str, Any]:
        """Insert a playlist (``attribute`` 0) or folder (1) under ``parent_id``."""
        parent = int(parent_id or 0)
        with self.conn:
            if parent and self.get_playlist_by_id(parent) is None:
                raise OneLibraryError(f"parent playlist id={parent} not present")
            slot = self._place("playlist", "playlist_id_parent", parent, seq)
            cur = self.conn.execute(
                'INSERT INTO playlist ("sequenceNo", name, attribute, playlist_id_parent) '
                "VALUES (?, ?, ?, ?)",
                (slot, str(name), 1 if folder else 0, parent),
            )
            new_id = cur.lastrowid
        row = None if new_id is None else self.get_playlist_by_id(int(new_id))
        if row is None:
            raise OneLibraryError(f"playlist {name!r} was not readable after insert")
        return row

    def create_playlist_content(
        self, playlist_id: int, content_id: int, seq: int | None = None
    ) -> dict[str, Any]:
        """Add an existing content row to a playlist."""
        pid, cid = int(playlist_id), int(content_id)
        with self.conn:
            if self.get_playlist_by_id(pid) is None:
                raise OneLibraryError(f"playlist id={pid} not present")
            if self.get_content_by_id(cid) is None:
                raise OneLibraryError(f"content id={cid} not present")
            slot = self._place("playlist_content", "playlist_id", pid, seq)
            self.conn.execute(
                'INSERT INTO playlist_content (playlist_id, content_id, "sequenceNo") '
                "VALUES (?, ?, ?)",
                (pid, cid, slot),
            )
        return {"playlist_id": pid, "content_id": cid, "seq": slot}

    def checkpoint(self) -> None:
        """Fold any ``-wal`` pages into the main file."""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
