"""H17: every safety rail on the iCloud-to-Music heal apply path stays wired.

`tests/reconcile/test_heal_icloud_paths.py` covers `_classify` and
`build_plan`; `grep apply_unique tests/` returned nothing. The shared
primitives in `apps/reconcile/apply.py` are tested in isolation, so a refactor
that simply STOPS CALLING one of them from `apply_unique` would go unnoticed --
and the thing it stops calling is the backup, or the running-Rekordbox check,
or the readback verify.

These tests record the exact order in which the rails fire, so both "the rail
disappeared" and "the rail moved after the write" are caught. Candidate paths
are real materialised files in tmp_path, never synthesised strings, because
`apply_unique` refuses a non-materialised candidate on purpose.

Acceptance criteria (UNCAPTURED-REQUIREMENTS.md H17):
  [if] --apply-unique runs without --i-understand-the-risks [then] exit
       non-zero and write nothing ⛔️ FolderPath rows change
  [if] a Rekordbox process is running [then] abort before opening the DB for
       write ⛔️ a live write races the app
  [if] a verify readback mismatches after write [then] the failure message
       names the backup file under data/reconcile/backups/ ⛔️ a silent partial
       rewrite with no recovery pointer

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.reconcile import apply as reconcile_apply
from apps.reconcile import heal_icloud_paths as heal


@pytest.fixture
def rows(tmp_path: Path) -> list[heal.HealRow]:
    """Two ready_unique rows whose candidates are real files on disk."""
    music = tmp_path / "Music" / "Library"
    music.mkdir(parents=True)
    out: list[heal.HealRow] = []
    for i in range(2):
        twin = music / f"track-{i}.aiff"
        twin.write_bytes(b"FORM" + b"\0" * 64)
        out.append(
            heal.HealRow(
                id=f"rb-{i}",
                title=f"Track {i}",
                artist="Artist",
                original_path=f"/Users/someone/Library/Mobile Documents/track-{i}.aiff",
                zone_reason="mobile_documents",
                status="ready_unique",
                candidate_path=str(twin),
                confidence=0.97,
                signals=["basename_exact", "size_match"],
                rationale="basename + size",
            )
        )
    return out


@pytest.fixture
def rails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Record every rail `apply_unique` fires, in order. Healthy by default."""
    calls: list[str] = []
    backup = tmp_path / "backups" / "master.20260817T090000.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(b"master")

    def _running() -> bool:
        calls.append("rekordbox_running")
        return False

    def _confirm() -> bool:
        calls.append("confirm")
        return True

    def _backup(backup_dir: Path) -> Path:  # noqa: ARG001
        calls.append("backup")
        return backup

    def _apply(updates: list[reconcile_apply.Update]) -> list[tuple[object, str]]:
        calls.append(f"write:{len(updates)}")
        return []

    def _verify(updates: list[reconcile_apply.Update]) -> list[tuple[object, str]]:
        calls.append(f"verify:{len(updates)}")
        return []

    monkeypatch.setattr(reconcile_apply, "_rekordbox_running", _running)
    monkeypatch.setattr(reconcile_apply, "_confirm", _confirm)
    monkeypatch.setattr(reconcile_apply, "_backup_db", _backup)
    monkeypatch.setattr(reconcile_apply, "_apply_updates", _apply)
    monkeypatch.setattr(reconcile_apply, "_verify", _verify)
    return calls


# ----- rail: the explicit risk flag --------------------------------------


@pytest.mark.requirement("GUARD-02")
def test_refuses_without_the_risk_flag_and_writes_nothing(
    rows: list[heal.HealRow], rails: list[str]
) -> None:
    rc = heal.apply_unique(rows, i_understand=False)
    assert rc != 0, "a missing --i-understand-the-risks returned success"
    assert rails == [], f"rails fired without the risk flag: {rails}"


# ----- rail: Rekordbox must be quit BEFORE the DB is opened --------------


@pytest.mark.requirement("GUARD-02")
def test_running_rekordbox_aborts_before_backup_or_write(
    rows: list[heal.HealRow], rails: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _running() -> bool:
        rails.append("rekordbox_running")
        return True

    monkeypatch.setattr(reconcile_apply, "_rekordbox_running", _running)
    rc = heal.apply_unique(rows, i_understand=True)
    assert rc != 0
    assert rails == ["rekordbox_running"], (
        f"a live write raced Rekordbox: {rails}"
    )


@pytest.mark.requirement("GUARD-02")
def test_failed_confirmation_aborts_before_backup_or_write(
    rows: list[heal.HealRow], rails: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reconcile_apply, "_confirm", lambda: rails.append("confirm") or False)
    rc = heal.apply_unique(rows, i_understand=True)
    assert rc != 0
    assert "backup" not in rails and not any(c.startswith("write") for c in rails)


# ----- rail: backup precedes the first write, verify follows it ----------


@pytest.mark.requirement("GUARD-02")
def test_happy_path_fires_every_rail_in_order(
    rows: list[heal.HealRow], rails: list[str]
) -> None:
    rc = heal.apply_unique(rows, i_understand=True)
    assert rc == 0
    assert rails == [
        "rekordbox_running",
        "confirm",
        "backup",
        "write:2",
        "verify:2",
    ], f"a rail was dropped or reordered: {rails}"


# ----- rail: a failure names the recovery pointer ------------------------


@pytest.mark.requirement("GUARD-02")
def test_verify_failure_names_the_backup_file(
    rows: list[heal.HealRow],
    rails: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        reconcile_apply,
        "_verify",
        lambda updates: [(u, "FolderPath read back unchanged") for u in updates],
    )
    rc = heal.apply_unique(rows, i_understand=True)
    assert rc != 0, "a verify mismatch reported success"
    out = capsys.readouterr().out
    assert "master.20260817T090000.db" in out.replace("\n", ""), (
        f"the failure did not name the backup file: {out}"
    )
    assert "verify failure" in out


@pytest.mark.requirement("GUARD-02")
def test_write_error_names_the_backup_file(
    rows: list[heal.HealRow],
    rails: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        reconcile_apply,
        "_apply_updates",
        lambda updates: [(u, "sqlite locked") for u in updates],
    )
    rc = heal.apply_unique(rows, i_understand=True)
    assert rc != 0
    out = capsys.readouterr().out
    assert "master.20260817T090000.db" in out.replace("\n", "")


@pytest.mark.requirement("GUARD-02")
def test_the_backup_dir_is_the_documented_reconcile_backups_dir() -> None:
    """The failure message is only a recovery pointer if the path is findable."""
    assert reconcile_apply.DEFAULT_BACKUP_DIR.parts[-2:] == ("reconcile", "backups")


# ----- rail: candidates must be real, materialised, out-of-zone ----------


@pytest.mark.requirement("GUARD-02")
def test_a_non_materialised_candidate_is_refused_before_any_write(
    rows: list[heal.HealRow], rails: list[str], tmp_path: Path
) -> None:
    rows[1].candidate_path = str(tmp_path / "Music" / "Library" / "not-here.aiff")
    rc = heal.apply_unique(rows, i_understand=True)
    assert rc != 0
    assert rails == [], f"a write was attempted with a dataless candidate: {rails}"


@pytest.mark.requirement("GUARD-02")
def test_rows_that_are_not_ready_unique_are_never_written(
    rows: list[heal.HealRow], rails: list[str]
) -> None:
    for r in rows:
        r.status = "needs_confirm"
    assert heal.apply_unique(rows, i_understand=True) == 0
    assert rails == [], f"an unconfirmed row reached the write path: {rails}"
