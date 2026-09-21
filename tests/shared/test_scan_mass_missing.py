"""LIBM-41: a populated scan root that collapses to empty is an error.

Never purge on absence. Real temp dirs, no mocked filesystem.

[if] a populated scan root reads as empty [then] the guard raises and keeps its rows, [else stop].
"""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import pytest

from apps.mik import availability as avail
from apps.reconcile import index_disk
from apps.shared.scan_mass_missing import (
    DEFAULT_DROP_FRACTION,
    MassMissingError,
    guard_roots,
    guard_scan_count,
    path_is_under_root,
)
from apps.shared.state import db as state_db
from apps.shared.state.ingest import folder as folder_ingest
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("LIBM-41")


def _mp3(folder: Path, name: str) -> Path:
    path = folder / name
    # ID3 magic alone is not enough when mutagen is installed: probe_playable_audio
    # cross-checks duration against size, so include a minimal MPEG frame body.
    path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 500)
    return path


def _wav(folder: Path, name: str) -> Path:
    """Playable wav for folder-ingest paths that call probe_playable_audio."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44_100 * 0.05)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


def _empty(folder: Path) -> None:
    for child in folder.iterdir():
        if child.is_file():
            child.unlink()


# ----- pure guard ---------------------------------------------------------


def test_a_new_root_may_resolve_zero() -> None:
    guard_scan_count("/new", current_n=0, prior_n=None)


def test_a_recorded_zero_baseline_may_stay_zero() -> None:
    guard_scan_count("/empty", current_n=0, prior_n=0)


def test_zero_after_n_raises_and_names_the_root_and_n() -> None:
    with pytest.raises(MassMissingError, match=r"/music") as caught:
        guard_scan_count("/music", current_n=0, prior_n=12)
    assert caught.value.prior_n == 12
    assert caught.value.current_n == 0
    assert "12" in str(caught.value)


def test_a_drop_of_more_than_half_raises() -> None:
    with pytest.raises(MassMissingError, match="LIBM-41"):
        guard_scan_count("/music", current_n=2, prior_n=5)


def test_a_drop_of_exactly_half_is_allowed() -> None:
    guard_scan_count("/music", current_n=1, prior_n=2)


def test_allow_mass_missing_overrides_zero_and_fraction() -> None:
    guard_scan_count("/music", current_n=0, prior_n=40, allow_mass_missing=True)
    guard_scan_count("/music", current_n=1, prior_n=10, allow_mass_missing=True)


def test_default_drop_fraction_is_fifty_percent() -> None:
    assert DEFAULT_DROP_FRACTION == 0.5


def test_path_is_under_root_does_not_match_a_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "music"
    sibling = tmp_path / "music2" / "a.mp3"
    assert path_is_under_root(root / "a.mp3", root)
    assert not path_is_under_root(sibling, root)


def test_guard_roots_refuses_only_the_collapsed_root(tmp_path: Path) -> None:
    live = tmp_path / "live"
    dead = tmp_path / "dead"
    live.mkdir()
    dead.mkdir()
    with pytest.raises(MassMissingError, match="dead"):
        guard_roots(
            [live, dead],
            current_paths=[str(live / "a.mp3")],
            prior_paths=[str(live / "a.mp3"), str(dead / "b.mp3")],
        )


# ----- ingest-folder write path ------------------------------------------


def test_ingest_folder_empty_after_populate_refuses_and_keeps_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    _wav(root, "one.wav")
    _wav(root, "two.wav")
    conn = state_db.open_rw(tmp_path / "state.db")
    writer = StateWriter(conn, actor="test")
    try:
        first = folder_ingest.ingest_folder(writer, [root], dry_run=False)
        assert first.tracks_inserted == 2
        before = list(
            conn.execute("SELECT stable_id, file_path, deleted_at FROM tracks ORDER BY stable_id")
        )
        _empty(root)
        with pytest.raises(MassMissingError, match="2") as caught:
            folder_ingest.ingest_folder(writer, [root], dry_run=False)
        assert str(root) in str(caught.value)
        after = list(
            conn.execute("SELECT stable_id, file_path, deleted_at FROM tracks ORDER BY stable_id")
        )
        assert after == before
        assert all(row[2] is None for row in after)
    finally:
        writer.close()
        conn.close()


def test_ingest_folder_allow_mass_missing_writes_nothing_on_empty(
    tmp_path: Path,
) -> None:
    """Override is a refusal bypass, not a purge. Ingest never tombstones."""
    root = tmp_path / "lib"
    root.mkdir()
    _wav(root, "one.wav")
    conn = state_db.open_rw(tmp_path / "state.db")
    writer = StateWriter(conn, actor="test")
    try:
        folder_ingest.ingest_folder(writer, [root], dry_run=False)
        _empty(root)
        report = folder_ingest.ingest_folder(writer, [root], dry_run=False, allow_mass_missing=True)
        assert report.files_seen == 0
        n, deleted = conn.execute(
            "SELECT COUNT(*), SUM(deleted_at IS NOT NULL) FROM tracks"
        ).fetchone()
        assert n == 1
        assert not deleted
    finally:
        writer.close()
        conn.close()


# ----- availability refresh write path -----------------------------------


def test_availability_empty_after_populate_refuses_and_keeps_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    one = _mp3(root, "one.mp3")
    two = _mp3(root, "two.mp3")
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        now = "2026-09-11T00:00:00+00:00"
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, "
            "file_path, created_at, updated_at) VALUES "
            "(?, 'inferred', 'one', ?, ?, ?), "
            "(?, 'inferred', 'two', ?, ?, ?)",
            ("a" * 40, str(one), now, now, "b" * 40, str(two), now, now),
        )
        first = avail.refresh(conn, now="2026-09-11T01:00:00+00:00")
        assert first.counts.get("present") == 2
        before = list(
            conn.execute(
                "SELECT stable_id, state, checked_path, checked_at "
                "FROM track_availability ORDER BY stable_id"
            )
        )
        _empty(root)
        with pytest.raises(MassMissingError, match="library"):
            avail.refresh(conn, now="2026-09-11T02:00:00+00:00")
        after = list(
            conn.execute(
                "SELECT stable_id, state, checked_path, checked_at "
                "FROM track_availability ORDER BY stable_id"
            )
        )
        assert after == before
        assert {row[1] for row in after} == {"present"}
    finally:
        conn.close()


def test_availability_allow_mass_missing_records_the_drop(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    one = _mp3(root, "one.mp3")
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        now = "2026-09-11T00:00:00+00:00"
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, "
            "file_path, created_at, updated_at) VALUES "
            "(?, 'inferred', 'one', ?, ?, ?)",
            ("a" * 40, str(one), now, now),
        )
        avail.refresh(conn, now="2026-09-11T01:00:00+00:00")
        one.unlink()
        report = avail.refresh(
            conn,
            now="2026-09-11T02:00:00+00:00",
            allow_mass_missing=True,
        )
        assert report.counts.get("absent") == 1
        state = conn.execute("SELECT state FROM track_availability").fetchone()[0]
        assert state == "absent"
    finally:
        conn.close()


# ----- index_disk cache write path ---------------------------------------


def test_index_disk_empty_after_populate_refuses_and_keeps_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    _mp3(root, "one.mp3")
    _mp3(root, "two.wav")
    cache = tmp_path / "index.json"
    index_disk.build_index([root], cache_path=cache)
    before = cache.read_text(encoding="utf-8")
    payload = json.loads(before)
    assert len(payload["entries"]) == 2
    _empty(root)
    with pytest.raises(MassMissingError, match="2"):
        index_disk.build_index([root], cache_path=cache)
    assert cache.read_text(encoding="utf-8") == before
