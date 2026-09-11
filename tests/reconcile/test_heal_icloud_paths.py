"""Plan uniqueness rules for iCloud path heal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apps.reconcile import heal_icloud_paths as heal
from apps.reconcile import locate
from apps.shared import rekordbox_db


def _track(**kwargs: object) -> rekordbox_db.RBTrack:
    defaults = dict(
        id="1",
        title="Hypnosis",
        artist="PALAZZO",
        album="",
        genre="",
        folder_path="/Users/user/Library/Mobile Documents/com~apple~CloudDocs/x/01 Hypnosis.mp3",
        file_path=None,
        is_streaming=False,
        bpm=None,
        rating=None,
        file_size=5_448_878,
        date_added=None,
        isrc=None,
        duration_s=166.0,
    )
    defaults.update(kwargs)
    return rekordbox_db.RBTrack(**defaults)  # type: ignore[arg-type]


def test_classify_ready_unique_one_basename_plus_size() -> None:
    cand = locate.Candidate(
        path=Path("/Users/user/Music/Unravelling/01 Hypnosis.mp3"),
        confidence=0.55,
        signals=["basename_exact", "size_match"],
    )
    row = heal._classify(_track(), [cand])
    assert row.status == "ready_unique"
    assert row.candidate_path.endswith("01 Hypnosis.mp3")


def test_classify_needs_confirm_when_two_unique_bar() -> None:
    cands = [
        locate.Candidate(
            path=Path("/Users/user/Music/a/01 Hypnosis.mp3"),
            confidence=0.55,
            signals=["basename_exact", "size_match"],
        ),
        locate.Candidate(
            path=Path("/Users/user/Music/b/01 Hypnosis.mp3"),
            confidence=0.55,
            signals=["basename_exact", "duration_match"],
        ),
    ]
    row = heal._classify(_track(), cands)
    assert row.status == "needs_confirm"
    assert "2 unique-bar" in row.rationale


def test_classify_needs_confirm_basename_only() -> None:
    cand = locate.Candidate(
        path=Path("/Users/user/Music/01 Hypnosis.mp3"),
        confidence=0.35,
        signals=["basename_exact"],
    )
    row = heal._classify(_track(), [cand])
    assert row.status == "needs_confirm"


def test_classify_no_twin() -> None:
    row = heal._classify(_track(), [])
    assert row.status == "no_twin"


@pytest.mark.skipif(sys.platform != "darwin", reason="zone helper Darwin-scoped")
def test_build_plan_finds_music_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.shared import audio_files, icloud_zone, paths, platform_paths

    home = tmp_path
    music_root = home / "Music"
    music = music_root / "Unravelling"
    music.mkdir(parents=True)
    twin = music / "01 Hypnosis.mp3"
    twin.write_bytes(b"x" * 4096)

    zone = (
        home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        / "Convert" / "01 Hypnosis.mp3"
    )
    original = str(zone)
    roots = [music_root]

    monkeypatch.setattr(platform_paths, "HOME", home)
    monkeypatch.setattr(platform_paths, "MUSIC_ROOTS", roots)
    monkeypatch.setattr(paths, "MUSIC_ROOTS", roots)
    monkeypatch.setattr(audio_files.paths, "MUSIC_ROOTS", roots)
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", home)
    monkeypatch.setattr(heal.paths, "MUSIC_ROOTS", roots)

    track = _track(
        folder_path=original,
        file_size=4096,
        duration_s=None,
    )
    rows = heal.build_plan([track])
    assert len(rows) == 1
    assert rows[0].status == "ready_unique"
    assert Path(rows[0].candidate_path) == twin


# --- H17: safety rails on the --apply-unique entry point --------------------
#
# apply_unique is the only live-write door in this module. The shared rails it
# leans on (_rekordbox_running, _confirm, _backup_db, _apply_updates, _verify)
# are tested in isolation in tests/reconcile/test_apply.py, but nothing asserted
# that this entry point still CALLS them, or in what order. A refactor that
# dropped the backup or moved it after the first write would have gone unnoticed.
#
# The seams substituted below are environment facts, not the unit under test:
# whether a Rekordbox process exists, whether a human typed the phrase, and the
# live master.db itself, which a test must never open or copy. apply_unique's
# own branching, ordering and exit codes are the real code under assertion.


def _heal_row(candidate: Path, **kwargs: object) -> heal.HealRow:
    defaults = dict(
        id="1",
        title="Hypnosis",
        artist="PALAZZO",
        original_path=(
            "/Users/user/Library/Mobile Documents/com~apple~CloudDocs/x/01 Hypnosis.mp3"
        ),
        zone_reason="icloud_drive",
        status="ready_unique",
        candidate_path=str(candidate),
        confidence=0.55,
        rationale="basename_exact+size_match",
    )
    defaults.update(kwargs)
    return heal.HealRow(**defaults)  # type: ignore[arg-type]


class _Rails:
    """Records which live-DB rails ran, in order, without touching master.db."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.write_errors: list[tuple[object, str]] = []
        self.verify_failures: list[tuple[object, str]] = []

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backup: Path,
        *,
        rb_running: bool = False,
        confirmed: bool = True,
    ) -> None:
        def _backup(_dir: Path) -> Path:
            self.calls.append("backup")
            return backup

        def _apply(updates: list[object]) -> list[tuple[object, str]]:
            self.calls.append("apply")
            return self.write_errors

        def _verify(updates: list[object]) -> list[tuple[object, str]]:
            self.calls.append("verify")
            return self.verify_failures

        def _running() -> bool:
            self.calls.append("rb-check")
            return rb_running

        def _confirm() -> bool:
            self.calls.append("confirm")
            return confirmed

        monkeypatch.setattr(heal.reconcile_apply, "_rekordbox_running", _running)
        monkeypatch.setattr(heal.reconcile_apply, "_confirm", _confirm)
        monkeypatch.setattr(heal.reconcile_apply, "_backup_db", _backup)
        monkeypatch.setattr(heal.reconcile_apply, "_apply_updates", _apply)
        monkeypatch.setattr(heal.reconcile_apply, "_verify", _verify)


