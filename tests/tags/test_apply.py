"""Tests for ``apps.tags.apply``."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.shared.tag_writer import TagRead, read_tags
from apps.tags import apply as tags_apply

# tag write path needs the tags extra; skip (never fail) when absent.


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.requirement("META-01")
def test_dry_run_no_mutation(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=True,
    )
    assert _sha(dst) == before
    assert res["applied_count"] == 0


@pytest.mark.requirement("META-01")
def test_live_writes_tags_and_creates_backup(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - TitleX.mp3"
    shutil.copy2(src, dst)

    backup_root = tmp_path / "backups"
    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=backup_root,
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(
            title="Unified", artist="UArt", genre="Techno", bpm=128.0
        ),
    )
    assert res["applied_count"] == 1, f"unexpected result: {res}"
    assert not res["errors"], res["errors"]

    # Re-read to confirm the write landed.
    got = read_tags(dst)
    assert got.title == "Unified"
    assert got.artist == "UArt"
    assert got.genre == "Techno"
    assert got.bpm == 128.0

    # Backup exists.
    backups = list(backup_root.rglob("*.mp3"))
    assert backups, "expected backup file"


@pytest.mark.requirement("META-01")
def test_provenance_rows_inserted(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)

    dedup_db = tmp_path / "phase7.sqlite"
    stable_id = "stable-abc"
    res = tags_apply.run_apply(
        files=[dst],
        stable_id_lookup={str(dst): stable_id},
        dedup_db_path=dedup_db,
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(genre="Techno", bpm=128.0),
    )
    assert res["applied_count"] == 1
    # Query the tag_provenance table.
    conn = sqlite3.connect(dedup_db)
    try:
        count = conn.execute(
            "SELECT count(*) FROM tag_provenance WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count >= 2  # at least genre + bpm + whatever else unified


@pytest.mark.requirement("META-01")
def test_reversal_script_restores_original(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="Mutated", artist="Other"),
    )
    assert res["applied_count"] == 1
    # File now differs from original.
    assert _sha(dst) != before

    # Execute the reversal script via shell.
    import subprocess

    # Windows ships a stub System32\bash.exe that fails without WSL, so
    # "on PATH" is not enough -- require a bash that actually runs.
    bash = shutil.which("bash")
    if bash is None or subprocess.run(
        [bash, "-c", "true"], capture_output=True, check=False
    ).returncode != 0:
        pytest.skip("functional bash unavailable; cannot execute the reversal script")
    rc = subprocess.run(
        ["bash", res["reversal"]], capture_output=True, text=True, check=False
    )
    assert rc.returncode == 0, f"reversal failed: {rc.stderr}"
    # File restored.
    assert _sha(dst) == before


@pytest.mark.requirement("META-01")
def test_abort_if_rb_running(tmp_path: Path, monkeypatch) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "x.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    monkeypatch.setattr(
        tags_apply,
        "_is_app_running",
        lambda name: name == "rekordbox",
    )
    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=False,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="X"),
    )
    assert res["applied_count"] == 0
    assert any("rekordbox running" in e for e in res["errors"])
    assert _sha(dst) == before


@pytest.mark.requirement("META-01")
def test_icloud_placeholder_skip(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "cloud.mp3"
    shutil.copy2(src, dst)
    # Create the .icloud sibling placeholder marker.
    sib = dst.with_name("." + dst.name + ".icloud")
    sib.write_text("placeholder")

    res = tags_apply.run_apply(
        files=[dst],
        dedup_db_path=tmp_path / "phase7.sqlite",
        backup_root=tmp_path / "backups",
        reversal_dir=tmp_path / "reversals",
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="X"),
    )
    assert res["applied_count"] == 0
    assert any("iCloud" in e for e in res["errors"])


@pytest.mark.requirement("META-01")
def test_cli_live_requires_i_understand(
    tmp_path: Path, capsys
) -> None:
    dst = tmp_path / "missing.mp3"
    rc = tags_apply.main([str(dst), "--live"])
    assert rc == 2
    assert "i-understand-the-risks" in capsys.readouterr().err


@pytest.mark.requirement("META-03")
def test_allow_app_running_keeps_backup_rail(tmp_path: Path) -> None:
    """`allow_app_running` must only disable the process-check rail.

    The backup rail (copy-before-write under ``backup_root``) must still
    fire on every live write. Regression for codex finding P07-01.
    """
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)

    backup_root = tmp_path / "backups"
    res = tags_apply.apply_one(
        dst,
        backup_root=backup_root,
        allow_app_running=True,
        dry_run=False,
        fetch_rb=lambda p: TagRead(title="Unified", artist="UArt"),
    )
    assert res.error is None, res.error
    assert res.backup is not None
    assert res.backup.exists()
    assert list(backup_root.rglob("*.mp3"))


@pytest.mark.requirement("META-03")
def test_allow_app_running_rejected_outside_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allow_app_running=True` must raise when not running under pytest.

    It is a test-only escape hatch; production callers must not bypass
    the running-app safety check. Regression for codex finding P07-01.
    """
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "x.mp3"
    shutil.copy2(src, dst)

    monkeypatch.setattr(tags_apply, "_in_pytest", lambda: False)

    with pytest.raises(RuntimeError, match="test-only"):
        tags_apply.apply_one(
            dst,
            backup_root=tmp_path / "backups",
            allow_app_running=True,
            dry_run=True,
        )
