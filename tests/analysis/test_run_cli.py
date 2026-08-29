"""``apps.analysis.run`` CLI tests (META-01)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.analysis import run as run_mod
from apps.analysis.record import AnalysisRecord


class _MockBackend:
    name = "mock"
    version = "mock-1.0"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=cls.version,
            analyzed_at=datetime.now(timezone.utc),
            duration_s=42.0, sample_rate=44100,
            bpm=123.4, bpm_confidence=0.9,
            key_camelot="8A", key_openkey="8m", key_confidence=0.9,
            energy=7,
        )


@pytest.fixture()
def mock_backend(monkeypatch: pytest.MonkeyPatch) -> type[_MockBackend]:
    from apps.analysis import backends
    monkeypatch.setitem(backends.BACKENDS, "mock", _MockBackend)
    return _MockBackend


@pytest.mark.requirement("META-01")
def test_stable_id_from_path_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"; p.write_bytes(b"1")
    assert run_mod.stable_id_from_path(p) == run_mod.stable_id_from_path(p)
    assert run_mod.stable_id_from_path(p).startswith("pathid_")


@pytest.mark.requirement("META-01")
def test_stable_id_from_bytes_changes_on_edit(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"; p.write_bytes(b"1")
    s1 = run_mod.stable_id_from_audio_bytes(p)
    p.write_bytes(b"2")
    s2 = run_mod.stable_id_from_audio_bytes(p)
    assert s1 != s2


@pytest.mark.requirement("META-01")
def test_build_queue_skips_missing_files(tmp_path: Path) -> None:
    real = tmp_path / "real.wav"; real.write_bytes(b"x" * 100)
    fake = tmp_path / "missing.wav"
    refs = run_mod.build_queue([real, fake])
    assert len(refs) == 1
    assert refs[0].path == real


@pytest.mark.requirement("META-01")
def test_run_dry_run_no_writes(
    tmp_path: Path, mock_backend: type[_MockBackend]
) -> None:
    audio = tmp_path / "a.wav"; audio.write_bytes(b"x" * 100)
    db = tmp_path / "state.db"
    s = run_mod.run(run_mod.build_queue([audio]), backend_name="mock", dry_run=True, db_path=db)
    assert s.analysed == 1
    assert not db.exists()


@pytest.mark.requirement("META-01")
def test_run_persists_and_is_idempotent(
    tmp_path: Path, mock_backend: type[_MockBackend]
) -> None:
    audio = tmp_path / "a.wav"; audio.write_bytes(b"x" * 100)
    db = tmp_path / "state.db"

    s1 = run_mod.run(run_mod.build_queue([audio]), backend_name="mock", dry_run=False, db_path=db, only_missing=False)
    assert s1.analysed == 1 and s1.failed == 0

    s2 = run_mod.run(run_mod.build_queue([audio]), backend_name="mock", dry_run=False, db_path=db, only_missing=True)
    # Queue is empty after only-missing filter; nothing happens.
    assert s2.analysed == 0 and s2.skipped_existing == 0 and s2.failed == 0

    with sqlite3.connect(str(db)) as conn:
        n_rows = conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]
        n_evt = conn.execute(
            "SELECT COUNT(*) FROM analysis_events WHERE event_type='analyze'"
        ).fetchone()[0]
    assert n_rows == 1
    assert n_evt == 1


@pytest.mark.requirement("META-01")
def test_run_main_argparse(
    tmp_path: Path, mock_backend: type[_MockBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "b.wav"; audio.write_bytes(b"x" * 100)
    from apps.shared import paths as _p
    monkeypatch.setattr(_p, "STATE_DB", tmp_path / "state.db")
    rc = run_mod.main(["--backend", "mock", "--dry-run", "--files", str(audio)])
    assert rc == 0
