"""Phase 5 + Phase 17 handshake tests for ``bootstrap_db.py``.

Validates the Phase 5 readiness behaviour of the launcher bootstrap:

* When ``state.db`` exists with the core Phase 5 ``tracks`` table, the
  bootstrap no-ops (prints ``SHARED_STATE_READY``) and leaves the core
  tables untouched.
* When ``state.db`` exists but is missing the launcher extensions
  (``tracks_fts``, ``tracks_frecency``), the bootstrap adds them via a
  launcher-scoped additive migration, idempotently.
* When ``state.db`` does not exist at all, the bootstrap builds the
  fallback ``launcher-bootstrap.sqlite`` from whatever sources are
  available.

[if] launcher bootstrap runs against shared state.db [then] Phase 5 tables and FTS extensions stay idempotent per LAUNCH-01, [else stop].
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db

pytestmark = [
    pytest.mark.requirement("LAUNCH-01"),
    pytest.mark.requirement("INFRA-01"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_bootstrap_module():
    """Load ``apps/launcher/scripts/bootstrap_db.py`` as a module."""
    path = REPO_ROOT / "apps" / "launcher" / "scripts" / "bootstrap_db.py"
    spec = importlib.util.spec_from_file_location(
        "_launcher_bootstrap_phase5", path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_launcher_bootstrap_phase5"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandboxed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path]]:
    """Monkey-patch SHARED_DB / BOOTSTRAP_DB so tests never touch the repo."""
    bootstrap = _load_bootstrap_module()
    shared = tmp_path / "state" / "state.db"
    shared.parent.mkdir(parents=True)
    boot = tmp_path / "launcher-bootstrap.sqlite"
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DB", boot)
    yield shared, boot


# --- 1. state.db with tracks -> noop --------------------------------------

def test_shared_state_with_tracks_noops(
    sandboxed_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, boot = sandboxed_paths
    # Build a Phase 5 state.db via the real migration code so the tracks
    # table is guaranteed to match what Phase 5 ships.
    conn = state_db.open_rw(shared)
    conn.close()
    # sys.argv must NOT include pytest args when calling .main()
    monkeypatch.setattr(sys, "argv", ["bootstrap_db"])

    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DB", boot)

    rc = bootstrap.main()
    assert rc == 0
    out = capsys.readouterr()
    assert "SHARED_STATE_READY" in out.out
    # Fallback DB was never created.
    assert not boot.exists()
    # Core Phase 5 tables are still there, untouched.
    check = sqlite3.connect(str(shared))
    try:
        names = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        check.close()
    assert "tracks" in names
    assert "playlists" in names


# --- 2. state.db without FTS -> migration adds it --------------------------

def test_shared_state_missing_fts_adds_extensions(
    sandboxed_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, _ = sandboxed_paths
    # Phase 5 state.db with no launcher extensions.
    conn = state_db.open_rw(shared)
    conn.close()
    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)

    # Before: no tracks_fts / tracks_frecency.
    check = sqlite3.connect(str(shared))
    try:
        before = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
    finally:
        check.close()
    assert "tracks_fts" not in before
    assert "tracks_frecency" not in before

    added = bootstrap.apply_launcher_migration(shared)
    assert added["tracks_fts_created"] is True
    assert added["tracks_frecency_created"] is True
    # Empty Phase 5 tracks table -> nothing to backfill.
    assert added["tracks_fts_backfilled"] == 0

    check = sqlite3.connect(str(shared))
    try:
        after = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        # Launcher extensions now present.
        assert "tracks_fts" in after
        assert "tracks_frecency" in after
        # Phase 5 core tables still present and unmodified.
        assert "tracks" in after
        assert "playlists" in after
        # Can actually use the index.
        check.execute(
            "INSERT INTO tracks_frecency(stable_id, plays) VALUES (?, ?)",
            ("sid-x", 1),
        )
    finally:
        check.close()


def test_launcher_migration_is_idempotent(
    sandboxed_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, _ = sandboxed_paths
    conn = state_db.open_rw(shared)
    conn.close()
    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)

    first = bootstrap.apply_launcher_migration(shared)
    assert first["tracks_fts_created"] is True
    assert first["tracks_frecency_created"] is True

    second = bootstrap.apply_launcher_migration(shared)
    assert second["tracks_fts_created"] is False
    assert second["tracks_frecency_created"] is False
    assert second["tracks_fts_backfilled"] == 0
    # A third call is still safe.
    third = bootstrap.apply_launcher_migration(shared)
    assert third["tracks_fts_created"] is False
    assert third["tracks_frecency_created"] is False
    assert third["tracks_fts_backfilled"] == 0


def test_apply_launcher_migration_skips_when_missing(
    tmp_path: Path,
) -> None:
    bootstrap = _load_bootstrap_module()
    missing = tmp_path / "does_not_exist.db"
    # Should not raise or create the file.
    out = bootstrap.apply_launcher_migration(missing)
    assert out["tracks_fts_created"] is False
    assert out["tracks_frecency_created"] is False
    assert out["tracks_fts_backfilled"] == 0
    assert not missing.exists()


def test_main_with_shared_state_applies_extensions(
    sandboxed_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, boot = sandboxed_paths
    conn = state_db.open_rw(shared)
    conn.close()
    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DB", boot)
    monkeypatch.setattr(sys, "argv", ["bootstrap_db"])

    rc = bootstrap.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "SHARED_STATE_READY" in captured.out
    assert "added launcher extensions" in captured.err

    # Second run announces the extensions are already present.
    rc2 = bootstrap.main()
    assert rc2 == 0
    cap2 = capsys.readouterr()
    assert "already present" in cap2.err


# --- 3. state.db missing -> bootstrap fallback -----------------------------

def test_no_shared_state_falls_back_to_bootstrap(
    sandboxed_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, boot = sandboxed_paths
    assert not shared.exists()
    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DB", boot)
    monkeypatch.setattr(sys, "argv", ["bootstrap_db"])

    # Fabricate readable rows so build_db actually runs.
    fake_rows = [
        {
            "path": "/music/a.mp3", "title": "A", "artist": "X",
            "album": "", "genre": "", "key": None, "bpm": 120.0,
            "duration_ms": 200_000, "isrc": None, "source": "rekordbox",
        },
        {
            "path": "/music/b.mp3", "title": "B", "artist": "Y",
            "album": "", "genre": "", "key": None, "bpm": 125.0,
            "duration_ms": 210_000, "isrc": None, "source": "djay",
        },
    ]
    monkeypatch.setattr(bootstrap, "_iter_rekordbox", lambda: iter(fake_rows[:1]))
    monkeypatch.setattr(bootstrap, "_iter_djay", lambda: iter(fake_rows[1:]))

    rc = bootstrap.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "BOOTSTRAP_OK" in out
    assert boot.exists()

    check = sqlite3.connect(str(boot))
    try:
        names = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        assert "tracks" in names
        assert "tracks_fts" in names
        assert "tracks_frecency" in names
        assert check.execute(
            "SELECT COUNT(*) FROM tracks"
        ).fetchone()[0] == 2
    finally:
        check.close()


def test_shared_state_without_tracks_falls_through(
    sandboxed_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state.db exists but has no tracks table -> bootstrap fallback."""
    shared, boot = sandboxed_paths
    # Create a DB with some table but NOT tracks.
    conn = sqlite3.connect(str(shared))
    conn.execute("CREATE TABLE noise (x INTEGER)")
    conn.close()

    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DB", boot)
    monkeypatch.setattr(sys, "argv", ["bootstrap_db"])
    monkeypatch.setattr(
        bootstrap, "_iter_rekordbox", lambda: iter(()),
    )
    monkeypatch.setattr(
        bootstrap, "_iter_djay", lambda: iter(()),
    )

    rc = bootstrap.main()
    # No rows at all -> exit 2 after reporting the fall-through.
    assert rc == 2
    captured = capsys.readouterr()
    assert "no tracks table" in captured.err
    assert "no tracks found" in captured.err


