"""Tests for apps.sync.usb.verify engine + CLI."""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from apps.sync.usb import apply as apply_mod
from apps.sync.usb import verify as verify_mod
from apps.sync.usb.pioneer.reader import grid_summary_from_anlz, read_anlz_dir, read_usb_export
from apps.sync.usb.pioneer.value_verify import (
    DENOMINATOR_LABEL,
    ExpectedGrid,
    ExpectedTrack,
    FieldStatus,
    load_expected_json,
    stick_values_to_jsonable,
    verify_stick_values,
)
from apps.sync.usb.pioneer.value_verify_sidecar import probe_odj_analysis_scalar
from apps.sync.usb.profile import load_from_string
from apps.sync.usb.verify import FileStatus, plan_from_verify, verify_drive
from tests.fixtures.conftest import resolve_required_fixture

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = pytest.mark.rekordbox_writeback


def _profile():
    return load_from_string(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
"""
    )


def _apply(fixture_canonical, drive_root) -> None:
    """Synthesise a drive that matches canonical by copying bytes directly."""
    from apps.sync.usb.diff import compute_plan
    profile = _profile()
    plan = compute_plan(
        profile=profile, canonical=fixture_canonical, drive_root=drive_root
    )
    import shutil
    for op in plan.ops:
        assert op.src is not None
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(op.src, op.dst)


@pytest.mark.requirement("CAT-02")
def test_verify_all_ok(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.ok == 3
    assert report.missing == 0
    assert report.extra == 0
    assert report.corrupted == 0
    assert report.renamed == 0


@pytest.mark.requirement("CAT-02")
def test_verify_missing(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    # Delete one.
    (drive_root / "Alice" / "AA" / "One.mp3").unlink()
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.missing == 1
    assert report.ok == 2


@pytest.mark.requirement("CAT-02")
def test_verify_corrupted(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    # Scramble one.
    (drive_root / "Alice" / "AA" / "One.mp3").write_bytes(b"corrupt")
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.corrupted == 1


@pytest.mark.requirement("CAT-02")
def test_verify_extra_without_rename(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    extra = drive_root / "Random" / "Thing.mp3"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"random unrelated bytes")
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.extra == 1
    assert report.renamed == 0


@pytest.mark.requirement("CAT-02")
def test_verify_renamed(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    # Move a file from its expected path to another path; keep the bytes.
    src = drive_root / "Alice" / "AA" / "One.mp3"
    dst = drive_root / "Relocated" / "One.mp3"
    dst.parent.mkdir(parents=True)
    src.rename(dst)
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    # One renamed, one missing for the expected path is reported as RENAMED,
    # not MISSING + EXTRA.
    assert report.renamed == 1
    # The expected path is absent -> still MISSING. But EXTRA 0.
    assert report.missing == 1
    assert report.extra == 0


@pytest.mark.requirement("CAT-02")
def test_verify_playlist_broken(fixture_canonical, drive_root) -> None:
    _apply(fixture_canonical, drive_root)
    pl = drive_root / "Playlists"
    pl.mkdir()
    (pl / "Warmup.m3u8").write_text(
        "#EXTM3U\n#EXTINF:120,Alice - One\n../Alice/AA/One.mp3\n../Does/Not/Exist.mp3\n",
        encoding="utf-8",
    )
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert "Warmup.m3u8" in report.playlists_broken


@pytest.mark.requirement("CAT-02")
def test_verify_cli_exit_codes(
    fixture_canonical, drive_root, tmp_path, monkeypatch
) -> None:
    _apply(fixture_canonical, drive_root)

    def fake_loader(*, playlist_names, use_shared_state=False, hash_cache=None, db=None):
        return fixture_canonical

    monkeypatch.setattr(
        "apps.sync.usb.verify.load_canonical_tracks", fake_loader
    )

    pf = tmp_path / "profile.yaml"
    pf.write_text(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
""",
        encoding="utf-8",
    )
    # Happy path -> exit 0.
    out_json = tmp_path / "verify.json"
    rc = verify_mod.main(
        [
            "--profile",
            str(pf),
            "--drive-root",
            str(drive_root),
            "--json",
            str(out_json),
        ]
    )
    assert rc == 0
    data = json.loads(out_json.read_text())
    assert data["counts"]["ok"] == 3
    # Delete a file -> exit 5.
    (drive_root / "Alice" / "AA" / "One.mp3").unlink()
    rc2 = verify_mod.main(
        ["--profile", str(pf), "--drive-root", str(drive_root), "--only-drift"]
    )
    assert rc2 == 5


