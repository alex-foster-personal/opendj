"""Opt-in live demucs vocal-cache acceptance against SPIKE-B2 fixtures (#274).

Regression one-liners:
  - if Dub Congas demucs coverage > 5% then broken
  - if Mozart's House demucs IoU < 0.6 vs PVDI then broken
  - if trickle writes outside the disposable vocal-cache dir then broken
  - if a CUDA or CPU cache entry fails vcache._validate_entry or duration drifts then broken
  - if timeout cleanup leaves any uv/python/demucs/ffmpeg descendant then broken
  - if manual_recovery_required claim cannot be cleared and re-claimed then broken
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from apps.parity.vocal import _region_endpoints, vocal_iou
from apps.shared.paths import DATA_DIR
from apps.vocals import cache as vcache
from apps.vocals import cli as vcli
from apps.vocals.cli import WORKER_SCRIPT
from apps.vocals.cli import main as vocals_main
from scripts.vocal_gcloud_farm import cmd_import_outbox
from tests.vocals import spike_b2_fixture as sb2
from tests.vocals._process_tree import descendants_of, worker_like_pids
from tests.vocals.spike_b2_fixture import SpikeB2Fixture, SpikeB2Track

MDT_LIVE_DEMUCS_ACCEPTANCE = os.environ.get("MDT_LIVE_DEMUCS_ACCEPTANCE") == "1"
LIVE_DEMUCS_UNAVAILABLE = (
    "UNAVAILABLE: set MDT_LIVE_DEMUCS_ACCEPTANCE=1 and point "
    "VOCALS_ACCEPTANCE_SOURCE_DATA_DIR at a library with SPIKE-B2 tracks"
)


def live_demucs(fn):
    fn = pytest.mark.skipif(not MDT_LIVE_DEMUCS_ACCEPTANCE, reason=LIVE_DEMUCS_UNAVAILABLE)(fn)
    fn = pytest.mark.slow(fn)
    return pytest.mark.requirement("VOCALS-01")(fn)


def _ffprobe_duration_s(audio_path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())


def _duration_within_tolerance(entry: dict[str, Any], audio_path: Path) -> None:
    source_duration = _ffprobe_duration_s(audio_path)
    tolerance = max(0.5, 0.005 * source_duration)
    assert abs(float(entry["duration_s"]) - source_duration) <= tolerance


def _run_one_live(fixture: SpikeB2Fixture, track: SpikeB2Track) -> dict[str, Any]:
    code = vocals_main(
        [
            "one",
            "--force",
            "--stable-id",
            track.stable_id,
            "--data-dir",
            str(fixture.data_dir),
        ]
    )
    assert code == 0, f"one --force failed for {track.stable_id}"
    cache_file = vcache.cache_path(fixture.data_dir, track.stable_id)
    entry = vcache.load_valid_entry(cache_file, track.audio_path)
    assert entry is not None, f"missing cache entry for {track.stable_id}"
    vcache._validate_entry(entry, cache_file)
    _duration_within_tolerance(entry, track.audio_path)
    return entry


def _regions_for_iou(entry: dict[str, Any]) -> list[tuple[float, float]]:
    return _region_endpoints(
        [{"start_s": r["start_s"], "end_s": r["end_s"]} for r in entry["regions"]]
    )


@pytest.fixture(scope="module")
def spike_b2_fixture(tmp_path_factory: pytest.TempPathFactory) -> SpikeB2Fixture:
    source = sb2.resolve_source_data_dir()
    if source is None:
        pytest.skip("UNAVAILABLE: no source library for SPIKE-B2 acceptance tracks")
    dest = tmp_path_factory.mktemp("spike-b2-disposable")
    env_dest = os.environ.get("VOCALS_ACCEPTANCE_DATA_DIR")
    if env_dest:
        dest = Path(env_dest)
        dest.mkdir(parents=True, exist_ok=True)
    fixture = sb2.build_disposable_data_dir(dest, source)
    sb2.assert_disposable(fixture.data_dir / "state" / "vocal-cache", DATA_DIR)
    return fixture


@live_demucs
def test_spike_b2_demucs_pvdi_thresholds(spike_b2_fixture: SpikeB2Fixture) -> None:
    """[if] opt-in SPIKE-B2 audio present [then] coverage and IoU meet thresholds."""
    for track in spike_b2_fixture.tracks:
        t0 = time.perf_counter()
        entry = _run_one_live(spike_b2_fixture, track)
        wall_s = time.perf_counter() - t0
        device = entry.get("worker", {}).get("device", "unknown")
        coverage = float(entry["coverage_pct"])
        iou: float | None = None
        if track.role == "instr":
            assert track.max_coverage_pct is not None
            assert coverage <= track.max_coverage_pct
        elif track.role == "vocal":
            assert track.min_pvdi_iou is not None
            rb_regions = _region_endpoints(sb2.pvdi_regions(track.twoex_path))
            own_regions = _regions_for_iou(entry)
            iou, _, _ = vocal_iou(
                rb_regions,
                own_regions,
                duration_s=float(entry["duration_s"]),
            )
            assert iou >= track.min_pvdi_iou
        print(
            f"SPIKE-B2 {track.stable_id} coverage={coverage:.2f}% "
            f"iou={iou if iou is not None else 'n/a'} device={device} wall_s={wall_s:.1f}"
        )


@live_demucs
def test_cpu_cache_entry_device(spike_b2_fixture: SpikeB2Fixture) -> None:
    """[if] CPU path runs [then] cache worker.device is cpu and schema-valid."""
    track = next(t for t in spike_b2_fixture.tracks if t.role == "vocal")
    prev = os.environ.get("MDT_VOCAL_WORKER_DEVICE")
    os.environ["MDT_VOCAL_WORKER_DEVICE"] = "cpu"
    try:
        entry = _run_one_live(spike_b2_fixture, track)
    finally:
        if prev is None:
            os.environ.pop("MDT_VOCAL_WORKER_DEVICE", None)
        else:
            os.environ["MDT_VOCAL_WORKER_DEVICE"] = prev
    assert entry["worker"].get("device") == "cpu"


@live_demucs
def test_cuda_cache_import_path(spike_b2_fixture: SpikeB2Fixture, tmp_path: Path) -> None:
    """[if] CUDA farm-out available [then] outbox import yields valid cache."""
    if not _cuda_available(tmp_path):
        pytest.skip("UNAVAILABLE: no CUDA device (probed in the worker's PEP 723 env)")
    track = next(t for t in spike_b2_fixture.tracks if t.role == "vocal")
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    batch = tmp_path / "batch"
    inbox.mkdir()
    outbox.mkdir()
    batch.mkdir()
    staged = inbox / track.audio_path.name
    shutil.copy2(track.audio_path, staged)
    audio_mtime = staged.stat().st_mtime
    worker_sha256 = hashlib.sha256(WORKER_SCRIPT.read_bytes()).hexdigest()
    manifest_line = {
        "stable_id": track.stable_id,
        "src_path": str(staged),
        "audio_mtime": audio_mtime,
        "worker_sha256": worker_sha256,
    }
    (batch / "manifest.jsonl").write_text(
        json.dumps(manifest_line) + "\n", encoding="utf-8"
    )
    from scripts import vocal_worker_runner as runner

    rc = runner.main(
        [
            "--once",
            "--inbox",
            str(inbox),
            "--outbox",
            str(outbox),
            "--logs",
            str(tmp_path / "logs"),
            "--device",
            "cuda",
        ]
    )
    assert rc == 0
    outbox_files = list(outbox.glob("*.json"))
    assert outbox_files, "CUDA runner produced no outbox JSON"
    payload = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert payload.get("worker", {}).get("device_used") == "cuda"
    cmd_import_outbox(batch, outbox, spike_b2_fixture.data_dir)
    cache_file = vcache.cache_path(spike_b2_fixture.data_dir, track.stable_id)
    entry = vcache.load_valid_entry(cache_file, track.audio_path)
    assert entry is not None
    vcache._validate_entry(entry, cache_file)
    _duration_within_tolerance(entry, track.audio_path)


def _cuda_available(tmp_path: Path) -> bool:
    """Probe CUDA inside the demucs worker's own PEP 723 env.

    torch never enters the repo venv, so probing ``sys.executable`` could only
    ever answer "no torch". Reuse the worker script's inline metadata so the
    probe runs against the exact torch build the worker would use; a probe
    that cannot import torch is UNKNOWN and fails loudly rather than skipping.
    """
    worker_text = WORKER_SCRIPT.read_text(encoding="utf-8")
    metadata_end = worker_text.index("\n# ///\n") + len("\n# ///\n")
    probe = tmp_path / "cuda_probe.py"
    probe.write_text(
        worker_text[:metadata_end]
        + "import torch\n"
        + "raise SystemExit(0 if torch.cuda.is_available() else 3)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["uv", "run", "--script", str(probe)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 3:
        return False
    raise RuntimeError(
        f"UNKNOWN: CUDA probe could not run (rc={result.returncode}): {result.stderr[-2000:]}"
    )


@live_demucs
@pytest.mark.skipif(os.name == "nt", reason="POSIX descendant-leak regression")
def test_worker_timeout_leaves_no_worker_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] worker times out [then] no uv/python/demucs/ffmpeg descendant remains."""
    worker_pid_file = tmp_path / "worker.pid"
    child_pid_file = tmp_path / "child.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, signal, subprocess, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)'])\n"
        "open(sys.argv[2], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        vcli,
        "_worker_command",
        lambda _audio: [
            sys.executable,
            str(worker),
            str(worker_pid_file),
            str(child_pid_file),
        ],
    )
    cache_file = vcache.cache_path(tmp_path, "timeout-leak")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with vcli._managed_track_claim(claim):
        with pytest.raises(RuntimeError, match="timed out"):
            vcli.run_worker(child_pid_file, timeout_s=0.5)
        worker_pid = int(worker_pid_file.read_text(encoding="utf-8"))
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        leaked = worker_like_pids(descendants_of(worker_pid))
        assert leaked == [], f"worker descendants still alive: {leaked}"
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["released"] is True
    assert not record.get("manual_recovery_required")


