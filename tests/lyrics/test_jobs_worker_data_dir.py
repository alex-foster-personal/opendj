"""Issue #2965: lyrics jobs worker honors MDT_DATA_DIR / --state-dir.

Regression lines (one-line if/then, house format):
- if batch subprocesses still reference REPO_ROOT/data/state when --state-dir
  points outside the repo then broken
- if cloud live runs without R2 credentials do not fail before subprocess/Modal
  then broken
- if indexed stem bundles are re-separated on Modal instead of hydrated first
  then broken
"""

from __future__ import annotations

import json
import math
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.cloud.stem_hydration import HydrationOutcome
from apps.lyrics import jobs as jobs_mod
from apps.lyrics.batch import (
    BatchReport,
    Cmds,
    StageBlocked,
    TrackPlan,
    _lane_stems,
    batch_paths_for,
    run_batch,
)
from apps.shared.paths import PROJECT_ROOT
from apps.shared.state import db as state_db
from tests.lyrics.conftest import seed_stamped_policy, use_cloud_mode, use_local_mode


def _write_wav(path: Path, seconds: float = 0.5, sample_rate: int = 8000) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(
            b"".join(
                int(3000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) * 2
                for i in range(frames)
            )
        )


@dataclass
class Recorder:
    cmds: list[list[str]]
    progress: list[str]
    _lock: threading.Lock

    def __init__(self) -> None:
        self.cmds = []
        self.progress = []
        self._lock = threading.Lock()

    def __call__(self, cmd: list[str]) -> None:
        with self._lock:
            self.cmds.append(list(cmd))
        if any("modal_roformer_spike.py" in part for part in cmd):
            raise AssertionError(f"Modal stems must not run: {' '.join(cmd)}")

    def note(self, msg: str) -> None:
        with self._lock:
            self.progress.append(msg)


def _seed_state(tmp_path: Path, stable_ids: list[str]):
    library = tmp_path / "library"
    library.mkdir()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    db_path = state_dir / "state.db"
    conn = state_db.open_rw(db_path)
    for sid in stable_ids:
        audio = library / f"{sid}.wav"
        _write_wav(audio)
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "duration_ms, file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, ?, 60000, ?, '2026-09-01T00:00:00+00:00', "
            "'2026-09-01T00:00:00+00:00')",
            (sid, f"Title {sid}", json.dumps([f"Artist {sid}"]), str(audio)),
        )
    conn.commit()
    conn.close()
    paths = batch_paths_for(state_dir)
    paths.eval_dir.mkdir(parents=True, exist_ok=True)
    paths.bench_dir.mkdir(parents=True, exist_ok=True)
    paths.stems_root.mkdir(parents=True, exist_ok=True)
    return state_dir, paths


def test_jobs_worker_subprocess_uses_state_dir_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_local_mode(monkeypatch)
    monkeypatch.delenv("MDT_DATA_DIR", raising=False)
    state_dir, paths = _seed_state(tmp_path, ["sid-a"])
    jobs_mod.enqueue(state_dir, "lyricsync", ["sid-a"])
    recorded_cmds: list[list[str]] = []
    progress_lines: list[str] = []

    def progress(msg: str) -> None:
        progress_lines.append(msg)

    def runner(cmd: list[str]) -> None:
        recorded_cmds.append(list(cmd))
        if any("lyrics_crate_metadata.py" in p for p in cmd):
            corpus_dir = paths.eval_dir / "job-test"
            rows = json.loads((corpus_dir / "tracks.json").read_text(encoding="utf-8"))
            conn = state_db.open_ro(paths.state_db)
            for row in rows:
                hit = conn.execute(
                    "SELECT stable_id FROM tracks WHERE file_path = ?",
                    (row["source_path"],),
                ).fetchone()
                row["stable_id"] = hit[0]
            conn.close()
            (corpus_dir / "tracks.json").write_text(json.dumps(rows, indent=1))
        elif any("modal_roformer_spike.py" in p for p in cmd):
            return

    try:
        run_batch(
            corpus="job-test",
            stable_ids=["sid-a"],
            live=True,
            runner=runner,
            progress=progress,
            paths=paths,
            repo_root=PROJECT_ROOT,
        )
    except StageBlocked:
        pass

    metadata_cmds = [
        cmd for cmd in recorded_cmds
        if any("lyrics_crate_metadata.py" in p for p in cmd)
    ]
    assert metadata_cmds, "metadata stage must run"
    meta = metadata_cmds[0]
    assert "--state-dir" in meta
    assert meta[meta.index("--state-dir") + 1] == str(state_dir)
    repo_state = str(PROJECT_ROOT / "data" / "state")
    assert repo_state not in " ".join(meta)
    staged = paths.eval_dir / "job-test" / "tracks.json"
    assert staged.is_file() or any("corpus: staged" in line for line in progress_lines)


