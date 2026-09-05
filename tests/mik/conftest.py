"""Fixtures for ``tests/mik``.

Everything here builds REAL sqlite stores of the real shape. Nothing is
mocked: the fake MIK store is an actual Core-Data-shaped sqlite file, and the
fake bookmark blobs are actual bookmark blobs, byte-for-byte parseable by
:mod:`apps.mik.bookmark`. A mocked reader would prove nothing about the two
bugs that actually cost time on this data (the ASCII-scrape false zero match
and the seconds/ms confusion).
"""
from __future__ import annotations

import json
import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.mik import load as loader
from apps.mik import match as matcher
from apps.mik import mikdb
from apps.mik.mikdb import CORE_DATA_EPOCH
from apps.shared.equivalence import EquivalenceGate
from apps.shared.state import db as state_db

# Shared by tests/mik/test_promote.py and test_promote_precedence.py (the
# latter split out of the former purely for the file-size review gate).
ALL_PASSED = {field: "passed" for field in loader.GATED_FIELDS}
STAGED_FIELD_COUNT = len(loader.GATED_FIELDS)

HEADER_SIZE = 48
TYPE_STRING = 0x0101
TYPE_ARRAY = 0x0601
KEY_PATH_COMPONENTS = 0x1004
KEY_VOLUME_PATH = 0x2002
KEY_VOLUME_NAME = 0x2010


# ------------------------------------------------------- bookmark builder


class _BookmarkBuilder:
    """Emit a real Apple bookmark blob. Used to prove the parser, not fake it."""

    def __init__(self) -> None:
        self.data = bytearray()

    def _add(self, type_code: int, payload: bytes) -> int:
        offset = 4 + len(self.data)  # 4 bytes reserved for the TOC pointer
        self.data += struct.pack("<II", len(payload), type_code) + payload
        while len(self.data) % 4:
            self.data += b"\x00"
        return offset

    def add_string(self, text: str) -> int:
        return self._add(TYPE_STRING, text.encode("utf-8"))

    def add_array(self, offsets: list[int]) -> int:
        return self._add(
            TYPE_ARRAY, b"".join(struct.pack("<I", o) for o in offsets)
        )

    def build(self, entries: dict[int, int]) -> bytes:
        toc_offset = 4 + len(self.data)
        toc = struct.pack("<5I", 0, 0xFFFFFFFE, 1, 0, len(entries))
        for key, value_offset in sorted(entries.items()):
            toc += struct.pack("<3I", key, value_offset, 0)
        body = struct.pack("<I", toc_offset) + bytes(self.data) + toc
        total = HEADER_SIZE + len(body)
        header = struct.pack("<4sIII", b"book", total, 0x10040000, HEADER_SIZE)
        header += b"\x00" * (HEADER_SIZE - len(header))
        return header + body


def make_bookmark(path: str, *, volume_name: str = "Macintosh HD") -> bytes:
    """Encode ``path`` (absolute) as a bookmark blob."""
    components = [part for part in path.split("/") if part]
    builder = _BookmarkBuilder()
    component_offsets = [builder.add_string(part) for part in components]
    entries = {
        KEY_PATH_COMPONENTS: builder.add_array(component_offsets),
        KEY_VOLUME_PATH: builder.add_string("/"),
        KEY_VOLUME_NAME: builder.add_string(volume_name),
    }
    return builder.build(entries)


# ---------------------------------------------------------- fake MIK store


def _core_data_seconds(iso: str) -> float:
    return (datetime.fromisoformat(iso) - CORE_DATA_EPOCH).total_seconds()