@live_demucs
def test_manual_recovery_claim_operable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] manual_recovery_required set [then] lock delete allows reclaim [else stop]."""
    # Operator recovery: confirm manual_recovery_required, verify no live PIDs,
    # delete .json.lock, then re-run `python -m apps.vocals one --stable-id <id>`.

    class RunningProcess:
        pid = os.getpgid(0)

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("worker", timeout)
            return 0

    def _kill_group(_pid: int, requested_signal: int) -> None:
        if requested_signal == signal.SIGKILL:
            raise PermissionError("forced escalation denial")

    monkeypatch.setattr(vcli, "_WINDOWS", False)
    monkeypatch.setattr(vcli.os, "killpg", _kill_group)
    cache_file = vcache.cache_path(tmp_path, "manual-recovery")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with pytest.raises(vcli.WorkerCleanupError), vcli._managed_track_claim(claim):
        vcli._terminate_worker_tree(RunningProcess())  # type: ignore[arg-type]
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["manual_recovery_required"] is True
    assert vcli._claim_track(cache_file) is None
    _clear_manual_recovery_claim(claim.path)
    successor = vcli._claim_track(cache_file)
    assert successor is not None
    vcli._release_track_claim(successor)


def _clear_manual_recovery_claim(lock_path: Path) -> None:
    record = vcli._read_claim_record(lock_path)
    if record is None or not record.get("manual_recovery_required"):
        raise ValueError(f"claim is not manual_recovery_required: {lock_path}")
    lock_path.unlink(missing_ok=True)
