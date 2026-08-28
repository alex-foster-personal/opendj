"""SYNC-03: safety rails + path resolution for apps.sync.playlist_apply.

Ties to CONTEXT decision D5 (six-rail safety) + addresses the Phase 1.1
gap that ``--live`` must resolve to :data:`apps.shared.paths.DJAY_LIVE_DB`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.shared import paths
from apps.sync import playlist_apply as pa


@pytest.mark.requirement("SYNC-03")
def test_live_path_resolves_to_live_db_when_live_flag_true() -> None:
    assert pa._live_db_path(True) == paths.DJAY_LIVE_DB


@pytest.mark.requirement("SYNC-03")
def test_live_path_resolves_to_working_db_when_live_flag_false() -> None:
    assert pa._live_db_path(False) == paths.DJAY_WORKING_DB


def _fake_pgrep(found: bool):
    class _Result:
        returncode = 0 if found else 1
        stdout = "123\n" if found else ""

    def _run(cmd, check=False, capture_output=True, text=True):
        return _Result()

    return _run


@pytest.mark.requirement("SYNC-03")
def test_quit_check_aborts_when_djay_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(found=True))
    with pytest.raises(pa.PlaylistApplyError, match="djay"):
        pa._assert_djay_quit()


@pytest.mark.requirement("SYNC-03")
def test_quit_check_passes_when_djay_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(found=False))
    pa._assert_djay_quit()  # no raise


@pytest.mark.requirement("SYNC-03")
def test_quit_check_aborts_when_rekordbox_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(found=True))
    with pytest.raises(pa.PlaylistApplyError, match="[Rr]ekordbox"):
        pa._assert_rekordbox_quit()


@pytest.mark.requirement("SYNC-02")
def test_missing_pgrep_blocks_live_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P03-02 regression: missing pgrep must FAIL-SAFE (raise) instead
    of silently returning False, otherwise rail 2 (the
    djay/Rekordbox-running process check) is bypassed during ``--live``.
    """
    def _boom(*args, **kwargs):
        raise FileNotFoundError("no pgrep")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(pa.PlaylistApplyError, match="pgrep"):
        pa._process_running("rekordbox")
    # _assert_rekordbox_quit() must propagate the same fail-safe abort
    with pytest.raises(pa.PlaylistApplyError, match="pgrep"):
        pa._assert_rekordbox_quit()
    with pytest.raises(pa.PlaylistApplyError, match="pgrep"):
        pa._assert_djay_quit()


@pytest.mark.requirement("SYNC-03")
def test_typed_confirm_accepts_exact_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda: pa.CONFIRMATION_PHRASE)
    assert pa._typed_confirm() is True


@pytest.mark.requirement("SYNC-03")
def test_typed_confirm_aborts_on_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda: "yes")
    assert pa._typed_confirm() is False


@pytest.mark.requirement("SYNC-03")
def test_typed_confirm_aborts_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise():
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise)
    assert pa._typed_confirm() is False


