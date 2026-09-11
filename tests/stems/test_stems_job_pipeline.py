"""The stems job, end to end through the REAL engine pipeline.

Everything here is the production path: the real ``JobStore``, the real
``JobRunner``, a real forked worker subprocess, the real progress protocol,
the real bundle writer, the real progress observer. ONE thing is substituted
-- the Modal call itself -- through the worker's declared
``MDT_STEMS_SEPARATOR`` seam, because the alternative is billing a GPU per
test run.

That boundary is the point. The parts most likely to break are the joins
(does the argv spawn, does stdout parse, does a bundle land where the reader
looks, does an event carry the right track), and every one of them is
exercised for real here.

Single-line intent:
  - if the argv does not spawn then the kind is unrunnable no matter how well
    its payload validates
  - if the worker's stdout drifts off the protocol then the runner kills a
    job that was doing fine
  - if a bundle lands anywhere but state/stems/<stable_id>/ then the webui
    reader never sees it
  - if library.changed does not fire PER TRACK then the library only lights
    up when the whole batch ends
  - if a run with the separator override wrote a manifest naming a real
    model then a test artifact would be indistinguishable from a paid one

-Claude
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.runner import (
    JobRunner,
    register_progress_observer,
    register_reconcile,
    register_worker,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobStore
from apps.shared import events
from apps.stems import job as stems_job

REPO_ROOT = Path(__file__).resolve().parents[2]
SEPARATOR = "tests.stems.modal_separator_double:separate"
# A forked worker under `uv run --with modal` resolves the project and may
# fetch modal into an overlay env on a cold cache.
RUN_TIMEOUT_S = 300.0


def _farm_stem_bundle_schema() -> int:
    """scripts/modal_vocal_farm.py::STEM_BUNDLE_SCHEMA, without importing modal."""
    import ast

    tree = ast.parse((REPO_ROOT / "scripts" / "modal_vocal_farm.py").read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "STEM_BUNDLE_SCHEMA"
            and node.value is not None
        ):
            return int(ast.literal_eval(node.value))
    raise AssertionError(
        "STEM_BUNDLE_SCHEMA literal missing from scripts/modal_vocal_farm.py"
    )


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None
    or shutil.which("ffprobe") is None
    or shutil.which("uv") is None,
    reason="the pipeline test needs ffmpeg, ffprobe and uv on PATH",
)


@pytest.fixture(autouse=True)
def _default_modal_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most unit tests assume the Modal farm path unless they override.

    CI has no relay, so without this pin ``build_argv`` spawns
    ``scripts/stems_local_worker.py``, which ignores ``MDT_STEMS_SEPARATOR``
    and writes stem_bundle_worker schema 3.
    """
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "modal",
    )


class _RecordingHub:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


def _make_source_audio(path: Path, seconds: float = 3.0) -> None:
    """A real, synthesised source file. Test INPUT, not a test artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac", "2", "-b:a", "192k", str(path),
        ],
        check=True,
    )


def _make_state_db(data_dir: Path, rows: list[tuple[str, Path, int]]) -> None:
    state_db = data_dir / "state" / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db)
    try:
        connection.execute(
            "CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, "
            "file_path TEXT, duration_ms INTEGER)"
        )
        connection.executemany(
            "INSERT INTO tracks (stable_id, file_path, duration_ms) VALUES (?,?,?)",
            [(sid, str(path), ms) for sid, path, ms in rows],
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def kind() -> Iterator[None]:
    register_worker(stems_job.JOB_KIND, stems_job.build_argv)
    register_progress_observer(stems_job.JOB_KIND, stems_job.on_progress)
    register_reconcile(stems_job.JOB_KIND, stems_job.reconcile_from_disk)
    yield
    unregister_worker(stems_job.JOB_KIND)


@pytest.fixture
def hub() -> Iterator[_RecordingHub]:
    recorder = _RecordingHub()
    events.set_hub(recorder)
    yield recorder
    events.set_hub(None)


@pytest.fixture
def separator_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the worker at the double, and keep argv on the Modal worker
    that actually reads MDT_STEMS_SEPARATOR.

    After #1883, resolve_stems_executor falls back to local when the relay
    is unconfigured (CI). The local worker ignores this env var and runs
    htdemucs, so the job never goes terminal inside RUN_TIMEOUT_S.
    """
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "modal",
    )
    monkeypatch.setenv("MDT_STEMS_SEPARATOR", SEPARATOR)
    yield


def _drive(store: JobStore, job_id: str) -> dict[str, Any]:
    """Run the supervisor until the row goes terminal, or time out loudly."""

    async def _go() -> dict[str, Any]:
        runner = JobRunner(store, poll_s=0.05)
        await runner.start()
        try:
            waited = 0.0
            while waited < RUN_TIMEOUT_S:
                row = store.get(job_id)
                if row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                    return row
                await asyncio.sleep(0.1)
                waited += 0.1
            raise AssertionError(
                f"job {job_id} never went terminal in {RUN_TIMEOUT_S}s; "
                f"last status {store.get(job_id)['status']!r}"
            )
        finally:
            await runner.stop()

    return asyncio.run(_go())


