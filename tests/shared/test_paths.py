"""Tests for :mod:`apps.shared.paths`.

Ties to INFRA-02 (shared modules usable across apps).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared import paths


@pytest.mark.requirement("INFRA-02")
def test_parse_music_roots_strips_entries_and_expands_user_home() -> None:
    """Configured roots are whitespace-normalized and support ``~`` paths."""
    configured_roots = f"  ~/Music  {os.pathsep} /Volumes/DJ Library "

    assert paths._parse_music_roots(configured_roots) == [
        Path("~/Music").expanduser(),
        Path("/Volumes/DJ Library"),
    ]


@pytest.mark.requirement("INFRA-02")
@pytest.mark.parametrize("configured_roots", ["", "   ", os.pathsep])
def test_parse_music_roots_rejects_configurations_without_usable_entries(
    configured_roots: str,
) -> None:
    """A present but unusable override fails before a zero-root scan can run."""
    with pytest.raises(ValueError, match="MDT_MUSIC_ROOTS"):
        paths._parse_music_roots(configured_roots)


@pytest.mark.requirement("INFRA-02")
def test_constants_are_pathlib_paths() -> None:
    """All exported path constants are ``Path`` objects, not strings."""
    for const in (
        paths.HOME,
        paths.PROJECT_ROOT,
        paths.DATA_DIR,
        paths.REKORDBOX_LIVE_DB,
        paths.REKORDBOX_WORKING_DB,
        paths.DJAY_LIVE_DB,
        paths.DJAY_WORKING_DB,
    ):
        assert isinstance(const, Path)


@pytest.mark.requirement("INFRA-02")
def test_project_root_contains_apps_dir() -> None:
    """``PROJECT_ROOT`` is the dir above ``apps/`` — apps/shared lives under it."""
    assert (paths.PROJECT_ROOT / "apps" / "shared").is_dir()


@pytest.mark.requirement("INFRA-02")
def test_audio_extensions_all_lowercase_with_dot() -> None:
    """Extension constants are normalized for direct `os.path.splitext` compare."""
    assert paths.AUDIO_EXTENSIONS
    for ext in paths.AUDIO_EXTENSIONS:
        assert ext.startswith("."), ext
        assert ext == ext.lower(), ext


@pytest.mark.requirement("INFRA-02")
def test_copy_live_dbs_is_idempotent_and_handles_missing_djay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling ``copy_live_dbs`` twice doesn't raise; missing djay → ``None``.

    We redirect all path constants into ``tmp_path`` so no real live DB is
    touched; then seed a fake encrypted Rekordbox DB + no djay DB.
    """
    fake_live_rb = tmp_path / "rb_live" / "master.db"
    fake_live_rb.parent.mkdir()
    fake_live_rb.write_bytes(b"SQLITE-LIKE-PAYLOAD")

    fake_data = tmp_path / "data"
    fake_data.mkdir()

    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", fake_live_rb)
    monkeypatch.setattr(paths, "REKORDBOX_WORKING_DB", fake_data / "master.db.copy")
    monkeypatch.setattr(paths, "DJAY_LIVE_DB", tmp_path / "nope" / "MediaLibrary.db")
    monkeypatch.setattr(paths, "DJAY_WORKING_DB", fake_data / "djay.db.copy")
    monkeypatch.setattr(paths, "DATA_DIR", fake_data)

    first = paths.copy_live_dbs()
    second = paths.copy_live_dbs()

    assert first["rekordbox"] == fake_data / "master.db.copy"
    assert first["djay"] is None, "missing djay should degrade gracefully"
    assert second == first, "second call should return same mapping"
    assert (fake_data / "master.db.copy").read_bytes() == b"SQLITE-LIKE-PAYLOAD"


@pytest.mark.requirement("INFRA-02")
def test_copy_live_dbs_returns_none_for_both_when_both_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both missing → dict with both values ``None`` (no exception)."""
    monkeypatch.setattr(paths, "REKORDBOX_LIVE_DB", tmp_path / "no-rb.db")
    monkeypatch.setattr(paths, "DJAY_LIVE_DB", tmp_path / "no-djay.db")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    out = paths.copy_live_dbs()
    assert out == {"rekordbox": None, "djay": None}


@pytest.mark.live_db
@pytest.mark.requirement("INFRA-02")
def test_live_db_gate_is_skipped_without_flag() -> None:
    """Sentinel test — confirms live_db-marked tests are gated out by default.

    Running without ``--live-db`` skips this test. If you see it PASS in a
    default run, the ``pytest_collection_modifyitems`` gate in
    ``scripts/pytest_reqs_plugin.py`` is broken.
    """
    assert paths.REKORDBOX_LIVE_DB.exists(), "would only pass when --live-db given"


@pytest.mark.requirement("INFRA-02")
def test_paths_reexports_every_previously_public_name() -> None:
    """Windows-portability refactor moved OS-branching into platform_paths;

    every name paths.py exported before that refactor must still resolve
    here so no consumer's ``from apps.shared.paths import ...`` breaks.
    """
    expected_names = (
        "HOME",
        "PROJECT_ROOT",
        "DATA_DIR",
        "STATE_DIR",
        "STATE_DB",
        "REKORDBOX_LIVE_DB",
        "REKORDBOX_WORKING_DB",
        "DJAY_LIVE_DB",
        "DJAY_WORKING_DB",
        "MUSIC_ROOTS",
        "AUDIO_EXTENSIONS",
        "copy_live_dbs",
        "DEDUP_DIR",
        "DEDUP_FALLBACK_DB",
        "DEDUP_CLUSTERS_CSV",
        "DEDUP_MANUAL_REVIEW_CSV",
        "DEDUP_REWRITE_PLAN_CSV",
        "DEDUP_REWRITE_SUMMARY_MD",
        "DEDUP_ARCHIVE_ROOT",
        "TAGS_DIR",
        "TAGS_BACKUPS_DIR",
        "TAGS_UNIFIED_PREVIEW_CSV",
        "TAGS_REVERSAL_DIR",
    )
    for name in expected_names:
        assert hasattr(paths, name), f"apps.shared.paths lost export: {name}"


@pytest.mark.requirement("INFRA-02")
def test_music_roots_is_nonempty_list_of_paths() -> None:
    """MUSIC_ROOTS (PR #133 logic, re-exported from platform_paths) is usable."""
    assert isinstance(paths.MUSIC_ROOTS, list)
    assert paths.MUSIC_ROOTS
    for root in paths.MUSIC_ROOTS:
        assert isinstance(root, Path)


@pytest.mark.requirement("INFRA-02")
def test_music_roots_respects_mdt_music_roots_env() -> None:
    """MDT_MUSIC_ROOTS (os.pathsep-split) overrides the default ``~/Music``.

    MUSIC_ROOTS is a module-level constant computed once at import (PR #133
    logic), so we exercise this in a fresh subprocess rather than mutating
    the already-imported, process-wide ``apps.shared.platform_paths`` module
    (which would leak into other tests via the shared ``sys.modules`` entry).
    """
    env = dict(os.environ)
    env["MDT_MUSIC_ROOTS"] = f"/tmp/a{os.pathsep}/tmp/b"
    proc = subprocess.run(
        [sys.executable, "-c", "from apps.shared.paths import MUSIC_ROOTS; print(MUSIC_ROOTS)"],
        cwd=paths.PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == str([Path("/tmp/a"), Path("/tmp/b")])