@pytest.mark.requirement("SYNC-03")
def test_backup_file_is_timestamped_and_identical_to_source(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    src.write_bytes(b"hello world")
    backup_dir = tmp_path / "backups"
    dst, ts = pa._backup_djay_db(src, backup_dir)
    assert dst.parent == backup_dir
    assert ts in dst.name
    assert dst.read_bytes() == b"hello world"


@pytest.mark.requirement("SYNC-03")
def test_backup_zero_size_source_raises(tmp_path: Path) -> None:
    src = tmp_path / "empty.db"
    src.write_bytes(b"")
    with pytest.raises(pa.PlaylistApplyError):
        pa._backup_djay_db(src, tmp_path / "backups")


@pytest.mark.requirement("SYNC-03")
def test_reversal_script_restores_backup(tmp_path: Path) -> None:
    backup = tmp_path / "backup.db"
    backup.write_bytes(b"original")
    target = tmp_path / "target.db"
    target.write_bytes(b"corrupted")
    rev = pa._write_reversal_script(backup, target, tmp_path / "out", "20260417T123000")
    assert rev.exists()
    txt = rev.read_text(encoding="utf-8")
    assert "CloudKit" in txt

    import runpy

    ns = runpy.run_path(str(rev), run_name="__not_main__")
    assert ns["main"]() == 0
    assert target.read_bytes() == b"original"


@pytest.mark.requirement("SYNC-03")
def test_live_requires_i_understand_flag() -> None:
    rc = pa.main(["--live"])
    assert rc == 2


@pytest.mark.requirement("SYNC-03")
def test_playlists_and_bulk_are_mutex(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text('{"generated_at":"","match_set_sha256":"","playlists":[],"djay_only":[]}')
    rc = pa.main(
        [
            "--plan", str(plan),
            "--live", "--i-understand-the-risks",
            "--playlists", "A",
            "--bulk",
        ]
    )
    assert rc == 2


@pytest.mark.requirement("SYNC-03")
def test_missing_plan_returns_exit_2(tmp_path: Path) -> None:
    rc = pa.main(["--plan", str(tmp_path / "no.json")])
    assert rc == 2


@pytest.mark.requirement("SYNC-03")
def test_live_create_without_leaf_type_byte_is_refused() -> None:
    """Live create ops must fail cleanly when --leaf-type-byte is absent.

    The module-level ``PLAYLIST_TYPE_LEAF=0x01`` is an unconfirmed
    placeholder; writing it to the live djay DB risks corrupting the
    playlist TSAF schema. The guard in ``_apply_single_op`` raises
    :class:`PlaylistApplyError` so the per-op transaction rolls back.
    """
    op = {
        "rb_id": "r1",
        "rb_name": "Peak",
        "op": "create",
        "djay_uuid": None,
        "target_members": [],
    }
    with pytest.raises(pa.PlaylistApplyError) as exc:
        pa._apply_single_op(None, op, leaf_type_byte=None, live=True)  # type: ignore[arg-type]
    assert "--leaf-type-byte" in str(exc.value)


@pytest.mark.requirement("SYNC-03")
def test_live_create_with_leaf_type_byte_skips_guard(tmp_path: Path) -> None:
    """When --leaf-type-byte is provided, the live guard lets the op run."""
    import sqlite3

    db_path = tmp_path / "djay.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE database2 ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "collection TEXT NOT NULL, key TEXT NOT NULL, data BLOB, "
        "UNIQUE(collection, key))"
    )
    con.execute(
        "CREATE TABLE view_mediaItemPlaylistView_page ("
        "pageKey TEXT PRIMARY KEY, \"group\" TEXT NOT NULL, "
        "prevPageKey TEXT, count INTEGER NOT NULL, data BLOB)"
    )
    con.commit()
    op = {
        "rb_id": "r1",
        "rb_name": "Peak",
        "op": "create",
        "djay_uuid": None,
        "target_members": [],
    }
    # Supplying leaf_type_byte satisfies the guard; op should attempt the
    # write path (empty target_members -> 0 rowids is fine).
    result = pa._apply_single_op(con, op, leaf_type_byte=0x02, live=True)
    con.close()
    assert result.status == "written"


@pytest.mark.requirement("SYNC-03")
def test_non_live_create_without_leaf_type_byte_is_allowed(tmp_path: Path) -> None:
    """In dry-run / working-DB mode (live=False), no guard should fire.

    Existing integration tests rely on the default ``live=False`` so that
    the working DB can be exercised against the placeholder type byte.
    """
    import sqlite3

    db_path = tmp_path / "djay.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE database2 ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "collection TEXT NOT NULL, key TEXT NOT NULL, data BLOB, "
        "UNIQUE(collection, key))"
    )
    con.execute(
        "CREATE TABLE view_mediaItemPlaylistView_page ("
        "pageKey TEXT PRIMARY KEY, \"group\" TEXT NOT NULL, "
        "prevPageKey TEXT, count INTEGER NOT NULL, data BLOB)"
    )
    con.commit()
    op = {
        "rb_id": "r1",
        "rb_name": "Peak",
        "op": "create",
        "djay_uuid": None,
        "target_members": [],
    }
    result = pa._apply_single_op(con, op, leaf_type_byte=None, live=False)
    con.close()
    assert result.status == "written"


@pytest.mark.requirement("SYNC-02")
def test_verify_failure_rolls_back_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P03-01 regression: when readback verification fails the per-op
    transaction MUST ROLLBACK so the bad write is not durably committed.

    Strategy: stub ``_verify_playlist_members`` to always return
    ``(False, "synthetic mismatch")``, run ``apply_plan`` against a fresh
    djay-shaped sqlite DB, then assert (a) the op result is ``failed``
    with a ``rolled back`` message and (b) the ``database2`` /
    ``view_mediaItemPlaylistView_page`` rows the op would have inserted
    are NOT present after the call.
    """
    import sqlite3

    db_path = tmp_path / "djay.sqlite"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE database2 ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "collection TEXT NOT NULL, key TEXT NOT NULL, data BLOB, "
        "UNIQUE(collection, key))"
    )
    con.execute(
        "CREATE TABLE view_mediaItemPlaylistView_page ("
        "pageKey TEXT PRIMARY KEY, \"group\" TEXT NOT NULL, "
        "prevPageKey TEXT, count INTEGER NOT NULL, data BLOB)"
    )
    con.commit()
    con.close()

    plan = {
        "generated_at": "",
        "match_set_sha256": "",
        "playlists": [
            {
                "rb_id": "r1",
                "rb_name": "Peak",
                "op": "create",
                "djay_uuid": None,
                "target_members": [],
            }
        ],
        "djay_only": [],
    }

    monkeypatch.setattr(
        pa, "_verify_playlist_members",
        lambda con_, uuid_, expected: (False, "synthetic mismatch"),
    )

    # leaf_type_byte=0x02 satisfies the create guard in dry-run mode is
    # not needed (live=False); but pass it anyway for symmetry.
    result = pa.apply_plan(
        plan,
        db_path=db_path,
        playlist_filter=None,
        leaf_type_byte=0x02,
        live=False,
    )

    assert len(result.per_playlist) == 1
    op_result = result.per_playlist[0]
    assert op_result.status == "failed", (
        f"expected failed after verify mismatch, got {op_result.status}"
    )
    assert "rolled back" in op_result.message.lower()
    assert not result.all_verified

    # Critically: the rolled-back transaction must leave NO durable rows
    # behind. Re-open the DB and confirm both tables are empty.
    con2 = sqlite3.connect(db_path)
    n_db2 = con2.execute("SELECT COUNT(*) FROM database2").fetchone()[0]
    n_pages = con2.execute(
        "SELECT COUNT(*) FROM view_mediaItemPlaylistView_page"
    ).fetchone()[0]
    con2.close()
    assert n_db2 == 0, (
        f"database2 has {n_db2} rows after rollback; bad write was durable"
    )
    assert n_pages == 0, (
        f"view_mediaItemPlaylistView_page has {n_pages} rows after rollback"
    )
