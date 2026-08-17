"""Additional branch-coverage tests for :mod:`apps.reconcile.apply`.

Complements ``test_apply.py``. We focus on the error paths that the
primary test file stubs over: the live-DB helpers (``_apply_updates``,
``_verify``, ``_apply_updates_strict``), the confirm prompts
(``_confirm`` / ``_confirm_bulk_count``), ``_backup_db`` edge cases,
``_rekordbox_running``'s pgrep-missing path, and the full
``_run_live`` + ``_run_bulk`` success + failure paths.

All tests are fully hermetic — the \"live DB\" is replaced by an
in-memory stand-in that mimics the ``pyrekordbox`` surface we touch
(``db.get_content(ID=...)`` + per-row ``FolderPath`` attribute + a
``commit()`` + a ``close()``).

Requirement: RECON-04.
"""
from __future__ import annotations

import builtins
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.reconcile import apply


pytestmark = pytest.mark.requirement("RECON-04")


# --------------------------------------------------------------------------- #
# Fake live-DB surface
# --------------------------------------------------------------------------- #


@dataclass
class _FakeRow:
    FolderPath: str


@dataclass
class _FakeDB:
    """Minimal drop-in for ``pyrekordbox.Rekordbox6Database`` access.

    Only the attrs/methods touched by apply.py are implemented.
    """
    rows: dict[str, _FakeRow] = field(default_factory=dict)
    commits: int = 0
    closed: bool = False
    raise_on_lookup: bool = False
    raise_on_assign: bool = False
    session: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(rollback=lambda: None)
    )

    def get_content(self, ID):
        if self.raise_on_lookup:
            raise RuntimeError("boom-lookup")
        return self.rows.get(str(ID))

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def _mk_update(
    *,
    id: str = "1",
    new_path: str = "/tmp/new.mp3",
    old_path: str = "/tmp/old.mp3",
    triple: bool = True,
) -> apply.Update:
    return apply.Update(
        id=id, title=f"t-{id}", artist="a",
        old_path=old_path, new_path=new_path,
        confidence=1.0, rationale="", triple_validated=triple,
    )


# --------------------------------------------------------------------------- #
# _rekordbox_running fallback paths
# --------------------------------------------------------------------------- #


def test_rekordbox_running_handles_missing_pgrep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If ``pgrep`` isn't installed, the helper logs + returns False."""
    def _raise(*a, **kw):
        raise FileNotFoundError("pgrep not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert apply._rekordbox_running() is False
    assert "pgrep not available" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# _confirm prompt
# --------------------------------------------------------------------------- #


def test_confirm_accepts_magic_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "yes I understand  ")
    assert apply._confirm() is True


def test_confirm_rejects_other_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "nope")
    assert apply._confirm() is False


def test_confirm_treats_eof_as_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_eof(_prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _raise_eof)
    assert apply._confirm() is False


def test_confirm_bulk_count_matches_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "APPLY 5 UPDATES")
    assert apply._confirm_bulk_count(5) is True


def test_confirm_bulk_count_rejects_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "apply 4 updates")
    assert apply._confirm_bulk_count(5) is False


def test_confirm_bulk_count_eof_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _raise)
    assert apply._confirm_bulk_count(3) is False


# --------------------------------------------------------------------------- #
# _backup_db
# --------------------------------------------------------------------------- #


def test_backup_db_copies_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "master.db"
    live.write_bytes(b"fake-db-contents")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    out_dir = tmp_path / "backups"
    backup = apply._backup_db(out_dir)
    assert backup.is_file()
    assert backup.read_bytes() == b"fake-db-contents"
    assert backup.parent == out_dir


def test_backup_db_raises_when_copy_produces_empty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "master.db"
    live.write_bytes(b"real")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    def _copy_empty(src, dst, *a, **kw):
        Path(dst).write_bytes(b"")
        return dst
    monkeypatch.setattr(apply.shutil, "copy2", _copy_empty)

    with pytest.raises(RuntimeError, match="size 0"):
        apply._backup_db(tmp_path / "b")


# --------------------------------------------------------------------------- #
# _apply_updates
# --------------------------------------------------------------------------- #


