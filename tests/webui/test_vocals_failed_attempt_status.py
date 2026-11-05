"""A vocals attempt that fails after the 202 is reported by the status route.

``POST /vocals/analyze`` with ``mode: "one"`` answers 202 and runs the worker
in a background task. When that task raised, nothing recorded it: the status
route kept answering ``not_analyzed`` with no reason, so a new user watched a
track sit unanalyzed for as long as they cared to wait.

[if] a vocals attempt fails after the 202 [then] the status route reports the failed attempt and its reason, [else stop].

Real state.db, real vocal-cache, and a real worker subprocess launched through
the real ``_worker_command``; the clean worker is the shipped worker's own
``main()`` around a stand-in ``analyse`` (torch and demucs never enter the repo
environment).

Regression one-liners:
  - if a clean worker result isn't stored with the regions the worker computed then broken
  - if a worker that exits 0 with no result leaves status not_analyzed with no reason then broken
  - if a worker that exits non-zero leaves status not_analyzed with no reason then broken
  - if a failed attempt blocks the next attempt or outlives its success then broken
  - if a track that has vocals still reports an old failed attempt then broken
  - if an old failure outlives a newer attempt on the CLI or from-stems path then broken
  - if a request that was refused clears a recorded failure then broken
  - if a record that cannot be cleared strands the claim then broken
  - if the failure is recorded after the claim is released then broken
  - if a failure that cannot be recorded displaces the error that caused it then broken
  - if a failure record that is not valid UTF-8 answers 500 then broken
  - if an unreadable failure record reads as no failure then broken
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.vocals import attempts as vattempts
from apps.vocals import cache as vcache
from apps.vocals import cli as vcli
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import vocals as vocals_routes

pytestmark = pytest.mark.requirement("VOCALS-05")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = REPO_ROOT / "scripts" / "vocal_region_worker.py"

RESULT: dict[str, Any] = {
    "schema": vcache.VOCAL_CACHE_SCHEMA,
    "source": "demucs-htdemucs",
    "audio_path": "song.wav",
    "source_sample_rate": 44100,
    "analysis_sample_rate": 44100,
    "duration_s": 30.0,
    "device": "cpu",
    "params": {
        "model": "htdemucs",
        "hop_s": 0.5,
        "on_ratio": 0.1,
        "off_ratio": 0.05,
        "thresholds_adapted": False,
        "merge_gap_s": 1.5,
        "min_region_s": 1.0,
        "confidence_gain": 2.5,
    },
    "timings": {"load_s": 1.9, "separate_s": 24.9},
    "regions": [{"start_s": 0.0, "end_s": 9.0, "confidence": 1.0}],
    "coverage_pct": 30.0,
    "fps": 2.0,
}

_CLEAN = """
import importlib.util, json
spec = importlib.util.spec_from_file_location("vocal_region_worker", {worker!r})
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
worker.analyse = lambda audio_path, device_pref: json.loads({result!r})
worker.main()
"""
_SILENT = "import sys\nprint('done: 5 regions cov=81.0%', file=sys.stderr)\n"
_FAILING = "import sys\nprint('RuntimeError: weights download refused', file=sys.stderr)\nsys.exit(3)\n"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder-only library with one imported track and no rekordbox."""
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()
    audio = media / "song.wav"
    audio.write_bytes(b"RIFF song")
    conn = state_db.open_rw(data / "state" / "state.db")
    StateWriter(conn, actor="unit-test").upsert_track(
        stable_id="song",
        stable_id_tier="inferred",
        title="Song",
        artists=["X"],
        album=None,
        isrc=None,
        duration_ms=30_000,
        file_path=str(audio),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data / "master.plain.db")
    monkeypatch.setattr(rb_config, "STATE_DB", data / "state" / "state.db")
    monkeypatch.setattr(rb_config, "VOCAL_CACHE_DIR", vcache.cache_dir(data))
    monkeypatch.setenv(vcli.PACKAGED_WORKER_PYTHON_ENV, sys.executable)
    return data


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(data_dir / "state" / "state.db"),
        mount_frontend=False,
        stem_roots=(data_dir / "configured-stems",),
    )
    # The failure under test is raised inside a background task, after the
    # 202 has been sent; the client must not turn it into a test error.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def use_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, source: str) -> None:
    script = tmp_path / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setenv(vcli.PACKAGED_WORKER_SCRIPT_ENV, str(script))