@pytest.fixture()
def _twin(tmp_path: Path) -> Path:
    """A real, materialised, non-zone audio file for apply_unique to accept."""
    music = tmp_path / "Music" / "Unravelling"
    music.mkdir(parents=True)
    twin = music / "01 Hypnosis.mp3"
    twin.write_bytes(b"x" * 4096)
    return twin


def _recording_console(monkeypatch: pytest.MonkeyPatch) -> "io.StringIO":
    import io as _io

    from rich.console import Console

    buf = _io.StringIO()
    monkeypatch.setattr(heal, "console", Console(file=buf, width=400))
    monkeypatch.setattr(heal.reconcile_apply, "console", Console(file=buf, width=400))
    return buf


def test_apply_unique_refuses_without_the_risk_flag_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] --apply-unique runs without --i-understand-the-risks
    [then] exit non-zero and no rail that touches master.db runs."""
    rails = _Rails()
    rails.install(monkeypatch, tmp_path / "master.bak.db")
    buf = _recording_console(monkeypatch)

    code = heal.apply_unique([_heal_row(_twin)], i_understand=False)

    assert code == 2, "if apply runs without the risk flag then a footgun is armed"
    assert rails.calls == [], (
        "if any live-DB rail runs on the refusal path then the refusal is cosmetic"
    )
    assert "Refusing apply" in buf.getvalue()


def test_apply_unique_aborts_before_backup_when_rekordbox_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] a Rekordbox process is running
    [then] abort before opening the DB for write."""
    rails = _Rails()
    rails.install(monkeypatch, tmp_path / "master.bak.db", rb_running=True)
    _recording_console(monkeypatch)

    code = heal.apply_unique([_heal_row(_twin)], i_understand=True)

    assert code == 2, "if a live write races Rekordbox then the DB can corrupt"
    assert "apply" not in rails.calls, "if we write while RB is up then we race it"
    assert "backup" not in rails.calls
    assert rails.calls == ["rb-check"]


