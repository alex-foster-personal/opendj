"""``apps.analysis.run`` CLI tests (META-01)."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import run as run_mod
from apps.analysis.backends import DEFAULT_BACKEND, get_backend
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
            analyzed_at=datetime.now(UTC),
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



@pytest.mark.requirement("PARITY-06")
def test_a_pairs_file_with_a_non_string_member_is_a_usage_error(
    tmp_path: Path,
) -> None:
    """Malformed handoff, not a systemic fault - and the status has to say so.

    The outer shape is right and only the member types are wrong, which is
    what an encoder bug on the caller's side actually produces. Before the
    members were checked, ``Path(1)`` raised inside ``_dispatch()``, ``main()``
    translated it at its boundary, and the caller read EXIT_INTERNAL_ERROR (5)
    - "stop, this machine is broken" - for a file it wrote wrong itself.

    Nothing is patched. The backend is the SHIPPED default the drain really
    uses, and the run really traverses ``get_backend`` before it reads the
    handoff - which matters here, because an unresolvable backend exits 2 as
    well, so a test that skipped that step could report this contract green
    while proving only that the name was unknown. The precondition below
    pins it: the default backend resolves, therefore the 2 came from the
    file. The registry lookup needs no analysis extra installed; the CLI
    rejects the handoff long before any decode is attempted.
    """
    assert get_backend(DEFAULT_BACKEND) is not None, (
        "fixture precondition: the shipped default backend must resolve, or "
        "the usage exit below could be about the backend name instead"
    )
    handoff = tmp_path / "pairs.json"
    handoff.write_text(json.dumps([["sid00001", 1]]))

    with pytest.raises(SystemExit) as caught:
        run_mod.main(["--backend", DEFAULT_BACKEND, "--pairs-json", str(handoff)])

    assert caught.value.code == run_mod.EXIT_USAGE, (
        f"a malformed handoff file reported {caught.value.code}, which tells "
        "the chunking caller its machine is broken rather than its input"
    )


class _StartMethodBackend:
    """Reports how the WORKER process was started, in the key field.

    Module-level, so a spawned worker can import it by reference. It is the
    real production path: ``run`` resolves the backend once in the parent and
    the pool pickles the class, not a name that a spawned child could not
    find in its own, empty registry.
    """

    name = "start-method"
    version = "1"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=cls.version,
            analyzed_at=datetime.now(UTC),
            duration_s=1.0, sample_rate=44100,
            bpm=120.0, bpm_confidence=1.0,
            key_camelot="1A", key_openkey=multiprocessing.get_start_method(), key_confidence=1.0,
            energy=1,
        )


@pytest.mark.requirement("META-01")
def test_parallel_workers_are_spawned_not_forked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if the pool forks then a worker inherits librosa/numba/OpenBLAS thread and
    JIT state from the parent and can die with "segfault at 0 ip 0" (#1316);
    a spawned worker reports "spawn" from inside itself, and a forked one
    cannot, because the forked child shares the parent's start method"""
    from apps.analysis import backends

    monkeypatch.setitem(backends.BACKENDS, "start-method", _StartMethodBackend)
    refs = []
    for i in range(2):
        audio = tmp_path / f"t{i}.wav"
        audio.write_bytes(b"0")
        refs.append(run_mod.TrackRef(stable_id=f"sid{i:05d}", path=audio))
    db = tmp_path / "state.db"
    summary = run_mod.run(
        refs, backend_name="start-method", workers=2, only_missing=False, db_path=db
    )
    assert summary.failed == 0, summary.errors
    con = sqlite3.connect(db)
    try:
        methods = {row[0] for row in con.execute("SELECT key_openkey FROM analysis")}
    finally:
        con.close()
    assert methods == {"spawn"}, methods