def test_two_tracks_land_as_bundles_and_announce_themselves_one_by_one(
    tmp_path: Path, kind: None, hub: _RecordingHub, separator_seam: None
) -> None:
    """The whole deliverable in one run: enqueue -> Modal seam -> bundles on
    disk in the reader's layout -> one library event per finished track."""
    data_dir = tmp_path / "data"
    music = tmp_path / "music"
    sid_a, sid_b = "a" * 40, "b" * 40
    track_a, track_b = music / "a.mp3", music / "b.mp3"
    _make_source_audio(track_a)
    _make_source_audio(track_b)
    _make_state_db(data_dir, [(sid_a, track_a, 3000), (sid_b, track_b, 3000)])

    store = JobStore(data_dir / "jobs.db", boot_id="boot-test", owner_pid=os.getpid())
    store.recover()
    enqueued = store.enqueue(
        stems_job.JOB_KIND,
        {"stable_ids": [sid_a, sid_b], "tier": "M", "data_dir": str(data_dir)},
    )

    row = _drive(store, enqueued["id"])
    assert row["status"] == "succeeded", row.get("error")

    # 1. Bundles where the webui reader looks, in the farm's own layout.
    stems_root = data_dir / "state" / "stems"
    for stable_id in (sid_a, sid_b):
        bundle = stems_root / stable_id
        assert bundle.is_dir(), f"no bundle directory for {stable_id}"
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["stable_id"] == stable_id
        assert manifest["schema_version"] == _farm_stem_bundle_schema()
        assert {"sample_rate", "frame_count", "channels"} <= set(manifest["audio"])
        assert "preset" in manifest
        assert set(manifest["files"]) == {"vocals", "drums", "bass", "other"}
        for name in manifest["files"].values():
            part = bundle / name
            assert part.is_file() and part.stat().st_size > 0
            assert part.read_bytes()[:4] == b"fLaC", f"{name} is not real FLAC"

    # 2. A run through the seam is stamped as one, so a test artifact can
    #    never be mistaken for a separation somebody paid for.
    stamped = json.loads((stems_root / sid_a / "manifest.json").read_text())
    assert stamped["model"]["name"].startswith("test-double:")
    assert stamped["preset"]["tag"].startswith("test-double:")

    # 3. One library event per track, published WHILE the job ran.
    library = [p for topic, p in hub.published if topic == "library.changed"]
    announced = [sid for event in library for sid in event["ids"]]
    assert sorted(announced) == sorted([sid_a, sid_b])
    assert all(event["kind"] == stems_job.LIBRARY_KIND for event in library)

    # 4. The job row carried real progress, not a single jump to done.
    jobs_events = [p for topic, p in hub.published if topic == "jobs.updated"]
    seen = sorted({round(float(event["progress"]), 3) for event in jobs_events})
    assert seen[-1] == 1.0
    assert any(0.0 < value < 1.0 for value in seen), (
        f"no intermediate progress was published, only {seen} -- the TopBar "
        "bar would jump straight from nothing to done"
    )


def test_a_track_with_no_audio_on_disk_fails_before_any_separation(
    tmp_path: Path, kind: None, hub: _RecordingHub, separator_seam: None
) -> None:
    """if a missing file were skipped then the job would report success for a
    track that has no stems and never will"""
    data_dir = tmp_path / "data"
    sid = "c" * 40
    _make_state_db(data_dir, [(sid, tmp_path / "gone.mp3", 3000)])

    store = JobStore(data_dir / "jobs.db", boot_id="boot-test", owner_pid=os.getpid())
    store.recover()
    enqueued = store.enqueue(
        stems_job.JOB_KIND,
        {"stable_ids": [sid], "tier": "M", "data_dir": str(data_dir)},
    )

    row = _drive(store, enqueued["id"])
    assert row["status"] == "failed"
    assert "no audio on disk" in (row["error"] or "")
    assert not (data_dir / "state" / "stems").exists()


def test_the_worker_refuses_an_unknown_stable_id(tmp_path: Path) -> None:
    """if an unknown id were skipped then a caller could ask for ten tracks,
    get eight, and be told the job succeeded"""
    data_dir = tmp_path / "data"
    _make_state_db(data_dir, [])
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "stems_modal_worker.py"),
            "--tier", "M",
            "--data-dir", str(data_dir),
            "--stable-id", "d" * 40,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        # The refusal IS the assertion, so a non-zero exit is expected.
        check=False,
    )
    assert result.returncode != 0
    assert "are not in" in result.stderr


def test_the_worker_refuses_both_ids_and_a_scope(tmp_path: Path) -> None:
    """if both were accepted then the run's target set would be ambiguous"""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "stems_modal_worker.py"),
            "--tier", "M",
            "--data-dir", str(tmp_path),
            "--stable-id", "e" * 40,
            "--scope", "pending",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        # The refusal IS the assertion, so a non-zero exit is expected.
        check=False,
    )
    assert result.returncode != 0
    assert "exactly one of" in result.stderr
