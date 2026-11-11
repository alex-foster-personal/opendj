"""Plan 05-03 integration tests: Rekordbox -> state.db.

Uses the shared ``rb_plain_db_path`` fixture from the root conftest, which
points at the committed ~50-track Rekordbox fixture
(``tests/fixtures/rekordbox/master.plain.db``). Every test opens a throwaway
state.db under ``tmp_path`` and leaves the fixture untouched (read-only).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter

pytestmark = [
    pytest.mark.requirement("INFRA-01"),
    pytest.mark.requirement("OPEN-01b"),
    pytest.mark.requirement("OPEN-01"),
]


@pytest.fixture
def rb_fixture(rb_plain_db_path: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "rb.db"
    shutil.copy2(rb_plain_db_path, dst)
    return dst


@pytest.fixture
def writer_and_conn(state_db_path: Path):
    conn = state_db.open_rw(state_db_path)
    w = StateWriter(conn, bus=FakeEventBus(), actor="ingest-rb")
    try:
        yield w, conn
    finally:
        w.close()
        conn.close()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in (
            "tracks",
            "track_vendor_ids",
            "track_fields",
            "track_field_history",
            "playlists",
            "playlist_memberships",
            "adapters",
            "events",
        )
    }


def test_ingest_dry_run_persists_nothing(rb_fixture: Path, writer_and_conn) -> None:
    writer, conn = writer_and_conn
    report = rb_ingest.ingest_rb(writer, rb_fixture, dry_run=True)
    assert report.dry_run is True
    counts = _counts(conn)
    for name, count in counts.items():
        assert count == 0, f"dry-run should have left {name} empty; got {count}"


def test_ingest_write_populates_tables(rb_fixture: Path, writer_and_conn) -> None:
    writer, conn = writer_and_conn
    report = rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False)
    assert report.tracks_inserted > 0
    counts = _counts(conn)
    total_ops = report.tracks_inserted + report.tracks_updated + report.tracks_unchanged
    assert counts["tracks"] <= total_ops
    assert counts["tracks"] >= report.tracks_inserted
    assert counts["track_vendor_ids"] == counts["tracks"]
    assert counts["track_fields"] > 0
    assert counts["adapters"] == 1
    assert counts["events"] > 0


def test_ingest_tier_breakdown_counts_match_ops(rb_fixture: Path, writer_and_conn) -> None:
    writer, _conn = writer_and_conn
    report = rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False)
    total_from_tiers = sum(report.tier_counts.values())
    total_ops = report.tracks_inserted + report.tracks_updated + report.tracks_unchanged
    assert total_from_tiers == total_ops
    assert set(report.tier_counts).issubset({"isrc", "fingerprint", "inferred"})


@pytest.mark.requirement("LIBM-12770")
def test_ingest_rb_reuses_vendor_linked_row_on_path_remap(monkeypatch, writer_and_conn, tmp_path: Path) -> None:
    """[if] state holds vendor V at old path [then] remap reuse, [else stop]."""
    from apps.shared.state.ids import stable_id
    from apps.shared.state.ingest.path_identity import resolve_rb_existing_stable_id

    writer, conn = writer_and_conn
    old_path = str(tmp_path / "Users" / "dev" / "song.mp3")
    new_path = str(tmp_path / "Music" / "air-library" / "song.mp3")
    vendor = "test-vendor-11425"
    sid, tier = stable_id(isrc=None, abs_path=old_path, mtime=1.0)
    writer.upsert_track(
        stable_id=sid,
        stable_id_tier=tier,
        title="x",
        artists=[],
        album=None,
        isrc=None,
        duration_ms=1000,
        file_path=old_path,
        content_hash="d" * 64,
    )
    writer.set_vendor_id(sid, "rekordbox", vendor)

    class _FakeRbDb:
        def get_cue(self):
            return []

        def close(self):
            return None

    import pyrekordbox

    row = {
        "id": vendor,
        "title": "x",
        "artist": "",
        "album": "",
        "folder_path": new_path,
        "is_streaming": False,
        "isrc": None,
        "bpm": None,
        "rating": None,
        "duration_ms": 1000,
        "file_size": 500,
        "key_name": None,
        "updated_at": None,
    }
    monkeypatch.setattr(pyrekordbox, "Rekordbox6Database", lambda *a, **k: _FakeRbDb())
    monkeypatch.setattr(rb_ingest, "_rb_rows", lambda _rb: iter([row]))
    monkeypatch.setattr(rb_ingest, "_rb_playlists", lambda _rb: iter([]))
    monkeypatch.setattr(rb_ingest, "_content_hash_for", lambda _p, _s: "d" * 64)

    assert resolve_rb_existing_stable_id(conn, vendor, "d" * 64, None) == (sid, tier)
    report = rb_ingest.ingest_rb(writer, tmp_path / "rb.db", dry_run=False)
    assert report.tracks_inserted == 0
    assert conn.execute("SELECT stable_id FROM tracks").fetchone()[0] == sid


def test_ingest_idempotent(rb_fixture: Path, writer_and_conn) -> None:
    writer, conn = writer_and_conn
    first = rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False)
    h_before = conn.execute("SELECT COUNT(*) FROM track_field_history").fetchone()[0]
    second = rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False)
    h_after = conn.execute("SELECT COUNT(*) FROM track_field_history").fetchone()[0]
    assert second.tracks_inserted == 0
    assert second.tracks_unchanged == first.tracks_inserted
    # Byte-stable provenance writes mean no history churn.
    assert h_after == h_before


def test_ingest_appends_expected_event_kinds(rb_fixture: Path, writer_and_conn) -> None:
    writer, conn = writer_and_conn
    rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False, limit=5)
    kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM events")}
    assert "track.insert" in kinds
    assert "track.vendor_id.set" in kinds
    assert "adapter.run" in kinds


def test_ingest_limit_caps_tracks(rb_fixture: Path, writer_and_conn) -> None:
    writer, _conn = writer_and_conn
    report = rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False, limit=3)
    total_ops = report.tracks_inserted + report.tracks_updated + report.tracks_unchanged
    assert total_ops <= 3


def test_ingest_playlists_populated(rb_fixture: Path, writer_and_conn) -> None:
    writer, conn = writer_and_conn
    rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False)
    pl_count = conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]
    assert pl_count >= 1
    orphans = conn.execute(
        "SELECT COUNT(*) FROM playlist_memberships m "
        "LEFT JOIN tracks t ON t.stable_id = m.stable_id "
        "WHERE t.stable_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_ingest_reports_vendor_id_links(rb_fixture: Path, writer_and_conn) -> None:
    writer, conn = writer_and_conn
    rb_ingest.ingest_rb(writer, rb_fixture, dry_run=False)
    tracks_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    vendor_count = conn.execute("SELECT COUNT(*) FROM track_vendor_ids WHERE vendor = 'rekordbox'").fetchone()[0]
    assert vendor_count == tracks_count


def test_cli_ingest_rb_dry_run_exits_zero(rb_fixture: Path, state_db_path: Path, capsys: pytest.CaptureFixture) -> None:
    from apps.shared.state import cli as state_cli

    state_cli.main(["--db", str(state_db_path), "init"])
    capsys.readouterr()
    rc = state_cli.main(
        [
            "--db",
            str(state_db_path),
            "ingest-rb",
            "--rb-db",
            str(rb_fixture),
            "--stale-ok",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Rekordbox ingest (dry-run)" in out


def test_cli_ingest_rb_write_then_stats(rb_fixture: Path, state_db_path: Path, capsys: pytest.CaptureFixture) -> None:
    from apps.shared.state import cli as state_cli

    state_cli.main(["--db", str(state_db_path), "init"])
    capsys.readouterr()
    rc = state_cli.main(
        [
            "--db",
            str(state_db_path),
            "ingest-rb",
            "--rb-db",
            str(rb_fixture),
            "--stale-ok",
            "--write",
            "--limit",
            "5",
        ]
    )
    assert rc == 0
    capsys.readouterr()
    rc = state_cli.main(["--db", str(state_db_path), "stats"])
    assert rc == 0
    stats = capsys.readouterr().out
    assert "tracks:" in stats
    assert "rekordbox" in stats