# --- 4. P17-01: FTS backfill is part of the migration path ----------------

@pytest.mark.requirement("LAUNCH-01")
def test_bootstrap_db_runs_fts_backfill_after_migration(
    sandboxed_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for codex P17-01.

    A Phase 5 ``state.db`` that already has rows in ``tracks`` but no
    ``tracks_fts`` (the launcher attaching for the first time) must end
    up with a populated FTS index after the launcher migration runs.
    Before the fix the migration only created the virtual table and
    every palette search returned zero rows even though the launcher
    reported SHARED_STATE_READY.
    """
    shared, _ = sandboxed_paths
    # Build a Phase 5-shaped tracks table by hand -- only the columns
    # the as-shipped Phase 5 schema guarantees -- and seed rows.
    conn = sqlite3.connect(str(shared))
    try:
        conn.execute(
            "CREATE TABLE tracks ("
            " stable_id TEXT PRIMARY KEY,"
            " title TEXT,"
            " album TEXT,"
            " file_path TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracks(stable_id, title, album, file_path)"
            " VALUES (?, ?, ?, ?)",
            [
                ("sid-1", "Lanterns", "Paper Harbors",
                 "/m/lanterns.mp3"),
                ("sid-2", "Glass Orchard", "Random Album Title",
                 "/m/glass.mp3"),
                ("sid-3", "Salt Flats", "Salt Flats",
                 "/m/salt.mp3"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    bootstrap = _load_bootstrap_module()
    monkeypatch.setattr(bootstrap, "SHARED_DB", shared)

    out = bootstrap.apply_launcher_migration(shared)
    assert out["tracks_fts_created"] is True
    assert out["tracks_frecency_created"] is True
    assert out["tracks_fts_backfilled"] == 3

    check = sqlite3.connect(str(shared))
    try:
        n_fts = check.execute("SELECT COUNT(*) FROM tracks_fts").fetchone()[0]
        assert n_fts == 3
        hits = [
            r[0] for r in check.execute(
                "SELECT title FROM tracks_fts WHERE tracks_fts MATCH ?",
                ("lanterns",),
            )
        ]
        assert "Lanterns" in hits
    finally:
        check.close()

    # Idempotent: second call does not re-backfill.
    again = bootstrap.apply_launcher_migration(shared)
    assert again["tracks_fts_created"] is False
    assert again["tracks_fts_backfilled"] == 0