def test_apply_updates_writes_new_folder_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/old.mp3")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    u = _mk_update(id="1", new_path="/new.mp3")
    errors = apply._apply_updates([u])
    assert errors == []
    assert db.rows["1"].FolderPath == "/new.mp3"
    assert db.commits == 1
    assert db.closed is True


def test_apply_updates_records_missing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/x")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    errors = apply._apply_updates([_mk_update(id="1"), _mk_update(id="2")])
    assert len(errors) == 1
    assert errors[0][0].id == "2"
    assert "not found" in errors[0][1]


def test_apply_updates_records_lookup_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(raise_on_lookup=True)
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    errors = apply._apply_updates([_mk_update(id="1")])
    assert len(errors) == 1
    assert "lookup failed" in errors[0][1]


# --------------------------------------------------------------------------- #
# _verify
# --------------------------------------------------------------------------- #


def test_verify_returns_empty_when_everything_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "n.mp3"
    real.write_bytes(b"x")
    db = _FakeDB(rows={"1": _FakeRow(FolderPath=str(real))})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    u = _mk_update(id="1", new_path=str(real))
    assert apply._verify([u]) == []


def test_verify_flags_missing_row_after_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(rows={})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    failures = apply._verify([_mk_update(id="1")])
    assert len(failures) == 1
    assert "missing after write" in failures[0][1]


def test_verify_flags_folder_path_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/wrong.mp3")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    u = _mk_update(id="1", new_path="/right.mp3")
    failures = apply._verify([u])
    assert len(failures) == 1
    assert "FolderPath mismatch" in failures[0][1]


def test_verify_flags_missing_file_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = tmp_path / "ghost.mp3"  # never created
    db = _FakeDB(rows={"1": _FakeRow(FolderPath=str(ghost))})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    u = _mk_update(id="1", new_path=str(ghost))
    failures = apply._verify([u])
    assert len(failures) == 1
    assert "does not exist on disk" in failures[0][1]


# --------------------------------------------------------------------------- #
# _apply_updates_strict (bulk)
# --------------------------------------------------------------------------- #


def test_apply_updates_strict_success_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {str(i): _FakeRow(FolderPath="/old") for i in range(1, 4)}
    db = _FakeDB(rows=rows)
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    ups = [_mk_update(id=str(i), new_path=f"/new-{i}") for i in range(1, 4)]
    errors = apply._apply_updates_strict(ups)
    assert errors == []
    assert db.commits == 1
    for i in range(1, 4):
        assert db.rows[str(i)].FolderPath == f"/new-{i}"


def test_apply_updates_strict_aborts_on_missing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/old")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    ups = [_mk_update(id="1", new_path="/n1"), _mk_update(id="2", new_path="/n2")]
    errors = apply._apply_updates_strict(ups)
    assert len(errors) == 1
    assert errors[0][0].id == "2"
    assert db.commits == 0  # no commit on any failure


def test_apply_updates_strict_aborts_on_lookup_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(raise_on_lookup=True)
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    errors = apply._apply_updates_strict([_mk_update(id="1")])
    assert len(errors) == 1
    assert "lookup failed" in errors[0][1]
    assert db.commits == 0


def test_apply_updates_strict_aborts_on_assign_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingRow:
        @property
        def FolderPath(self) -> str:
            return "/old"
        @FolderPath.setter
        def FolderPath(self, v: str) -> None:
            raise RuntimeError("assign-bang")

    db = _FakeDB(rows={"1": _ExplodingRow()})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    errors = apply._apply_updates_strict([_mk_update(id="1")])
    assert len(errors) == 1
    assert "assign failed" in errors[0][1]


# --------------------------------------------------------------------------- #
# _run_live full success + verify-failure branches
# --------------------------------------------------------------------------- #


def test_run_live_happy_path_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full _run_live success path with a fake live DB + on-disk new path."""
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    new_file = tmp_path / "new.mp3"
    new_file.write_bytes(b"audio")

    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/old")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)

    u = _mk_update(id="1", new_path=str(new_file))
    rc = apply._run_live([u], tmp_path / "bk")
    assert rc == 0
    assert db.rows["1"].FolderPath == str(new_file)
    out = capsys.readouterr().out
    assert "Applied updates" in out
    assert "Reversal script" in out


def test_run_live_apply_error_returns_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error during apply → exit 5 and NO verify/reversal."""
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)
    # ID not in fake DB → apply records an error.
    db = _FakeDB(rows={})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)

    u = _mk_update(id="404", new_path="/whatever.mp3")
    rc = apply._run_live([u], tmp_path / "bk")
    assert rc == 5


