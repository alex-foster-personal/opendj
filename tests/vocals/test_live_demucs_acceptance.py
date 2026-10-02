"""Opt-in live demucs vocal-cache acceptance against SPIKE-B2 fixtures (#274).

Regression one-liners:
  - if Dub Congas demucs coverage > 5% then broken
  - if Mozart's House demucs IoU < 0.6 vs PVDI then broken
  - if trickle writes outside the disposable vocal-cache dir then broken
  - if a CUDA or CPU cache entry fails vcache._validate_entry or duration drifts then broken
  - if timeout cleanup leaves any uv/python/demucs/ffmpeg descendant then broken
  - manual_recovery_required is NOT a live leg (UNAVAILABLE: no unprivileged SIGKILL-survivor worker);
    unit coverage in tests/vocals/test_cli_queue.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from apps.parity.vocal import _region_endpoints, vocal_iou
from apps.shared.paths import DATA_DIR
from apps.vocals import cache as vcache
from apps.vocals import cli as vcli
from apps.vocals.cli import WORKER_SCRIPT
from apps.vocals.cli import main as vocals_main
from scripts.vocal_gcloud_farm import cmd_import_outbox
from tests.vocals import spike_b2_fixture as sb2
from tests.vocals._process_tree import cmdline_of, descendants_of, worker_like_pids
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
    # pytest.skip raises; the gate's isolated mypy has no pytest stubs to see that.
    assert source is not None
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
    # Stage under the stable id, as cmd_stage_playlist ships it: the runner names the
    # outbox JSON after the staged stem, and cmd_import_outbox keys the manifest by it.
    staged = inbox / f"{track.stable_id}{track.audio_path.suffix}"
    shutil.copy2(track.audio_path, staged)
    audio_mtime = staged.stat().st_mtime
    worker_sha256 = hashlib.sha256(WORKER_SCRIPT.read_bytes()).hexdigest()
    manifest_line = {
        "stable_id": track.stable_id,
        "src_path": str(staged),
        "audio_mtime": audio_mtime,
        "worker_sha256": worker_sha256,
    }
    (batch / "manifest.jsonl").write_text(json.dumps(manifest_line) + "\n", encoding="utf-8")
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
    assert [path.stem for path in outbox_files] == [track.stable_id]
    payload = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert payload.get("worker", {}).get("device_used") == "cuda"
    assert cmd_import_outbox(batch, outbox, spike_b2_fixture.data_dir) == 0
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


# Short enough to fire while the real uv -> python -> demucs worker is still running.
TIMEOUT_LEG_S = 8.0


@live_demucs
@pytest.mark.skipif(os.name == "nt", reason="POSIX descendant-leak regression")
def test_worker_timeout_leaves_no_worker_descendants(
    spike_b2_fixture: SpikeB2Fixture, tmp_path: Path
) -> None:
    """[if] the production worker times out [then] no uv/python/demucs/ffmpeg descendant remains.

    Runs the shipped command (``vcli._worker_command``: uv + the PEP 723 worker) on real
    SPIKE-B2 audio. A sampler records the worker tree while it runs, which is the positive
    control that a real worker was alive to be killed.
    """
    track = next(t for t in spike_b2_fixture.tracks if t.role == "vocal")
    baseline = set(descendants_of(os.getpid()))
    seen: dict[int, str] = {}
    born: dict[int, float] = {}
    last_alive_s = [0.0]
    stop = threading.Event()
    started = time.monotonic()

    def _still_alive(pid: int) -> bool:
        # A killed parent's child is reparented away from this process, so liveness
        # is tracked per observed pid (guarded against pid reuse by create_time),
        # never by re-walking this process's descendants.
        try:
            return psutil.Process(pid).create_time() == born[pid]
        except psutil.NoSuchProcess:
            return False

    def _sample() -> None:
        while not stop.is_set():
            for pid in worker_like_pids(descendants_of(os.getpid()) - baseline):
                if pid not in seen:
                    try:
                        born[pid] = psutil.Process(pid).create_time()
                    except psutil.NoSuchProcess:
                        continue
                    seen[pid] = cmdline_of(pid)
            if any(_still_alive(pid) for pid in list(born)):
                last_alive_s[0] = time.monotonic() - started
            time.sleep(0.1)

    sampler = threading.Thread(target=_sample, daemon=True)
    cache_file = vcache.cache_path(tmp_path, track.stable_id)
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with vcli._managed_track_claim(claim):
        sampler.start()
        try:
            with pytest.raises(RuntimeError, match="timed out"):
                vcli.run_worker(track.audio_path, timeout_s=TIMEOUT_LEG_S)
        finally:
            stop.set()
            sampler.join(timeout=5)
        assert any(WORKER_SCRIPT.name in cmdline for cmdline in seen.values()), (
            f"no production worker process was observed before the timeout: {seen}"
        )
        # The whole tree must be gone within the terminate grace, not merely by the time
        # run_worker returns: a surviving child holds stdout open, so run_worker waits it
        # out, and a check made only afterwards would pass on a leak.
        deadline_s = TIMEOUT_LEG_S + vcli.WORKER_TERMINATE_GRACE_S + 2.0
        assert last_alive_s[0] <= deadline_s, (
            f"worker descendants outlived the timeout: last seen at {last_alive_s[0]:.1f}s, "
            f"deadline {deadline_s:.1f}s"
        )
        alive = [pid for pid in list(born) if _still_alive(pid)]
        leaked = worker_like_pids(set(alive) | (descendants_of(os.getpid()) - baseline))
        assert leaked == [], f"worker descendants still alive: {leaked}"
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["released"] is True
    assert not record.get("manual_recovery_required")
