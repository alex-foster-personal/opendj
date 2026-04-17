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

    def _run(cmd, check=False, capture_output=True, text=True):  # noqa: ANN001
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


@pytest.mark.requirement("SYNC-03")
def test_quit_check_warns_when_pgrep_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*args, **kwargs):
        raise FileNotFoundError("no pgrep")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert pa._process_running("anything") is False
    assert "pgrep not available" in capsys.readouterr().err


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