def test_run_live_verify_failure_returns_six(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply succeeds but verify fails (file doesn't exist) → exit 6."""
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)

    ghost = tmp_path / "ghost.mp3"  # never created
    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/old")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)

    u = _mk_update(id="1", new_path=str(ghost))
    rc = apply._run_live([u], tmp_path / "bk")
    assert rc == 6


# --------------------------------------------------------------------------- #
# _run_bulk full success + verify-failure + count-confirm branches
# --------------------------------------------------------------------------- #


def test_run_bulk_count_confirmation_denied_returns_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    new_file = tmp_path / "new.mp3"
    new_file.write_bytes(b"x")
    u = _mk_update(id="1", new_path=str(new_file))

    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: {"1"})
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)
    monkeypatch.setattr(apply, "_confirm_bulk_count", lambda n: False)

    rc = apply._run_bulk([u], tmp_path / "b.csv", tmp_path / "bk")
    assert rc == 4


def test_run_bulk_race_check_drift_returns_eight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-commit re-selection count drifts vs. prompt → exit 8."""
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    new_file = tmp_path / "n.mp3"
    new_file.write_bytes(b"x")
    u = _mk_update(id="1", new_path=str(new_file))

    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)
    monkeypatch.setattr(apply, "_confirm_bulk_count", lambda n: True)

    # First call returns the id, second call (race re-check) returns empty.
    state = {"calls": 0}
    def _broken(_p):
        state["calls"] += 1
        return {"1"} if state["calls"] == 1 else set()
    monkeypatch.setattr(apply, "_load_broken_ids", _broken)

    rc = apply._run_bulk([u], tmp_path / "b.csv", tmp_path / "bk")
    assert rc == 8


def test_run_bulk_happy_path_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    new_file = tmp_path / "n.mp3"
    new_file.write_bytes(b"x")
    u = _mk_update(id="1", new_path=str(new_file))

    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)
    monkeypatch.setattr(apply, "_confirm_bulk_count", lambda n: True)
    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: {"1"})

    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/old")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)

    rc = apply._run_bulk([u], tmp_path / "b.csv", tmp_path / "bk")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Reversal script" in out


def test_run_bulk_apply_errors_returns_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An apply-strict error aborts the batch with exit 5."""
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    new_file = tmp_path / "n.mp3"
    new_file.write_bytes(b"x")
    u = _mk_update(id="nope", new_path=str(new_file))

    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)
    monkeypatch.setattr(apply, "_confirm_bulk_count", lambda n: True)
    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: {"nope"})
    db = _FakeDB(rows={})  # ID not found → error
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)

    rc = apply._run_bulk([u], tmp_path / "b.csv", tmp_path / "bk")
    assert rc == 5


def test_run_bulk_verify_failure_returns_six(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bulk apply succeeds but a row fails verify → exit 6."""
    live = tmp_path / "master.db"
    live.write_bytes(b"db")
    monkeypatch.setattr(apply.paths, "REKORDBOX_LIVE_DB", live)

    # File exists at pre-flight but _verify checks mismatch via fake DB.
    new_file = tmp_path / "n.mp3"
    new_file.write_bytes(b"x")

    u = _mk_update(id="1", new_path=str(new_file))
    monkeypatch.setattr(apply, "_rekordbox_running", lambda: False)
    monkeypatch.setattr(apply, "_confirm", lambda: True)
    monkeypatch.setattr(apply, "_confirm_bulk_count", lambda n: True)
    monkeypatch.setattr(apply, "_load_broken_ids", lambda p: {"1"})

    # Apply succeeds; then we swap _verify to return a synthetic failure
    # to exercise the bulk verify-failure branch.
    db = _FakeDB(rows={"1": _FakeRow(FolderPath="/old")})
    monkeypatch.setattr(apply, "_open_live_db", lambda: db)
    monkeypatch.setattr(apply, "_verify", lambda ups: [(ups[0], "forced fail")])

    rc = apply._run_bulk([u], tmp_path / "b.csv", tmp_path / "bk")
    assert rc == 6
