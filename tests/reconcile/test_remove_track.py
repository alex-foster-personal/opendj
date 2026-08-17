"""Tests for :mod:`apps.reconcile.remove_track`. Ties to RECON-05.

Every test uses the per-test ``tmp_rb_db`` fixture so writes are isolated.
The module never touches the live Rekordbox DB.
"""
from __future__ import annotations

import json
import runpy
import sqlite3
import subprocess
from pathlib import Path

import pytest

from apps.reconcile import remove_track


# ------------------------------------------------------------------ helpers


def _pick_id_with_children(db_path: Path) -> str:
    """Return a DjmdContent.ID that has both a djmdCue row and a
    djmdSongPlaylist row — guarantees the cascade logic is exercised."""
    with sqlite3.connect(db_path) as con:
        cur = con.cursor()
        for (cid,) in cur.execute("SELECT ID FROM djmdContent").fetchall():
            n_cue = cur.execute(
                "SELECT COUNT(*) FROM djmdCue WHERE ContentID=?", (cid,)
            ).fetchone()[0]
            n_spl = cur.execute(
                "SELECT COUNT(*) FROM djmdSongPlaylist WHERE ContentID=?", (cid,)
            ).fetchone()[0]
            if n_cue and n_spl:
                return cid
    pytest.skip("fixture lacks a content row with both cue + playlist children")


def _count_orphans(db_path: Path, track_id: str) -> dict[str, int]:
    """Return row counts keyed by table for this ContentID."""
    tables = list(remove_track.MANUAL_CASCADE_TABLES) + [
        "djmdContent",
        "djmdCue",
        "djmdMixerParam",
    ]
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as con:
        cur = con.cursor()
        for t in tables:
            col = "ID" if t == "djmdContent" else "ContentID"
            try:
                n = cur.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE {col}=?",
                    (track_id,),
                ).fetchone()[0]
            except sqlite3.Error:
                n = 0
            counts[t] = n
    return counts


# ------------------------------------------------------------------ dry-run


@pytest.mark.requirement("RECON-05")
def test_dry_run_lists_footprint_without_touching_db(
    tmp_rb_db: Path, tmp_path: Path
) -> None:
    """Dry-run writes a plan JSON + never mutates the DB."""
    tid = _pick_id_with_children(tmp_rb_db)
    before = _count_orphans(tmp_rb_db, tid)
    plan_dir = tmp_path / "plan"
    rc = remove_track.main(
        [
            "--dry-run",
            "--tracks",
            tid,
            "--db",
            str(tmp_rb_db),
            "--plan-dir",
            str(plan_dir),
            "--reason",
            "unit-test",
        ]
    )
    assert rc == 0
    after = _count_orphans(tmp_rb_db, tid)
    assert before == after, "dry-run must not mutate the DB"

    # Exactly one plan file was written and it has the expected shape.
    plans = list(plan_dir.glob("remove-plan-*.json"))
    assert len(plans) == 1
    payload = json.loads(plans[0].read_text())
    assert len(payload) == 1
    entry = payload[0]
    assert entry["id"] == tid
    assert entry["reason"] == "unit-test"
    for key in (
        "title",
        "artist",
        "folder_path",
        "playlists",
        "cue_points",
        "beatgrid_entries",
        "analysis_entries",
    ):
        assert key in entry


# ------------------------------------------------------------------ CLI rails