@pytest.fixture
def make_mik_store(tmp_path: Path):
    """Factory: write a Core-Data-shaped ``.mikdb`` and return its path."""

    def _make(songs: list[dict], *, name: str = "Collection10.mikdb") -> Path:
        path = tmp_path / name
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE ZSONG (
                Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZARTIST TEXT, ZALBUM TEXT,
                ZKEY TEXT, ZENERGY REAL, ZTEMPO REAL, ZVOLUME REAL,
                ZCLIPPEDPEAKCOUNT INTEGER, ZANALYSISDATE REAL,
                ZBOOKMARKDATA BLOB
            );
            CREATE TABLE ZENERGYSEGMENT (
                Z_PK INTEGER PRIMARY KEY, ZSONG INTEGER, ZENERGY REAL,
                ZLENGTH REAL, ZSTARTTIME REAL
            );
            CREATE TABLE ZKEYSEGMENT (
                Z_PK INTEGER PRIMARY KEY, ZSONG INTEGER, ZCONFIDENCE REAL,
                ZKEY TEXT
            );
            """
        )
        for index, song in enumerate(songs, start=1):
            path_value = song.get("path")
            blob = (
                make_bookmark(path_value)
                if path_value
                else song.get("bookmark_blob")
            )
            conn.execute(
                "INSERT INTO ZSONG(Z_PK, ZNAME, ZARTIST, ZALBUM, ZKEY, ZENERGY, "
                "ZTEMPO, ZVOLUME, ZCLIPPEDPEAKCOUNT, ZANALYSISDATE, "
                "ZBOOKMARKDATA) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    song.get("pk", index),
                    song.get("name"),
                    song.get("artist"),
                    song.get("album"),
                    song.get("key", "8A"),
                    song.get("energy", 6.0),
                    song.get("tempo", 124.0),
                    song.get("volume", -11.5),
                    song.get("clipped_peaks", 0),
                    _core_data_seconds(
                        song.get("analysed_at", "2024-01-01T00:00:00+00:00")
                    ),
                    blob,
                ),
            )
            if song.get("confidence") is not None:
                conn.execute(
                    "INSERT INTO ZKEYSEGMENT(ZSONG, ZCONFIDENCE, ZKEY) "
                    "VALUES (?, ?, ?)",
                    (
                        song.get("pk", index),
                        song["confidence"],
                        song.get("key", "8A"),
                    ),
                )
            for start_s, length_s, energy in song.get("segments", []):
                conn.execute(
                    "INSERT INTO ZENERGYSEGMENT(ZSONG, ZENERGY, ZLENGTH, "
                    "ZSTARTTIME) VALUES (?, ?, ?, ?)",
                    (song.get("pk", index), float(energy), length_s, start_s),
                )
        conn.commit()
        conn.close()
        return path

    return _make


# ------------------------------------------------------------- state DB


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def state_conn(data_dir: Path):
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def add_track(state_conn: sqlite3.Connection):
    """Insert a ``tracks`` row and return its stable_id."""

    def _add(
        stable_id: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        file_path: str | None = None,
    ) -> str:
        now = datetime.now(UTC).isoformat()
        state_conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?, ?)",
            (
                stable_id,
                title,
                json.dumps([artist] if artist else []),
                file_path,
                now,
                now,
            ),
        )
        return stable_id

    return _add


@pytest.fixture
def write_verdicts(data_dir: Path):
    """Factory writing ``data/state/equivalence-verdicts.json``."""

    def _write(statuses: dict[str, str]) -> Path:
        payload = {
            field: {
                "status": status,
                "normaliser": f"test_{field}" if status == "passed" else None,
                "checked_at": "2026-07-28T00:00:00+00:00",
            }
            for field, status in statuses.items()
        }
        path = data_dir / "state" / "equivalence-verdicts.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    return _write


# --------------------------------------------------------- promotion staging


@pytest.fixture
def staged(state_conn, make_mik_store, data_dir: Path, write_verdicts):
    """Stage MIK song pk 1 with no matching track, then return its ids."""
    write_verdicts(ALL_PASSED)
    store = make_mik_store(
        [
            {
                "pk": 1,
                "path": "/Users/dev/orphan.mp3",
                "name": "7 - Orphan",
                "artist": "Nobody",
                "key": "9A",
                "energy": 7.0,
                "confidence": 0.91,
                "segments": [(0.0, 60.0, 5), (60.0, 30.0, 8)],
            }
        ]
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(store))
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(songs, index)
    gate = EquivalenceGate.load(data_dir)
    plan = loader.build_plan(songs, report, gate, state_conn)
    loader.apply_plan(state_conn, plan)
    assert len(plan.staged) == STAGED_FIELD_COUNT
    return {"source_row_id": "1", "gate": gate}


def _stage_one(
    state_conn: sqlite3.Connection,
    make_mik_store,
    data_dir: Path,
    write_verdicts,
    *,
    confidence: float,
) -> EquivalenceGate:
    """Stage MIK song pk 1 (no matching track) with a chosen key confidence."""
    write_verdicts(ALL_PASSED)
    store = make_mik_store(
        [
            {
                "pk": 1,
                "path": "/Users/dev/orphan.mp3",
                "name": "7 - Orphan",
                "artist": "Nobody",
                "key": "9A",
                "energy": 7.0,
                "confidence": confidence,
                "segments": [(0.0, 60.0, 5)],
            }
        ]
    )
    songs, _ = mikdb.read_songs(mikdb.open_ro(store))
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(songs, index)
    gate = EquivalenceGate.load(data_dir)
    plan = loader.build_plan(songs, report, gate, state_conn)
    loader.apply_plan(state_conn, plan)
    return gate