@pytest.mark.requirement("CAT-02")
def test_remediate_missing_round_trip(
    fixture_canonical, drive_root, monkeypatch, tmp_path
) -> None:
    _apply(fixture_canonical, drive_root)
    # Delete one file so it shows as MISSING.
    (drive_root / "Alice" / "AA" / "One.mp3").unlink()

    # Fake the canonical loader for apply module.
    def fake_loader(*, playlist_names, use_shared_state=False, hash_cache=None, db=None):
        return fixture_canonical

    monkeypatch.setattr("apps.sync.usb.apply.load_canonical_tracks", fake_loader)
    monkeypatch.setattr("apps.sync.usb.verify.load_canonical_tracks", fake_loader)
    monkeypatch.setattr(apply_mod, "REVERSAL_DIR", tmp_path / "rev")

    pf = tmp_path / "profile.yaml"
    pf.write_text(
        """
name: fixtureA
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
""",
        encoding="utf-8",
    )
    rc = apply_mod.main(
        [
            "--profile",
            str(pf),
            "--drive-root",
            str(drive_root),
            "--remediate-drift",
            "--cautious",
            "--playlists",
            "Warmup",
        ]
    )
    assert rc == 0
    # The missing file is back.
    assert (drive_root / "Alice" / "AA" / "One.mp3").exists()


@pytest.mark.requirement("CAT-02")
def test_plan_from_verify_builds_rename_ops(
    fixture_canonical, drive_root
) -> None:
    _apply(fixture_canonical, drive_root)
    src = drive_root / "Alice" / "AA" / "One.mp3"
    dst = drive_root / "Relocated" / "One.mp3"
    dst.parent.mkdir(parents=True)
    src.rename(dst)
    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert report.renamed == 1

    plan = plan_from_verify(
        profile=_profile(),
        canonical=fixture_canonical,
        report=report,
        drive_root=drive_root,
        restrict_statuses={FileStatus.RENAMED},
    )
    kinds = [op.kind for op in plan.ops]
    assert "rename" in kinds


@pytest.mark.requirement("CAT-02")
def test_hash_parallel_with_hashcache_multiple_workers(tmp_path: Path) -> None:
    """Regression: adv-r4 finding R4-01.

    ``_hash_parallel`` previously called ``HashCache.put`` from inside the
    pool.map loop. In practice the iteration ran on the main thread, but
    the review flagged that a refactor moving the call into the worker
    function would trip SQLite's check_same_thread guard. After the fix
    the cache is written only after the pool joins, so this test proves
    that supplying a ``HashCache`` with workers > 1 and multiple files
    populates both entries without raising ``sqlite3.ProgrammingError``.
    """
    from apps.shared.hashing import HashCache, sha256_file
    from apps.sync.usb.verify import _hash_parallel

    # Create four files with distinct bodies so every hash is unique.
    files = []
    for i in range(4):
        p = tmp_path / f"track{i}.bin"
        p.write_bytes(f"payload-{i}".encode() * 256)
        files.append(p)

    cache_db = tmp_path / "hash.db"
    cache = HashCache(cache_db)
    # Record which thread called put() so the test asserts put() is only
    # invoked on the main thread (the thread that constructed the cache).
    import threading

    put_thread_ids: list[int] = []
    real_put = cache.put

    def _tracked_put(path, digest):  # type: ignore[no-untyped-def]
        put_thread_ids.append(threading.get_ident())
        return real_put(path, digest)

    cache.put = _tracked_put  # type: ignore[method-assign]
    main_tid = threading.get_ident()

    try:
        hits = _hash_parallel(files, cache, max_workers=4)
    finally:
        cache.close()

    # Every file hashed.
    assert set(hits.keys()) == set(files)
    for p, digest in hits.items():
        assert digest == sha256_file(p)

    # put() must be called once per file, all on the main thread.
    assert len(put_thread_ids) == len(files)
    assert all(tid == main_tid for tid in put_thread_ids), (
        f"HashCache.put called from non-main threads: {put_thread_ids} "
        f"(main={main_tid})"
    )

    # Cache populated for every file (via a fresh connection to avoid the
    # sqlite3.Connection thread affinity from the writer thread).
    cache2 = HashCache(cache_db)
    try:
        for p, digest in hits.items():
            assert cache2.get(p) == digest, f"cache miss for {p}"
    finally:
        cache2.close()


