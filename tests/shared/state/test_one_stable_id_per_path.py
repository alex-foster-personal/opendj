"""LIBM-180 (#5988): one file path keeps one inferred stable_id.

Tier-3 ids hash path and mtime, so a refresh after an mtime move used to
mint a second identity for the same file (270 present files on silver).
This module imports one real fixture by the folder-import route, then
again by the folder-refresh route, and demands a single stable_id.

[if] folder import then refresh see one file [then] one stable_id, [else stop].
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.ids import stable_id
from apps.shared.state.ingest import folder, folder_rescan
from apps.shared.state.ingest.path_identity import (
    live_inferred_by_path_key,
    live_rekordbox_stable_ids_by_vendor,
    resolve_inferred_stable_id,
    resolve_rb_existing_stable_id,
)
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("LIBM-180")

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "phase7-dedup" / "src-v2.mp3"


def _writer(conn) -> StateWriter:
    return StateWriter(conn, bus=FakeEventBus(), actor="libm-180")


def _all_sids(conn) -> list[str]:
    rows = conn.execute("SELECT stable_id FROM tracks ORDER BY created_at, stable_id").fetchall()
    return [row[0] for row in rows]


def _live_sids(conn) -> list[str]:
    rows = conn.execute(
        "SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY created_at, stable_id"
    ).fetchall()
    return [row[0] for row in rows]


def _copy_real_mp3(tmp_path: Path) -> tuple[Path, Path]:
    assert FIXTURE.is_file() and FIXTURE.stat().st_size > 0
    music = tmp_path / "music"
    music.mkdir()
    dest = music / FIXTURE.name
    shutil.copy2(FIXTURE, dest)
    return music, dest


def _bump_mtime(path: Path) -> None:
    later = path.stat().st_mtime + 10
    os.utime(path, (later, later))


def test_folder_import_then_refresh_keeps_one_stable_id(state_conn, tmp_path: Path) -> None:
    """[if] folder import then refresh see one file [then] one stable_id, [else stop]."""
    music, dest = _copy_real_mp3(tmp_path)
    writer = _writer(state_conn)
    imported = folder.ingest_folder(writer, [music], dry_run=False)
    assert imported.tracks_inserted == 1
    first = _live_sids(state_conn)
    assert len(first) == 1
    stable_id = first[0]

    _bump_mtime(dest)
    refreshed = folder_rescan.reconcile_folders(writer, [music], previous_signature="")
    writer.close()

    assert _live_sids(state_conn) == [stable_id]
    assert _all_sids(state_conn) == [stable_id]
    assert refreshed.tracks_added == 0
    assert refreshed.tracks_removed == 0


def test_folder_import_twice_after_mtime_move_keeps_one_stable_id(state_conn, tmp_path: Path) -> None:
    """[if] folder import runs twice after mtime moves [then] one stable_id, [else stop]."""
    music, dest = _copy_real_mp3(tmp_path)
    writer = _writer(state_conn)
    folder.ingest_folder(writer, [music], dry_run=False)
    first = _live_sids(state_conn)
    assert len(first) == 1

    _bump_mtime(dest)
    folder.ingest_folder(writer, [music], dry_run=False)
    writer.close()

    assert _live_sids(state_conn) == first
    assert _all_sids(state_conn) == first


def test_existing_duplicate_rows_do_not_gain_a_third_id(state_conn, tmp_path: Path) -> None:
    """[if] two live inferred rows already share one path [then] no third, [else stop]."""
    music, dest = _copy_real_mp3(tmp_path)
    writer = _writer(state_conn)
    folder.ingest_folder(writer, [music], dry_run=False)
    first = _live_sids(state_conn)[0]
    writer.upsert_track(
        stable_id="already-duplicate",
        stable_id_tier="inferred",
        title="dup",
        artists=[],
        album=None,
        isrc=None,
        duration_ms=None,
        file_path=str(dest),
    )
    assert len(_live_sids(state_conn)) == 2

    _bump_mtime(dest)
    folder.ingest_folder(writer, [music], dry_run=False)
    writer.close()

    live = set(_live_sids(state_conn))
    assert live == {first, "already-duplicate"}


def test_path_identity_reuses_the_oldest_live_inferred_id(state_conn, tmp_path: Path) -> None:
    """[if] two inferred rows share one path [then] resolve keeps the oldest, [else stop]."""
    music, dest = _copy_real_mp3(tmp_path)
    writer = _writer(state_conn)
    folder.ingest_folder(writer, [music], dry_run=False)
    oldest = _live_sids(state_conn)[0]
    writer.upsert_track(
        stable_id="newer-duplicate",
        stable_id_tier="inferred",
        title="dup",
        artists=[],
        album=None,
        isrc=None,
        duration_ms=None,
        file_path=str(dest),
    )
    by_key = live_inferred_by_path_key(state_conn)
    resolved, tier = resolve_inferred_stable_id(str(dest), dest.stat().st_mtime + 99, by_key)
    writer.close()
    assert tier == "inferred"
    assert resolved == oldest
    assert resolved != "newer-duplicate"


@pytest.mark.requirement("LIBM-12770")
def test_resolve_rb_existing_stable_id_prefers_vendor_link(state_conn, tmp_path: Path) -> None:
    """[if] vendor V is linked [then] resolve_rb reuses that id, [else stop]."""
    music, _dest = _copy_real_mp3(tmp_path)
    writer = _writer(state_conn)
    folder.ingest_folder(writer, [music], dry_run=False)
    sid = _live_sids(state_conn)[0]
    writer.set_vendor_id(sid, "rekordbox", "rb-vendor-12770")
    writer.close()
    alt = tmp_path / "Music" / "air-library" / FIXTURE.name
    alt.parent.mkdir(parents=True)
    by_vendor = live_rekordbox_stable_ids_by_vendor(state_conn)
    resolved = resolve_rb_existing_stable_id(
        state_conn,
        "rb-vendor-12770",
        None,
        None,
        rb_vendor_index=by_vendor,
    )
    assert resolved == (sid, "inferred")
    minted, _tier = resolve_inferred_stable_id(str(alt), 0.0, live_inferred_by_path_key(state_conn))
    assert minted != sid


def _seed_hash_row(writer: StateWriter, *, path: str, content_hash: str, rb_vendor: str | None) -> tuple[str, str]:
    sid, tier = stable_id(isrc=None, abs_path=path, mtime=1.0)
    writer.upsert_track(
        stable_id=sid,
        stable_id_tier=tier,
        title="hash-seed",
        artists=[],
        album=None,
        isrc=None,
        duration_ms=1000,
        file_path=path,
        content_hash=content_hash,
        audio_hash=None,
    )
    if rb_vendor is not None:
        writer.set_vendor_id(sid, "rekordbox", rb_vendor)
    return sid, tier


@pytest.mark.requirement("LIBM-12770")
def test_resolve_rb_hash_reuse_skips_row_claimed_by_other_present_vendor(state_conn, tmp_path: Path) -> None:
    """[if] two present rb ids share hash [then] second does not reuse first, [else stop]."""
    content_hash = "d" * 64
    writer = _writer(state_conn)
    _seed_hash_row(
        writer,
        path=str(tmp_path / "claimed.mp3"),
        content_hash=content_hash,
        rb_vendor="rb-A",
    )
    writer.close()
    resolved = resolve_rb_existing_stable_id(
        state_conn,
        "rb-B",
        content_hash,
        None,
        present_rb_vendor_ids={"rb-A", "rb-B"},
    )
    assert resolved is None


@pytest.mark.requirement("LIBM-12770")
def test_resolve_rb_hash_reuse_allows_unlinked_row(state_conn, tmp_path: Path) -> None:
    """[if] hash row has no vendor link [then] hash reuse succeeds, [else stop]."""
    content_hash = "e" * 64
    writer = _writer(state_conn)
    seeded_sid, seeded_tier = _seed_hash_row(
        writer,
        path=str(tmp_path / "unlinked.mp3"),
        content_hash=content_hash,
        rb_vendor=None,
    )
    writer.close()
    resolved = resolve_rb_existing_stable_id(
        state_conn,
        "rb-new",
        content_hash,
        None,
        present_rb_vendor_ids={"rb-new"},
    )
    assert resolved == (seeded_sid, seeded_tier)


@pytest.mark.requirement("LIBM-12770")
def test_resolve_rb_hash_reuse_allows_stale_vendor_claim(state_conn, tmp_path: Path) -> None:
    """[if] linked vendor is absent from present set [then] hash reuse succeeds, [else stop]."""
    content_hash = "f" * 64
    writer = _writer(state_conn)
    seeded_sid, seeded_tier = _seed_hash_row(
        writer,
        path=str(tmp_path / "stale-link.mp3"),
        content_hash=content_hash,
        rb_vendor="rb-A",
    )
    writer.close()
    resolved = resolve_rb_existing_stable_id(
        state_conn,
        "rb-B",
        content_hash,
        None,
        present_rb_vendor_ids={"rb-B"},
    )
    assert resolved == (seeded_sid, seeded_tier)