def clean_worker_source() -> str:
    return _CLEAN.format(worker=str(WORKER_SCRIPT), result=json.dumps(RESULT))


def analyze(client: TestClient) -> None:
    response = client.post("/api/v1/vocals/analyze", json={"stable_ids": ["song"], "mode": "one"})
    assert response.status_code == 202, response.text
    assert response.json() == {"claimed": ["song"], "refused": {}}


def status_of(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/vocals/song/status")
    assert response.status_code == 200, response.text
    return response.json()


def test_clean_worker_result_is_stored_and_served_as_before(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the worker returns a clean result [then] its regions are stored and served, [else stop]."""
    use_worker(monkeypatch, tmp_path, "clean", clean_worker_source())
    analyze(client)

    entry = vcache.load_valid_entry(vcache.cache_path(data_dir, "song"), tmp_path / "media" / "song.wav")
    assert entry is not None
    assert entry["regions"] == [{"start_s": 0.0, "end_s": 9.0, "confidence": 1.0, "intensity": 4}]
    assert entry["coverage_pct"] == 30.0
    status = status_of(client)
    assert status["status"] == "demucs"
    assert [(r["start_s"], r["end_s"]) for r in status["regions"]] == [(0.0, 9.0)]
    assert status.get("failed_attempt") is None


def test_worker_with_no_result_is_reported_as_a_failed_attempt(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the worker exits 0 with no result [then] status reports a failed attempt and its reason, [else stop]."""
    use_worker(monkeypatch, tmp_path, "silent", _SILENT)
    before = datetime.now(UTC).replace(microsecond=0)
    analyze(client)

    assert not vcache.cache_path(data_dir, "song").exists()
    status = status_of(client)
    assert status["status"] == "not_analyzed"
    failed = status["failed_attempt"]
    assert "vocal_region_worker (exit 0) wrote 0 result frames" in failed["reason"]
    assert "done: 5 regions cov=81.0%" in failed["reason"]
    assert failed["error_type"] == "WorkerOutputError"
    failed_at = datetime.fromisoformat(failed["failed_at"])
    assert failed_at.utcoffset() == timedelta(0)
    assert before <= failed_at <= datetime.now(UTC)


def test_worker_that_exits_non_zero_is_reported_with_its_stderr(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the worker exits non-zero [then] status reports its exit code and stderr message, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)

    status = status_of(client)
    assert status["status"] == "not_analyzed"
    assert "vocal_region_worker failed (exit 3)" in status["failed_attempt"]["reason"]
    assert "RuntimeError: weights download refused" in status["failed_attempt"]["reason"]


def test_a_later_success_replaces_the_failed_attempt(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a failed attempt is retried and succeeds [then] status is demucs with no failure, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)
    assert status_of(client)["failed_attempt"] is not None

    use_worker(monkeypatch, tmp_path, "clean", clean_worker_source())
    analyze(client)  # claimed again: the failure released its claim

    status = status_of(client)
    assert status["status"] == "demucs"
    assert status["failed_attempt"] is None
    assert list((data_dir / "state" / "vocal-attempts").glob("*.json")) == []


def test_a_track_that_has_vocals_reports_no_failed_attempt(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] vocals exist for a track with an older failure record [then] status reports no failure, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)
    assert vattempts.attempt_path(data_dir, "song").is_file()

    # A later attempt on the CLI path succeeds, and starting it clears the record.
    use_worker(monkeypatch, tmp_path, "clean", clean_worker_source())
    ctx = vcli.Ctx(data_dir=data_dir)
    (track,) = vcli.load_state_tracks(ctx, ["song"])
    vcli._process_one(ctx, track, "[test]", 60)
    assert not vattempts.attempt_path(data_dir, "song").exists()

    # Even a record that is on disk is not reported beside real regions.
    vattempts.record_failure(data_dir, "song", RuntimeError("an older failure"))
    assert vattempts.attempt_path(data_dir, "song").is_file()
    status = status_of(client)
    assert status["status"] == "demucs"
    assert status["failed_attempt"] is None


def test_an_unreadable_failure_record_is_reported_not_ignored(client: TestClient, data_dir: Path) -> None:
    """[if] a failure record exists but cannot be parsed [then] status says so, never no failure, [else stop]."""
    record = vattempts.attempt_path(data_dir, "song")
    record.parent.mkdir(parents=True)
    record.write_text("{torn", encoding="utf-8")

    failed = status_of(client)["failed_attempt"]
    assert failed["error_type"] == "UnreadableAttemptRecord"
    assert "song.json cannot be read" in failed["reason"]
    assert failed["failed_at"] is None


def test_a_stable_id_cannot_name_a_record_outside_the_attempts_directory(data_dir: Path) -> None:
    """[if] a stable id would escape the attempts directory [then] it is refused, [else stop]."""
    with pytest.raises(ValueError, match="escapes vocal-attempts directory"):
        vattempts.attempt_path(data_dir, "../../state")


def test_a_failing_cli_attempt_does_not_leave_the_older_failure_as_the_last_attempt(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a newer attempt starts on the CLI path [then] the older HTTP failure is no longer reported, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)
    assert "exit 3" in status_of(client)["failed_attempt"]["reason"]

    use_worker(monkeypatch, tmp_path, "silent", _SILENT)
    ctx = vcli.Ctx(data_dir=data_dir)
    (track,) = vcli.load_state_tracks(ctx, ["song"])
    with pytest.raises(RuntimeError, match="wrote 0 result frames"):
        vcli._process_one(ctx, track, "[test]", 60)

    assert not vattempts.attempt_path(data_dir, "song").exists()
    assert status_of(client) == {
        "stable_id": "song",
        "status": "not_analyzed",
        "fps": None,
        "regions": [],
        "failed_attempt": None,
    }


def test_a_from_stems_success_clears_the_older_failure(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] vocals are derived from stems after a failed attempt [then] the failure record is cleared, [else stop]."""
    import numpy as np

    sf = pytest.importorskip("soundfile", reason="needs the optional soundfile package")
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)
    assert vattempts.attempt_path(data_dir, "song").is_file()

    bundle = data_dir / "configured-stems" / "song"
    bundle.mkdir(parents=True)
    sr = 44100
    t = np.arange(sr * 4, dtype=np.float32) / sr
    vocals = np.zeros(sr * 4, dtype=np.float32)
    vocals[sr : 3 * sr] = 0.4 * np.sin(2 * np.pi * 440 * t[sr : 3 * sr])
    quiet = 0.02 * np.sin(2 * np.pi * 110 * t)
    for part, mono in (("vocals", vocals), ("drums", quiet), ("bass", quiet), ("other", quiet)):
        sf.write(str(bundle / f"{part}.wav"), mono, sr, subtype="PCM_16")
    manifest = {
        "schema_version": 1,
        "stable_id": "song",
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/tmp/source.wav", "sha256": "a" * 64},
        "files": {part: f"{part}.wav" for part in ("vocals", "drums", "bass", "other")},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    response = client.post("/api/v1/vocals/analyze", json={"stable_ids": ["song"], "mode": "from-stems"})
    assert response.json() == {"claimed": ["song"], "refused": {}}, response.text
    assert status_of(client)["status"] == "demucs"
    assert not vattempts.attempt_path(data_dir, "song").exists()


def test_a_refused_request_leaves_the_recorded_failure_alone(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a request is refused because another attempt holds the claim [then] the recorded failure stays, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)
    recorded = status_of(client)["failed_attempt"]
    assert "exit 3" in recorded["reason"]

    held = vcli._claim_track(vcache.cache_path(data_dir, "song"))
    assert held is not None
    try:
        use_worker(monkeypatch, tmp_path, "clean", clean_worker_source())
        response = client.post("/api/v1/vocals/analyze", json={"stable_ids": ["song"], "mode": "one"})
        assert response.json() == {"claimed": [], "refused": {"song": "in_progress"}}, response.text
        assert status_of(client)["failed_attempt"] == recorded
    finally:
        vcli._release_track_claim(held)


def test_a_record_that_cannot_be_cleared_does_not_strand_the_claim(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the old failure record cannot be removed [then] the attempt releases its claim, [else stop]."""
    record = vattempts.attempt_path(data_dir, "song")
    record.mkdir(parents=True)  # unlink and replace both fail on a directory
    use_worker(monkeypatch, tmp_path, "clean", clean_worker_source())

    analyze(client)
    assert not vcache.cache_path(data_dir, "song").exists()  # the attempt did not run
    analyze(client)  # claimed again: the first attempt released its claim


def test_a_from_stems_request_with_no_bundle_leaves_the_recorded_failure_alone(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a from-stems request is refused for a missing bundle [then] the recorded failure stays, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)
    recorded = status_of(client)["failed_attempt"]
    assert "exit 3" in recorded["reason"]

    response = client.post("/api/v1/vocals/analyze", json={"stable_ids": ["song"], "mode": "from-stems"})
    assert response.json() == {"claimed": [], "refused": {"song": "missing_stems"}}, response.text
    assert status_of(client)["failed_attempt"] == recorded


def test_the_failure_is_recorded_while_the_claim_is_still_held(
    client: TestClient, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an attempt fails [then] its failure is written before its claim is released, [else stop]."""
    cache_file = vcache.cache_path(data_dir, "song")
    claim_free_at_record_time: list[bool] = []
    real_record_failure = vattempts.record_failure

    def observing_record_failure(*args: Any, **kwargs: Any) -> Any:
        # A second claim succeeds only if the failing attempt has already let go.
        second = vcli._claim_track(cache_file)
        claim_free_at_record_time.append(second is not None)
        if second is not None:
            vcli._release_track_claim(second)
        return real_record_failure(*args, **kwargs)

    monkeypatch.setattr(vattempts, "record_failure", observing_record_failure)
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    analyze(client)

    assert claim_free_at_record_time == [False]
    assert "exit 3" in status_of(client)["failed_attempt"]["reason"]  # and the real record was written
    use_worker(monkeypatch, tmp_path, "clean", clean_worker_source())
    analyze(client)  # the claim was released after the write


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a directory the process cannot write to")
def test_a_failure_that_cannot_be_recorded_does_not_displace_the_error_that_caused_it(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the failure record cannot be written [then] the worker's own error is still the one raised, [else stop]."""
    use_worker(monkeypatch, tmp_path, "failing", _FAILING)
    ctx = vcli.Ctx(data_dir=data_dir)
    (track,) = vcli.load_state_tracks(ctx, ["song"])
    cache_file = vcache.cache_path(data_dir, "song")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    attempts_root = vattempts.attempts_dir(data_dir)
    attempts_root.mkdir(parents=True)
    attempts_root.chmod(0o500)  # nothing to clear, and no record can be created
    try:
        with pytest.raises(RuntimeError, match=r"vocal_region_worker failed \(exit 3\)") as caught:
            vocals_routes._process_claimed_track(ctx, track, claim)
    finally:
        attempts_root.chmod(0o700)

    notes = " ".join(getattr(caught.value, "__notes__", []))
    assert "could not be recorded" in notes
    assert "PermissionError" in notes
    assert list(attempts_root.iterdir()) == []
    again = vcli._claim_track(cache_file)  # released despite the failed write
    assert again is not None
    vcli._release_track_claim(again)


def test_a_failure_record_that_is_not_utf8_is_reported_not_a_500(client: TestClient, data_dir: Path) -> None:
    """[if] a failure record holds a damaged byte [then] status reports it unreadable, never a 500, [else stop]."""
    record = vattempts.attempt_path(data_dir, "song")
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"schema": 1, "reason": "\xff\xfe"}')

    failed = status_of(client)["failed_attempt"]
    assert failed["error_type"] == "UnreadableAttemptRecord"
    assert "song.json cannot be read" in failed["reason"]