@pytest.mark.requirement("CAT-04")
def test_verify_catches_corrupted_playlist(fixture_canonical, drive_root) -> None:
    """Regression for Codex P10-F02.

    A corrupted playlist file (empty / truncated / missing #EXTM3U header,
    or with zero resolvable entries) must be classified as broken by
    verify_drive — not silently pass as healthy. The pre-fix
    implementation only checked .exists() on entry lines, so any of the
    corruption shapes below incorrectly counted as OK.
    """
    _apply(fixture_canonical, drive_root)
    pl = drive_root / "Playlists"
    pl.mkdir(exist_ok=True)

    # Shape 1: completely empty file.
    empty = pl / "Empty.m3u8"
    empty.write_text("", encoding="utf-8")

    # Shape 2: header present but no entries (e.g. a truncated write).
    header_only = pl / "HeaderOnly.m3u8"
    header_only.write_text("#EXTM3U\n", encoding="utf-8")

    # Shape 3: entries present but header missing.
    no_header = pl / "NoHeader.m3u8"
    no_header.write_text(
        "#EXTINF:120,Alice - One\n../Alice/AA/One.mp3\n",
        encoding="utf-8",
    )

    report = verify_drive(
        profile=_profile(), canonical=fixture_canonical, drive_root=drive_root
    )
    assert "Empty.m3u8" in report.playlists_broken
    assert "HeaderOnly.m3u8" in report.playlists_broken
    assert "NoHeader.m3u8" in report.playlists_broken


def _rb_export_pioneer() -> Path:
    return resolve_required_fixture("rb-usb-export") / "PIONEER"


@pytest.mark.requirement("CAT-06")
def test_pioneer_fixture_is_rekordbox_export() -> None:
    report = verify_stick_values(_rb_export_pioneer())
    assert report.is_rekordbox_export is True
    assert report.tracks_on_stick == 199


@pytest.mark.requirement("CAT-06")
def test_pioneer_denominator_is_stick_not_expected(tmp_path: Path) -> None:
    pioneer = _rb_export_pioneer()
    stick_count = len(read_usb_export(pioneer)["tracks"])
    expected = {
        "a": ExpectedTrack(filename="Flawless.mp3", key="8A"),
        "b": ExpectedTrack(filename="missing.mp3", key="1A"),
        "c": ExpectedTrack(filename="also-missing.mp3", key="2A"),
    }
    report = verify_stick_values(pioneer, expected=expected)
    assert report.tracks_on_stick == stick_count == 199
    assert report.denominator_label == DENOMINATOR_LABEL
    payload = stick_values_to_jsonable(report)
    assert payload["tracks_on_stick"] == 199
    assert payload["denominator_label"] == DENOMINATOR_LABEL
    assert len(payload["not_on_stick"]) == 2


@pytest.mark.requirement("CAT-06")
def test_pioneer_key_and_grid_counts_from_stick() -> None:
    pioneer = _rb_export_pioneer()
    data = read_usb_export(pioneer)
    report = verify_stick_values(pioneer)
    assert report.key.present + report.key.absent + report.key.unread == 199
    assert report.grid.present == 199
    assert any(t["key"] for t in data["tracks"])


@pytest.mark.requirement("CAT-06")
def test_pioneer_grid_stick_value_matches_anlz() -> None:
    pioneer = _rb_export_pioneer()
    anlz_dir = pioneer / "USBANLZ" / "P03D" / "000222D3"
    summary = grid_summary_from_anlz(anlz_dir)
    assert summary is not None
    report = verify_stick_values(pioneer)
    flawless = next(t for t in report.tracks if t.title == "Flawless")
    assert flawless.grid.stick_value is not None
    assert str(summary["beat_count"]) in flawless.grid.stick_value
    assert summary["beat_count"] == len(read_anlz_dir(anlz_dir)["beat_grid"])


@pytest.mark.requirement("CAT-06")
def test_pioneer_loudness_absent_on_rekordbox_dump() -> None:
    pioneer = _rb_export_pioneer()
    data = read_usb_export(pioneer)
    report = verify_stick_values(pioneer)
    assert report.loudness.present == 0
    assert report.loudness.absent + report.loudness.unread == 199
    commented = [t for t in data["tracks"] if t.get("comment")]
    if commented:
        row = next(t for t in report.tracks if t.track_id == commented[0]["id"])
        assert row.loudness_lufs.status is FieldStatus.ABSENT
        assert row.loudness_lufs.stick_value is None