def test_run_batch_refuses_missing_r2_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_cloud_mode(monkeypatch)
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    _state_dir, paths = _seed_state(tmp_path, ["sid-a"])
    conn = state_db.open_rw(paths.state_db)
    seed_stamped_policy(conn, asset_kind="karaoke_words", mode="pinned")
    seed_stamped_policy(conn, asset_kind="stem_bundle", mode="pinned")
    conn.commit()
    conn.close()

    def fail_runner(_cmd: list[str]) -> None:
        pytest.fail("runner must not be called when R2 credentials are missing")

    with pytest.raises(StageBlocked) as exc:
        run_batch(
            corpus="tbatch",
            stable_ids=["sid-a"],
            live=True,
            runner=fail_runner,
            paths=paths,
            repo_root=PROJECT_ROOT,
        )
    msg = str(exc.value)
    assert "R2_ACCOUNT_ID" in msg
    assert "R2_ACCESS_KEY_ID" in msg
    assert "R2_SECRET_ACCESS_KEY" in msg
    assert "doppler run" in msg


def test_lane_stems_hydrates_indexed_bundle_before_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_local_mode(monkeypatch)
    state_dir, paths = _seed_state(tmp_path, ["sid-a"])
    data_dir = paths.data_dir
    stem_index.save_cached_index(
        data_dir,
        {"sid-a": {"manifest.json": "a" * 64, "vocals.wav": "b" * 64}},
    )

    def fake_source(_data_dir: Path) -> object:
        return object()

    manifest_written = {"done": False}

    def fake_hydrate(
        stable_id: str,
        *,
        data_dir: Path,
        source: object,
        index: dict[str, dict[str, str]],
        stems_dir: Path | None = None,
    ) -> HydrationOutcome:
        root = stems_dir or paths.stems_root
        bundle = root / stable_id
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "layout": "roformer2",
                    "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
                }
            ),
            encoding="utf-8",
        )
        (bundle / "vocals.wav").write_bytes(b"voc")
        (bundle / "instrumental.wav").write_bytes(b"ins")
        manifest_written["done"] = True
        return HydrationOutcome(stable_id, "hydrated")

    monkeypatch.setattr(
        "apps.lyrics.batch.resolve_stem_hydration_source", fake_source
    )
    monkeypatch.setattr("apps.lyrics.batch.hydrate_one", fake_hydrate)

    corpus = "tbatch"
    corpus_dir = paths.eval_dir / corpus
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "stems").mkdir()
    audio = paths.state_db.parent.parent / "library" / "sid-a.wav"
    (corpus_dir / "audio").mkdir()
    (corpus_dir / "audio" / f"{corpus}000.wav").symlink_to(audio)
    report = BatchReport(corpus=corpus, live=True)
    report.tracks.append(
        TrackPlan(
            stable_id="sid-a",
            track_id=f"{corpus}000",
            db_file_path=str(audio),
            audio_path=audio,
            registered=False,
        )
    )
    cmds = Cmds(corpus, corpus_dir, paths.bench_dir, state_dir)
    recorder = Recorder()
    progress_lines: list[str] = []

    def progress(msg: str) -> None:
        progress_lines.append(msg)

    _lane_stems(cmds, report, recorder, progress, paths)

    assert manifest_written["done"]
    assert any("sid-a hydrated from fleet index" in line for line in progress_lines)
    assert not any("modal_roformer_spike.py" in " ".join(cmd) for cmd in recorder.cmds)