def test_apply_unique_aborts_when_confirmation_is_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] the human does not type the confirmation phrase
    [then] nothing is backed up and nothing is written."""
    rails = _Rails()
    rails.install(monkeypatch, tmp_path / "master.bak.db", confirmed=False)
    _recording_console(monkeypatch)

    code = heal.apply_unique([_heal_row(_twin)], i_understand=True)

    assert code == 2
    assert rails.calls == ["rb-check", "confirm"], (
        "if a denied confirmation still backs up or writes then consent is theatre"
    )


def test_apply_unique_backs_up_master_db_before_the_first_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] the apply proceeds
    [then] the backup is taken strictly before the first UPDATE."""
    rails = _Rails()
    rails.install(monkeypatch, tmp_path / "master.bak.db")
    _recording_console(monkeypatch)

    code = heal.apply_unique([_heal_row(_twin)], i_understand=True)

    assert code == 0
    assert rails.calls == ["rb-check", "confirm", "backup", "apply", "verify"], (
        "if backup does not precede apply then a failed write has no way back"
    )


def test_apply_unique_names_the_backup_path_on_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] a write errors [then] exit non-zero naming the backup to restore."""
    backup = tmp_path / "master.20260816T101500.db"
    rails = _Rails()
    rails.write_errors = [(_heal_row(_twin), "ID not found in live DB")]
    rails.install(monkeypatch, backup)
    buf = _recording_console(monkeypatch)

    code = heal.apply_unique([_heal_row(_twin)], i_understand=True)

    assert code == 1
    assert "verify" not in rails.calls, "if we verify after a failed write then we mask it"
    assert backup.name in buf.getvalue(), (
        "if the backup path is not named then the operator cannot restore"
    )


def test_apply_unique_names_the_backup_path_on_verify_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] a row does not read back [then] exit non-zero naming the backup."""
    backup = tmp_path / "master.20260816T101500.db"
    rails = _Rails()
    rails.verify_failures = [(_heal_row(_twin), "FolderPath mismatch after write")]
    rails.install(monkeypatch, backup)
    buf = _recording_console(monkeypatch)

    code = heal.apply_unique([_heal_row(_twin)], i_understand=True)

    assert code == 1, "if an unverified write reports success then silent drift ships"
    assert backup.name in buf.getvalue()


def test_apply_unique_refuses_a_dataless_candidate_before_any_rail_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the chosen twin is an iCloud stub rather than real bytes
    [then] refuse before the RB check, so we never heal onto a placeholder."""
    rails = _Rails()
    rails.install(monkeypatch, tmp_path / "master.bak.db")
    _recording_console(monkeypatch)
    ghost = tmp_path / "Music" / "01 Hypnosis.mp3"
    ghost.parent.mkdir(parents=True)
    ghost.write_bytes(b"x" * 4096)
    monkeypatch.setattr(heal.fs_residency, "is_materialised", lambda p: False)

    code = heal.apply_unique([_heal_row(ghost)], i_understand=True)

    assert code == 2, "if a dataless stub is healed to then the path still will not play"
    assert rails.calls == [], "if any rail ran then the candidate check came too late"


def test_apply_unique_returns_zero_and_touches_nothing_when_no_rows_are_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _twin: Path
) -> None:
    """[if] no row is ready_unique [then] succeed without arming any rail."""
    rails = _Rails()
    rails.install(monkeypatch, tmp_path / "master.bak.db")
    _recording_console(monkeypatch)

    code = heal.apply_unique(
        [_heal_row(_twin, status="needs_confirm")], i_understand=True
    )

    assert code == 0
    assert rails.calls == [], "if a needs_confirm row reaches a write then review is bypassed"
