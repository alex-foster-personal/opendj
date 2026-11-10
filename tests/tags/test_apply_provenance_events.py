"""Tag provenance is written in one StateWriter unit with one events row (STATE-11090).

[if] tags apply writes provenance [then] rows and one events row share one StateWriter unit, [else stop].
"""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.dedup import contract as dedup_contract
from apps.shared.state.db import open_rw
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter, TagProvenanceRow
from apps.shared.tag_writer import TagRead, WriteResult
from apps.tags import apply as tags_apply

pytestmark = pytest.mark.requirement("STATE-11090")

_FAIL_BPM = (
    "CREATE TRIGGER fail_bpm BEFORE INSERT ON tag_provenance "
    "WHEN NEW.field = 'bpm' BEGIN SELECT RAISE(ABORT, 'boom'); END;"
)


def _clock() -> datetime:
    return datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "phase7.sqlite"
    dedup_contract.ensure_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus, clock=_clock, actor="tags.apply")
    yield conn, bus, writer
    writer.close()
    conn.close()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _row(field: str, value: str, source: str = "rb") -> TagProvenanceRow:
    return TagProvenanceRow(field=field, value=value, source=source, confidence=0.9, modified_at=None)


def test_record_tag_provenance_writes_rows_and_one_event(db) -> None:
    conn, bus, writer = db
    writer.record_tag_provenance(stable_id="stable-1", rows=[_row("genre", "Techno"), _row("bpm", "128.0", "mik")])
    events = conn.execute("SELECT kind, stable_id, payload_json FROM events").fetchall()
    ok = _count(conn, "tag_provenance") == 2 and len(events) == 1
    assert ok, "if record_tag_provenance does not write one events row with the rows then broken"
    kind, sid, payload_json = events[0]
    assert (kind, sid) == ("tags.provenance", "stable-1")
    fields = {f["field"]: f["source"] for f in json.loads(payload_json)["fields"]}
    assert fields == {"genre": "rb", "bpm": "mik"}
    assert len(bus.events) == 1


def test_mid_batch_failure_leaves_neither_rows_nor_event(db) -> None:
    conn, bus, writer = db
    conn.execute(_FAIL_BPM)
    with pytest.raises(sqlite3.IntegrityError):
        writer.record_tag_provenance(stable_id="stable-1", rows=[_row("genre", "Techno"), _row("bpm", "128.0")])
    assert _count(conn, "tag_provenance") == 0, "if a failed batch leaves rows then broken"
    assert _count(conn, "events") == 0, "if a failed batch leaves its event then broken"
    assert not conn.in_transaction
    assert bus.events == []


def test_record_tag_provenance_rejects_empty_rows(db) -> None:
    conn, _bus, writer = db
    with pytest.raises(ValueError):
        writer.record_tag_provenance(stable_id="stable-1", rows=[])
    assert _count(conn, "events") == 0


def test_record_tag_provenance_does_not_commit_a_foreign_unit(db) -> None:
    conn, _bus, writer = db
    with writer.transaction(commit=False):
        writer.record_tag_provenance(stable_id="stable-1", rows=[_row("genre", "Techno")])
    assert _count(conn, "tag_provenance") == 0, "if record_tag_provenance commits on its own then broken"
    assert _count(conn, "events") == 0


def _patch_file_io(monkeypatch, tmp_path: Path) -> Path:
    track = tmp_path / "track.mp3"
    track.write_bytes(b"not really audio")
    written: dict = {}

    def fake_write_tags(path, unified, *, dry_run=True):
        written["tags"] = unified
        return WriteResult(path=path, applied={"genre": unified.genre}, dry_run=dry_run)

    def fake_read_tags(path):
        t = written["tags"]
        return TagRead(genre=t.genre, bpm=t.bpm)

    monkeypatch.setattr(tags_apply, "write_tags", fake_write_tags)
    monkeypatch.setattr(tags_apply, "read_tags", fake_read_tags)
    return track


def _apply(track: Path, tmp_path: Path, writer: StateWriter):
    return tags_apply.apply_one(
        track,
        backup_root=tmp_path / "backups",
        provenance_writer=writer,
        stable_id="stable-x",
        allow_app_running=True,
        fetch_rb=lambda _p: TagRead(genre="Techno", bpm=128.0),
        fetch_djay=lambda _p: None,
        fetch_mik=lambda _p: None,
    )


def test_apply_one_records_provenance_through_writer(db, monkeypatch, tmp_path: Path) -> None:
    conn, _bus, writer = db
    track = _patch_file_io(monkeypatch, tmp_path)
    res = _apply(track, tmp_path, writer)
    assert res.error is None
    events = conn.execute("SELECT payload_json FROM events WHERE kind = 'tags.provenance'").fetchall()
    assert len(events) == 1, "if apply_one does not emit one tags.provenance event then broken"
    payload_fields = {f["field"] for f in json.loads(events[0][0])["fields"]}
    row_fields = {r[0] for r in conn.execute("SELECT field FROM tag_provenance")}
    assert payload_fields == row_fields and "genre" in row_fields


def test_apply_one_provenance_failure_stays_non_fatal(db, monkeypatch, tmp_path: Path) -> None:
    conn, _bus, writer = db
    conn.execute(_FAIL_BPM)
    track = _patch_file_io(monkeypatch, tmp_path)
    res = _apply(track, tmp_path, writer)
    assert res.error and res.error.startswith("provenance log failed (audio write OK)")
    assert res.applied == {"genre": "Techno"}
    assert _count(conn, "tag_provenance") == 0
    assert _count(conn, "events") == 0


def test_apply_module_has_no_bare_commit() -> None:
    tree = ast.parse(Path(tags_apply.__file__).read_text(encoding="utf-8"))
    attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not attrs & {"commit", "executemany"}, "if apply.py writes provenance outside StateWriter then broken"


def test_dedup_events_table_matches_state_events(tmp_path: Path) -> None:
    """If the dedup events DDL drifts from state.db's, replay of tags.provenance breaks."""
    dedup_path = tmp_path / "phase7.sqlite"
    dedup_contract.ensure_schema(dedup_path)
    dedup_conn = sqlite3.connect(dedup_path)
    state_conn = open_rw(tmp_path / "state.db")
    try:

        def cols(conn: sqlite3.Connection) -> list[tuple]:
            return [(r[1], r[2], r[3]) for r in conn.execute("PRAGMA table_info(events)")]

        assert cols(dedup_conn), "dedup schema has no events table"
        assert cols(dedup_conn) == cols(state_conn)
    finally:
        dedup_conn.close()
        state_conn.close()