@pytest.mark.requirement("RECON-05")
def test_live_requires_risk_flag(tmp_rb_db: Path, tmp_path: Path) -> None:
    """``--live --tracks X`` without ``--i-understand-the-risks`` aborts (exit 2)."""
    tid = _pick_id_with_children(tmp_rb_db)
    rc = remove_track.main(
        [
            "--live",
            "--tracks",
            tid,
            "--db",
            str(tmp_rb_db),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 2
    # DB must still contain the row.
    assert _count_orphans(tmp_rb_db, tid)["djmdContent"] == 1


@pytest.mark.requirement("RECON-05")
def test_live_rejects_more_than_3_tracks(tmp_rb_db: Path, tmp_path: Path) -> None:
    """CLI-level guard: more than MAX_LIVE_TRACKS → exit 2."""
    ids = ",".join(str(i) for i in range(remove_track.MAX_LIVE_TRACKS + 1))
    rc = remove_track.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            ids,
            "--db",
            str(tmp_rb_db),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 2


@pytest.mark.requirement("RECON-05")
def test_tracks_flag_is_required(tmp_rb_db: Path) -> None:
    """Neither dry-run nor live may run without ``--tracks``."""
    rc = remove_track.main(["--dry-run", "--db", str(tmp_rb_db)])
    assert rc == 2


# ------------------------------------------------------------------ running RB


@pytest.mark.requirement("RECON-05")
def test_live_aborts_when_rekordbox_running(
    tmp_rb_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pgrep says RB is running, abort with exit 3 BEFORE any write."""
    tid = _pick_id_with_children(tmp_rb_db)
    monkeypatch.setattr(remove_track, "_rekordbox_running", lambda: True)
    rc = remove_track.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            tid,
            "--db",
            str(tmp_rb_db),
            "--backup-dir",
            str(tmp_path / "bk"),
        ]
    )
    assert rc == 3
    # DB untouched.
    assert _count_orphans(tmp_rb_db, tid)["djmdContent"] == 1


@pytest.mark.requirement("RECON-05")
def test_rekordbox_running_check_uses_pgrep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_rekordbox_running`` keys off pgrep's return code + stdout."""

    class _FakeResult:
        def __init__(self, code: int, stdout: str) -> None:
            self.returncode = code
            self.stdout = stdout

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeResult(0, "1 rb\n"))
    assert remove_track._rekordbox_running() is True
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeResult(1, ""))
    assert remove_track._rekordbox_running() is False


# ------------------------------------------------------------------ write path


@pytest.mark.requirement("RECON-05")
def test_live_removes_row_and_cascades(
    tmp_rb_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy-path live removal deletes the row + all cascade orphans."""
    tid = _pick_id_with_children(tmp_rb_db)
    # Bypass interactive confirm + RB-running check.
    monkeypatch.setattr(remove_track, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(remove_track, "_confirm", lambda: True)
    # pyrekordbox's Rekordbox6Database.commit() calls get_rekordbox_pid()
    # internally and raises if RB is running on the host. Our app-layer
    # gate is already patched above; also stub the library-level check so
    # commits succeed inside the test harness.
    monkeypatch.setattr(
        "pyrekordbox.db6.database.get_rekordbox_pid", lambda: 0
    )

    before = _count_orphans(tmp_rb_db, tid)
    assert before["djmdContent"] == 1

    backup_dir = tmp_path / "bk"
    rc = remove_track.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            tid,
            "--db",
            str(tmp_rb_db),
            "--backup-dir",
            str(backup_dir),
            "--plan-dir",
            str(tmp_path / "plan"),
            "--reason",
            "cascade-test",
        ]
    )
    assert rc == 0

    after = _count_orphans(tmp_rb_db, tid)
    assert after["djmdContent"] == 0, "content row still present"
    # Every dependent table must be clean (cascade or manual cleanup).
    for tbl, n in after.items():
        assert n == 0, f"orphans remain in {tbl}: {n}"


# ------------------------------------------------------------------ backup


@pytest.mark.requirement("RECON-05")
def test_backup_is_taken_before_delete(
    tmp_rb_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``master.*.db`` backup must exist + be non-empty before any write."""
    tid = _pick_id_with_children(tmp_rb_db)

    # Capture the DB state seen at the exact moment we'd start removing,
    # which proves the backup was taken BEFORE any delete.
    captured: dict[str, int] = {}

    _orig_apply = remove_track._apply_removals

    def _spy_apply_removals(db_path, footprints):
        # At this point the backup should already exist.
        backups = list((tmp_path / "bk").glob("master.*.db"))
        captured["backups_at_apply"] = len(backups)
        captured["backup_size"] = (
            backups[0].stat().st_size if backups else 0
        )
        # Also capture the row count so we know the DB is still intact here.
        with sqlite3.connect(db_path) as con:
            captured["content_rows"] = con.execute(
                "SELECT COUNT(*) FROM djmdContent WHERE ID=?", (tid,)
            ).fetchone()[0]
        return _orig_apply(db_path, footprints)

    monkeypatch.setattr(remove_track, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(remove_track, "_confirm", lambda: True)
    monkeypatch.setattr(remove_track, "_apply_removals", _spy_apply_removals)
    # See note in test_live_removes_row_and_cascades: also stub pyrekordbox's
    # internal pid check so commit() works when RB is running on the host.
    monkeypatch.setattr(
        "pyrekordbox.db6.database.get_rekordbox_pid", lambda: 0
    )

    rc = remove_track.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            tid,
            "--db",
            str(tmp_rb_db),
            "--backup-dir",
            str(tmp_path / "bk"),
            "--plan-dir",
            str(tmp_path / "plan"),
        ]
    )
    assert rc == 0
    assert captured.get("backups_at_apply") == 1
    assert captured.get("backup_size", 0) > 0
    # DB is still intact at the point the spy ran.
    assert captured.get("content_rows") == 1


# ------------------------------------------------------------------ reversal


@pytest.mark.requirement("RECON-05")
def test_reversal_script_restores_row(
    tmp_rb_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the generated restore-*.py re-inserts the removed rows."""
    tid = _pick_id_with_children(tmp_rb_db)
    monkeypatch.setattr(remove_track, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(remove_track, "_confirm", lambda: True)
    # See note in test_live_removes_row_and_cascades: also stub pyrekordbox's
    # internal pid check so commit() works when RB is running on the host.
    monkeypatch.setattr(
        "pyrekordbox.db6.database.get_rekordbox_pid", lambda: 0
    )

    before = _count_orphans(tmp_rb_db, tid)

    backup_dir = tmp_path / "bk"
    rc = remove_track.main(
        [
            "--live",
            "--i-understand-the-risks",
            "--tracks",
            tid,
            "--db",
            str(tmp_rb_db),
            "--backup-dir",
            str(backup_dir),
            "--plan-dir",
            str(tmp_path / "plan"),
        ]
    )
    assert rc == 0

    # Confirm removed.
    assert _count_orphans(tmp_rb_db, tid)["djmdContent"] == 0

    # Run the generated restore-*.py against the same tmp DB.
    scripts = list(backup_dir.glob("restore-*.py"))
    assert len(scripts) == 1
    script = scripts[0]
    # Must compile cleanly.
    compile(script.read_text(), str(script), "exec")
    # The script embeds the DB path it captured at removal time (= tmp_rb_db),
    # so it targets the correct DB without patching. It calls sys.exit(0)
    # on success — catch that.
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0, f"restore script exited {exc.code}"

    # After restoring, all the removed tables should come back.
    after = _count_orphans(tmp_rb_db, tid)
    # Content row is back.
    assert after["djmdContent"] == before["djmdContent"] == 1
    # Manual cascade rows should be fully restored (we captured them).
    for tbl in remove_track.MANUAL_CASCADE_TABLES:
        assert after[tbl] == before[tbl], (
            f"{tbl}: before={before[tbl]} after={after[tbl]}"
        )
    # ORM-cascaded tables should also be restored.
    for tbl in ("djmdCue", "djmdMixerParam"):
        assert after[tbl] == before[tbl], (
            f"{tbl}: before={before[tbl]} after={after[tbl]}"
        )


# ------------------------------------------------------------------ misc


@pytest.mark.requirement("RECON-05")
def test_parse_ids_handles_whitespace_and_empty() -> None:
    assert remove_track._parse_ids(None) is None
    assert remove_track._parse_ids("") is None
    assert remove_track._parse_ids("  185718719 , 2 ,3,,") == [
        "185718719", "2", "3",
    ]


@pytest.mark.requirement("RECON-05")
def test_dry_run_warns_on_missing_id(
    tmp_rb_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """IDs that don't exist in the DB are called out but don't crash."""
    rc = remove_track.main(
        [
            "--dry-run",
            "--tracks",
            "999999999",
            "--db",
            str(tmp_rb_db),
            "--plan-dir",
            str(tmp_path / "plan"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "999999999" in out
    # Plan file is written but with zero entries (no existing IDs).
    plans = list((tmp_path / "plan").glob("remove-plan-*.json"))
    assert len(plans) == 1
    assert json.loads(plans[0].read_text()) == []
