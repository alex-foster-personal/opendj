"""RBXAN-8322b: the guarded direct write into a real-schema Rekordbox library.

[if] a guard refuses or the write fails after the backup [then] nothing is written or master.db is restored byte for byte, [else stop].

Each guard refuses with nothing written; a failure after the backup restores
``master.db`` and ``masterPlaylists6.xml`` byte for byte.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.rekordbox_analyse import writer as writer_mod
from apps.rekordbox_analyse.plan import ANALYSE_ME_PLAYLIST, VIVA_TARGET_PLAYLIST, OdjTrack, build_plan
from apps.rekordbox_analyse.snapshot import sha256_file, snapshot_rekordbox
from apps.rekordbox_analyse.writer import AnalyseApplyFailed, AnalyseApplyRefused, apply_plan
from apps.shared.rekordbox_writeback import REKORDBOX_WRITEBACK_ENABLED_ENV, RekordboxWritebackDisabled
from tests.rekordbox_analyse.rb_fixture import FixturePlaylist, FixtureTrack, build_library

pytestmark = pytest.mark.requirement("RBXAN-8322b")


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # pyrekordbox's commit() refuses while a real Rekordbox runs on this host;
    # the fixture library is not that Rekordbox, so its own pid probe is isolated.
    monkeypatch.setattr("pyrekordbox.db6.database.get_rekordbox_pid", lambda: 0)
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, "1")
    music = tmp_path / "music"
    music.mkdir()
    for name in ("new.mp3", "viva.mp3"):
        (music / name).write_bytes(b"\xff\xfb" + b"\0" * 200)
    lib = build_library(
        tmp_path / "rb",
        [
            FixtureTrack("10", str(music / "queued.mp3"), analysed=False),
            FixtureTrack("11", str(music / "done.mp3"), analysed=True),
        ],
        [
            FixturePlaylist("501", ANALYSE_ME_PLAYLIST, ("10", "11")),
            FixturePlaylist("502", VIVA_TARGET_PLAYLIST, ()),
        ],
    )
    odj = [
        OdjTrack("s-new", str(music / "new.mp3")),
        OdjTrack("s-viva", str(music / "viva.mp3"), ("Viva - The cut",)),
    ]
    return lib, odj, tmp_path / "backups"


def _plan(lib, odj):
    rb = snapshot_rekordbox(lib.master_db)
    return build_plan(odj, rb.tracks, rb.playlists, rb.master_db_sha256), rb


def _apply(lib, plan, rb, backups, **kw):
    args = dict(
        plan_sha=plan.plan_sha,
        live_db=lib.master_db,
        backups_root=backups,
        before=rb,
        cloud_sync_off_confirmed=True,
        process_check=list,
    )
    args.update(kw)
    return apply_plan(plan, **args)


def _bytes(lib) -> tuple[bytes, bytes]:
    return lib.master_db.read_bytes(), (lib.rb_dir / "masterPlaylists6.xml").read_bytes()


def test_apply_adds_queues_files_and_drops_analysed(library):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    report = _apply(lib, plan, rb, backups)
    after = snapshot_rekordbox(lib.master_db)
    by_name = {pl.name: pl for pl in after.playlists}
    paths = {t.folder_path: t.content_id for t in after.tracks}
    new_ids = {paths[p] for p in plan.add_paths}
    assert set(by_name[ANALYSE_ME_PLAYLIST].member_ids) == {"10"} | new_ids, (
        "if ANALYSE-ME does not hold the new and still-unanalysed tracks only then broken"
    )
    assert paths[odj[1].file_path] in by_name[VIVA_TARGET_PLAYLIST].member_ids, "if Viva filing is lost then broken"
    assert report.integrity == "ok" and report.added == 2 and report.playlist_removed == 1
    assert Path(report.backup_dir, "master.db").is_file() and Path(report.backup_dir, "masterPlaylists6.xml").is_file()


def test_second_plan_after_apply_is_noop(library):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    _apply(lib, plan, rb, backups)
    again, _ = _plan(lib, odj)
    assert again.is_noop, "if a re-plan after apply wants to write again then broken (duplicates)"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"cloud_sync_off_confirmed": False}, "cloud_sync_unconfirmed"),
        ({"process_check": lambda: ["rekordboxAgent"]}, "rekordbox_running"),
        ({"plan_sha": "not-the-plan"}, "plan_mismatch"),
    ],
)
def test_guard_refuses_with_nothing_written(library, kwargs, code):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    before = _bytes(lib)
    with pytest.raises(AnalyseApplyRefused) as refused:
        _apply(lib, plan, rb, backups, **kwargs)
    assert refused.value.code == code
    assert _bytes(lib) == before and not backups.exists(), f"if {code} writes or backs up anything then broken"


def test_gate_off_refuses(library, monkeypatch):
    lib, odj, backups = library
    monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, "0")
    plan, rb = _plan(lib, odj)
    with pytest.raises(RekordboxWritebackDisabled):
        _apply(lib, plan, rb, backups)


def test_stale_plan_refused_when_db_changed(library):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    with lib.master_db.open("ab") as handle:
        handle.write(b"\0")
    with pytest.raises(AnalyseApplyRefused) as refused:
        _apply(lib, plan, rb, backups)
    assert refused.value.code == "stale_plan", "if a write lands on a database that changed after the plan then broken"


def test_failure_after_backup_restores_byte_identical(library, monkeypatch):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    before = _bytes(lib)
    real_write = writer_mod._write

    def write_then_fail(db, plan_):
        real_write(db, plan_)
        raise RuntimeError("injected failure after commit")

    monkeypatch.setattr(writer_mod, "_write", write_then_fail)
    with pytest.raises(AnalyseApplyFailed) as failed:
        _apply(lib, plan, rb, backups)
    assert failed.value.restored
    assert _bytes(lib) == before, "if a failed write leaves master.db or the playlist XML changed then broken"


def test_verify_mismatch_restores(library, monkeypatch):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    before_sha = sha256_file(lib.master_db)
    monkeypatch.setattr(writer_mod, "integrity_ok", lambda _p: "*** page 3 corrupt")
    with pytest.raises(AnalyseApplyFailed, match="integrity_check"):
        _apply(lib, plan, rb, backups)
    assert sha256_file(lib.master_db) == before_sha, "if a failed integrity check keeps the write then broken"


def test_backups_are_pruned_to_keep(tmp_path):
    root = tmp_path / "b"
    for i in range(12):
        (root / f"2026110{i:02d}").mkdir(parents=True)
    pruned = writer_mod.prune_backups(root, keep=10)
    assert len(pruned) == 2 and len(list(root.iterdir())) == 10


def test_wal_is_folded_in_and_no_connection_survives(library):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    _apply(lib, plan, rb, backups)
    wal = lib.rb_dir / "master.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0, (
        "if the commit is left in the WAL file then broken (a master.db-only copy misses it)"
    )


def test_restore_survives_process_exit_in_wal_mode(library, monkeypatch):
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    before = _bytes(lib)
    monkeypatch.setattr(writer_mod, "integrity_ok", lambda _p: "*** forced failure")
    with pytest.raises(AnalyseApplyFailed):
        _apply(lib, plan, rb, backups)
    import gc

    gc.collect()
    assert _bytes(lib) == before, (
        "if a pooled connection rewrites master.db after the restore then broken (seen live Wed 4 Nov)"
    )


def test_probe_reports_windows_exe_and_mixed_case():
    """[if] the enumerator yields rekordbox.exe [then] the probe reports rekordbox, [else stop]."""

    def process_iter(_attrs=None):
        yield SimpleNamespace(info={"name": "rekordbox.exe"})
        yield SimpleNamespace(info={"name": "RekordboxAgent"})
        yield SimpleNamespace(info={"name": "chrome"})

    assert writer_mod.running_rekordbox_processes(process_iter=process_iter) == [
        "rekordbox",
        "rekordboxAgent",
    ]


def test_probe_empty_enumerator_reports_none():
    """[if] the enumerator is empty [then] the probe reports no Rekordbox, [else stop]."""
    assert writer_mod.running_rekordbox_processes(process_iter=lambda _attrs=None: iter(())) == []


def test_probe_enumerator_failure_is_closed():
    """[if] the enumerator cannot run [then] the probe raises RuntimeError, [else stop]."""

    def boom(_attrs=None):
        raise OSError("no process table")

    with pytest.raises(RuntimeError, match="cannot enumerate processes"):
        writer_mod.running_rekordbox_processes(process_iter=boom)


def test_probe_source_does_not_spawn_pgrep():
    """[if] the writer source is read [then] it does not mention pgrep, [else stop]."""
    source = Path(writer_mod.__file__).read_text(encoding="utf-8")
    assert "pgrep" not in source and "subprocess" not in source


def test_writeback_lock_refuses_and_writes_nothing(library):
    """[if] the writeback lock file exists [then] apply refuses and writes nothing, [else stop]."""
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    before = _bytes(lib)
    lock = lib.master_db.with_name(".master.db.writeback.lock")
    lock.write_text("held", encoding="utf-8")
    with pytest.raises(AnalyseApplyRefused) as refused:
        _apply(lib, plan, rb, backups)
    assert refused.value.code == "writeback_in_progress"
    assert _bytes(lib) == before and not backups.exists()


def test_successful_apply_releases_writeback_lock(library):
    """[if] apply succeeds [then] the writeback lock file is gone, [else stop]."""
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    _apply(lib, plan, rb, backups)
    lock = lib.master_db.with_name(".master.db.writeback.lock")
    assert not lock.exists(), "if a successful apply leaves the writeback lock then a later apply cannot start"


def test_write_failure_before_commit_restores_byte_identical(library, monkeypatch):
    """[if] _write fails before commit [then] master.db is restored byte for byte, [else stop]."""
    lib, odj, backups = library
    plan, rb = _plan(lib, odj)
    before = _bytes(lib)
    real_write = writer_mod._write

    def write_then_fail_before_commit(db, plan_):
        def boom() -> None:
            raise RuntimeError("injected failure before commit")

        db.commit = boom
        real_write(db, plan_)

    monkeypatch.setattr(writer_mod, "_write", write_then_fail_before_commit)
    with pytest.raises(AnalyseApplyFailed) as failed:
        _apply(lib, plan, rb, backups)
    assert failed.value.restored
    import gc

    gc.collect()
    assert _bytes(lib) == before, "if a pre-commit failure leaves master.db or the playlist XML changed then broken"