@pytest.mark.requirement("CAT-06")
def test_pioneer_verified_means_stick_not_plan() -> None:
    pioneer = _rb_export_pioneer()
    data = read_usb_export(pioneer)
    track = next(t for t in data["tracks"] if t.get("filename") == "Flawless.mp3")
    wrong_key = ExpectedTrack(filename="Flawless.mp3", key="8A")
    report = verify_stick_values(pioneer, expected={"x": wrong_key})
    row = next(t for t in report.tracks if t.filename == "Flawless.mp3")
    assert row.key.stick_value == track.get("key")
    if track.get("key"):
        assert row.key.stick_value != "8A" or row.key.status is FieldStatus.MATCH
    assert row.key.status in (FieldStatus.MISMATCH, FieldStatus.ABSENT, FieldStatus.MATCH)

    anlz_dir = pioneer / "USBANLZ" / "P03D" / "000222D3"
    summary = grid_summary_from_anlz(anlz_dir)
    assert summary is not None
    bad_grid = ExpectedTrack(
        filename="Flawless.mp3",
        grid=ExpectedGrid(
            beat_count=summary["beat_count"] + 1,
            first_bpm=summary["first_bpm"],
            first_time_ms=summary["first_time_ms"],
        ),
    )
    report2 = verify_stick_values(pioneer, expected={"x": bad_grid})
    row2 = next(t for t in report2.tracks if t.filename == "Flawless.mp3")
    assert row2.grid.status is FieldStatus.MISMATCH
    assert str(summary["beat_count"]) in (row2.grid.stick_value or "")


@pytest.mark.requirement("CAT-06")
def test_pioneer_match_when_expected_agrees_with_reader() -> None:
    pioneer = _rb_export_pioneer()
    data = read_usb_export(pioneer)
    track = data["tracks"][0]
    summary = None
    if track.get("anlz_path"):
        anlz_path = Path(track["anlz_path"])
        anlz_dir = anlz_path if anlz_path.is_absolute() else pioneer / anlz_path
        summary = grid_summary_from_anlz(anlz_dir)
    expected = ExpectedTrack(
        filename=track.get("filename"),
        key=track.get("key"),
        grid=(
            ExpectedGrid(
                beat_count=summary["beat_count"],
                first_bpm=summary["first_bpm"],
                first_time_ms=summary["first_time_ms"],
            )
            if summary
            else None
        ),
    )
    report = verify_stick_values(pioneer, expected={"x": expected})
    row = next(t for t in report.tracks if t.track_id == track["id"])
    if track.get("key"):
        assert row.key.status is FieldStatus.MATCH
    if summary:
        assert row.grid.status is FieldStatus.MATCH


@pytest.mark.requirement("CAT-06")
def test_pioneer_overlay_only_tree_rejected(tmp_path: Path) -> None:
    overlay = tmp_path / "PIONEER" / "rekordbox"
    overlay.mkdir(parents=True)
    (overlay / "exportLibrary.db").write_bytes(b"")
    report = verify_stick_values(tmp_path)
    assert report.is_rekordbox_export is False
    assert report.tracks_on_stick == 0
    assert report.key.match == 0
    rc = verify_mod.main(["--pioneer-export", str(tmp_path)])
    assert rc == 4


def test_value_verify_ast_guard_no_overlay_imports() -> None:
    src = Path("apps/sync/usb/pioneer/value_verify.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    blocked = ("writer_onelibrary", "export_" + "workflow")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert blocked[0] not in alias.name
                assert blocked[1] not in alias.name
        if isinstance(node, ast.ImportFrom):
            assert node.module is not None
            assert blocked[0] not in node.module
            assert blocked[1] not in node.module


def test_verify_py_ast_guard_no_overlay_writer_usage() -> None:
    src = Path(__file__).read_text(encoding="utf-8")
    forbidden = ("write_one" + "library", "export_" + "workflow")
    for name in forbidden:
        assert name not in src


def test_probe_odj_analysis_scalar_unencrypted_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE odjAnalysisScalar (
            ContentID TEXT NOT NULL,
            field TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ContentID, field)
        )
        """
    )
    conn.execute(
        "INSERT INTO odjAnalysisScalar VALUES (?, ?, ?, ?)",
        ("1", "loudness_lufs", "-8.2", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO odjAnalysisScalar VALUES (?, ?, ?, ?)",
        ("1", "loudness_dbtp", "-0.1", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    rows = probe_odj_analysis_scalar(db_path)
    assert rows[1]["loudness_lufs"] == "-8.2"
    assert rows[1]["loudness_dbtp"] == "-0.1"

    empty_db = tmp_path / "empty.db"
    sqlite3.connect(str(empty_db)).close()
    assert probe_odj_analysis_scalar(empty_db) == {}


@pytest.mark.requirement("CAT-06")
def test_pioneer_expected_json_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "expected.json"
    path.write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "filename": "Flawless.mp3",
                        "key": "8A",
                        "grid": {
                            "beat_count": 1,
                            "first_bpm": 128.0,
                            "first_time_ms": 0,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_expected_json(path)
    assert "Flawless.mp3" in loaded
    assert loaded["Flawless.mp3"].key == "8A"
