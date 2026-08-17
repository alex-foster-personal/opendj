"""H1: no live library write happens behind a failing integrity check.

`apps/audit/library_integrity.py` was commissioned so "future syncs cannot
silently ship broken track locations", was unit-tested in isolation, and then
had ZERO production callers for two months. These tests guard the wiring, not
the arithmetic: a refactor that keeps `check_integrity` perfect but stops
calling it on the write path is exactly the failure this file catches.

Reports here are built by the real `check_integrity` over real files in a tmp
dir, never hand-assembled, so a change to how brokenness is counted moves these
tests too.

Acceptance criteria (UNCAPTURED-REQUIREMENTS.md H1):
  [if] an apply run starts while 75% of affected RB tracks point at files that
       do not exist [then] it aborts before taking a backup and names the
       broken ratio ⛔️ it backs up and writes anyway
  [if] the same run is re-issued with the explicit override flag [then] it
       proceeds and the override is recorded in the reversal log ⛔️ no
       override exists
  [if] every affected track resolves [then] the gate is a silent no-op ⛔️
       healthy libraries get an extra prompt

-Claude
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.audit import library_integrity
from apps.sync import playlist_apply as pa


@dataclass(slots=True)
class _Track:
    """Structural stand-in for `rekordbox_db.RBTrack` (folder_path + flag)."""

    folder_path: str
    is_streaming: bool = False


def _report(tmp_path: Path, *, present: int, broken: int) -> library_integrity.IntegrityReport:
    """A real integrity report over real files on disk."""
    tracks: list[_Track] = []
    for i in range(present):
        f = tmp_path / f"present-{i}.aiff"
        f.write_bytes(b"\0")
        tracks.append(_Track(str(f)))
    for i in range(broken):
        tracks.append(_Track(str(tmp_path / "gone" / f"missing-{i}.aiff")))
    return library_integrity.check_integrity(tracks)


def _plan_file(tmp_path: Path) -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"generated_at":"","match_set_sha256":"","playlists":[],"djay_only":[]}',
        encoding="utf-8",
    )
    return plan


def _nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Result()  # noqa: ARG005
    )


# ----- unit: the gate itself --------------------------------------------


@pytest.mark.requirement("SYNC-03")
def test_gate_refuses_and_names_the_broken_ratio(tmp_path: Path) -> None:
    rep = _report(tmp_path, present=1, broken=3)  # 75% broken
    assert rep.broken_ratio == pytest.approx(0.75)
    with pytest.raises(pa.PlaylistApplyError) as exc:
        pa._integrity_gate(rep, allow_broken=False)
    assert "75.0%" in str(exc.value), (
        "the refusal must name the broken ratio, not just say 'unhealthy'"
    )


@pytest.mark.requirement("SYNC-03")
def test_gate_is_a_silent_no_op_on_a_healthy_library(tmp_path: Path) -> None:
    rep = _report(tmp_path, present=40, broken=0)
    assert pa._integrity_gate(rep, allow_broken=False) is None


@pytest.mark.requirement("SYNC-03")
def test_gate_returns_an_override_note_when_allow_broken(tmp_path: Path) -> None:
    rep = _report(tmp_path, present=1, broken=3)
    note = pa._integrity_gate(rep, allow_broken=True)
    assert note is not None and "75.0%" in note


# ----- wiring: main() must actually call it -----------------------------


@pytest.mark.requirement("SYNC-03")
def test_live_apply_aborts_before_taking_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must land ahead of the backup, not after it."""
    _nothing_running(monkeypatch)
    monkeypatch.setattr(
        pa, "_live_integrity_report", lambda: _report(tmp_path, present=1, broken=3)
    )

    def _no_backup(*args: object, **kwargs: object) -> tuple[Path, str]:
        raise AssertionError("backup taken despite a failing integrity gate")

    monkeypatch.setattr(pa, "_backup_djay_db", _no_backup)
    monkeypatch.setattr(pa, "apply_plan", _no_backup)
    monkeypatch.setattr("builtins.input", lambda: pa.CONFIRMATION_PHRASE)

    rc = pa.main(
        [
            "--plan", str(_plan_file(tmp_path)),
            "--backup-dir", str(tmp_path / "backups"),
            "--live", "--i-understand-the-risks",
            "--skip-tsaf-validation",
        ]
    )
    assert rc == 7, f"expected the integrity refusal exit code, got {rc}"


@pytest.mark.requirement("SYNC-03")
def test_override_flag_proceeds_and_is_recorded_in_the_reversal_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nothing_running(monkeypatch)
    monkeypatch.setattr(
        pa, "_live_integrity_report", lambda: _report(tmp_path, present=1, broken=3)
    )
    db = tmp_path / "djay.db"
    db.write_bytes(b"djay")
    monkeypatch.setattr(pa, "_live_db_path", lambda live: db)  # noqa: ARG005
    monkeypatch.setattr(
        pa, "apply_plan", lambda *a, **k: pa.ApplyResult()  # noqa: ARG005
    )
    monkeypatch.setattr("builtins.input", lambda: pa.CONFIRMATION_PHRASE)

    backup_dir = tmp_path / "backups"
    rc = pa.main(
        [
            "--plan", str(_plan_file(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--live", "--i-understand-the-risks", "--allow-broken",
            "--skip-tsaf-validation",
        ]
    )
    assert rc == 0, f"--allow-broken must let the run proceed, got {rc}"
    reversals = sorted(backup_dir.glob("restore-playlist-sync-*.py"))
    assert reversals, "no reversal script written"
    text = reversals[-1].read_text(encoding="utf-8")
    assert "--allow-broken override" in text, (
        "the reversal log must record that the gate was overridden"
    )
    assert "75.0%" in text


@pytest.mark.requirement("SYNC-03")
def test_healthy_library_reaches_the_write_path_with_no_override_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nothing_running(monkeypatch)
    monkeypatch.setattr(
        pa, "_live_integrity_report", lambda: _report(tmp_path, present=40, broken=0)
    )
    db = tmp_path / "djay.db"
    db.write_bytes(b"djay")
    monkeypatch.setattr(pa, "_live_db_path", lambda live: db)  # noqa: ARG005
    monkeypatch.setattr(
        pa, "apply_plan", lambda *a, **k: pa.ApplyResult()  # noqa: ARG005
    )
    monkeypatch.setattr("builtins.input", lambda: pa.CONFIRMATION_PHRASE)

    backup_dir = tmp_path / "backups"
    rc = pa.main(
        [
            "--plan", str(_plan_file(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--live", "--i-understand-the-risks",
            "--skip-tsaf-validation",
        ]
    )
    assert rc == 0
    text = sorted(backup_dir.glob("restore-playlist-sync-*.py"))[-1].read_text(
        encoding="utf-8"
    )
    assert "override" not in text.lower() or "no override was used" in text
