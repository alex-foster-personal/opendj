"""Cross-platform vocal-worker farm-out loop.

Repo home for the ``run_worker.cmd`` farm-out loop sketched in
``.planning/bifrost2-handoff/HANDOFF.md`` section 4.2. Scans ``--inbox``
for audio files, optionally gates on the GPU being idle, runs the demucs
worker via ``apps.vocals.cli.run_worker`` (its ``MDT_VOCAL_WORKER_PYTHON``
interpreter override / ``uv run`` fallback, unmodified here), and on
success writes a wrapped result JSON into ``--outbox``, deletes the
processed input, and logs a ``done:`` line. On failure the input moves to
``inbox/failed/`` and the failure is logged; the loop continues.

Directory layout (HANDOFF section 2.5 -- inbox/outbox/logs/STOP are
siblings under one work root, e.g. ``D:\\demucs-work\\``): the STOP
sentinel is ``--inbox``'s PARENT directory / "STOP" -- create it to stop
the loop cleanly between tracks. Checked before every file, matching the
GPU gate.

Requirements (mini-PRD):
  ✔︎ ✅ --once processes whatever is in --inbox right now and exits
    (the default when neither --once nor --loop is passed); --loop polls
    every --poll-seconds until the STOP sentinel appears.
    [if] STOP exists before the first file [then] the pass exits, nothing
    processed
  ✔︎ ✅ --gpu-gate holds (skips this pass, leaves files queued) while
    nvidia-smi reports utilization>=10%% or memory.used>=500MB (HANDOFF
    4.2.b: "never contend with a game"); un-gated by default.
    [if] gpu_is_busy(reading) is True [then] no worker run this pass
  ✔︎ ✅ success -> outbox/<stem>.json wraps the worker JSON with
    {"worker": {"script_sha256", "host", "device_used"}}; input deleted;
    "done: <stem>" logged (stdout + logs/worker.log).
    [if] the worker succeeds [then] inbox no longer has that file
  ✔︎ ✅ failure -> input moved to inbox/failed/, failure logged, loop
    continues to the next file (a single bad track never wedges the farm).
    [if] run_worker raises [then] the file lands in inbox/failed/, not deleted

Run standalone:
  python scripts/vocal_worker_runner.py --inbox D:\\demucs-work\\inbox \\
      --outbox D:\\demucs-work\\outbox --logs D:\\demucs-work\\logs \\
      --device cuda --gpu-gate --loop --poll-seconds 60

-Claude
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.paths import AUDIO_EXTENSIONS
from apps.vocals.cli import WORKER_SCRIPT, run_worker

# ----- CFG -------------------------------------------------------------------
GPU_UTIL_BUSY_THRESHOLD: int = 10       # percent (HANDOFF 4.2.b)
GPU_MEM_BUSY_THRESHOLD_MB: int = 500    # MB (HANDOFF 4.2.b)
DEFAULT_POLL_SECONDS: float = 60.0
STOP_SENTINEL_NAME: str = "STOP"
FAILED_SUBDIR_NAME: str = "failed"
BELOW_NORMAL_PRIORITY_CLASS: int = 0x00004000


@dataclass(frozen=True)
class GpuReading:
    utilization_pct: int
    memory_used_mb: int


# ----- GPU gate ----------------------------------------------------------------

def read_gpu_state(nvidia_smi: str = "nvidia-smi") -> GpuReading:
    """One nvidia-smi sample. Fails fast (raises) if the binary is
    unusable -- never guess GPU state when --gpu-gate was requested."""
    proc = subprocess.run(
        [nvidia_smi, "--query-gpu=utilization.gpu,memory.used",
         "--format=csv,noheader,nounits"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"nvidia-smi failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("nvidia-smi returned no GPU rows")
    util_str, mem_str = (p.strip() for p in lines[0].split(","))
    return GpuReading(utilization_pct=int(util_str), memory_used_mb=int(mem_str))


def gpu_is_busy(
    reading: GpuReading,
    *,
    utilization_threshold: int = GPU_UTIL_BUSY_THRESHOLD,
    memory_threshold_mb: int = GPU_MEM_BUSY_THRESHOLD_MB,
) -> bool:
    return (
        reading.utilization_pct >= utilization_threshold
        or reading.memory_used_mb >= memory_threshold_mb
    )


# ----- logging -----------------------------------------------------------------

def _log(logs_dir: Path, line: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamped = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}"
    print(stamped, flush=True)
    with (logs_dir / "worker.log").open("a", encoding="utf-8") as fh:
        fh.write(stamped + "\n")


def _worker_script_sha256() -> str:
    return hashlib.sha256(WORKER_SCRIPT.read_bytes()).hexdigest()


def _pending_files(inbox: Path) -> list[Path]:
    return sorted(
        p for p in inbox.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def _stop_path(inbox: Path) -> Path:
    return inbox.parent / STOP_SENTINEL_NAME


def _read_resource_percent(resource_percent_file: Path) -> int:
    """Read the explicit 1..100 duty-cycle limit without guessing a default."""
    try:
        value = int(resource_percent_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"resource percent file must contain an integer 1..100: {resource_percent_file}"
        ) from exc
    if not 1 <= value <= 100:
        raise ValueError(
            f"resource percent must be in 1..100: {resource_percent_file}"
        )
    return value


def _sleep_duty_cycle(
    stop_path: Path,
    duration_seconds: float,
    sleep_fn: Callable[[float], None],
) -> bool:
    """Sleep in short chunks so a STOP sentinel interrupts throttling promptly."""
    remaining = duration_seconds
    while remaining > 0:
        if stop_path.exists():
            return False
        chunk = min(1.0, remaining)
        sleep_fn(chunk)
        remaining -= chunk
    return not stop_path.exists()


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}")
    return parsed


def _positive_finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a positive finite number: {value}"
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"expected a positive finite number: {value}"
        )
    return parsed


def _apply_windows_resource_controls(
    *,
    below_normal: bool,
    cpu_affinity_mask: int | None,
) -> None:
    """Apply process-wide controls inherited by worker subprocesses on Windows."""
    if not below_normal and cpu_affinity_mask is None:
        return
    if os.name != "nt":
        raise RuntimeError("Windows process resource controls require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    process_handle = kernel32.GetCurrentProcess()
    if below_normal and not kernel32.SetPriorityClass(
        process_handle, BELOW_NORMAL_PRIORITY_CLASS,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if cpu_affinity_mask is not None and not kernel32.SetProcessAffinityMask(
        process_handle, cpu_affinity_mask,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _failed_destination(audio_path: Path) -> Path:
    """Return a non-clobbering failed-file destination."""
    failed_dir = audio_path.parent / FAILED_SUBDIR_NAME
    failed_dir.mkdir(parents=True, exist_ok=True)
    candidate = failed_dir / audio_path.name
    index = 1
    while candidate.exists():
        candidate = failed_dir / f"{audio_path.stem}.{index}{audio_path.suffix}"
        index += 1
    return candidate


def _move_to_failed(audio_path: Path, logs_dir: Path, error: Exception) -> bool:
    """Quarantine a failed input without allowing a name collision to wedge the loop."""
    try:
        shutil.move(str(audio_path), str(_failed_destination(audio_path)))
    except OSError as move_error:
        _log(logs_dir, f"failed: {audio_path.name}: {error}; quarantine failed: {move_error}")
        return False
    _log(logs_dir, f"failed: {audio_path.name}: {error}")
    return False


def _write_json_atomic(result_path: Path, payload: dict[str, Any]) -> None:
    """Durably publish one result without exposing partially-written JSON."""
    lock_path = result_path.with_name(f".{result_path.name}.lock")
    temporary_path: Path | None = None
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(lock_fd)
    try:
        if result_path.exists():
            raise FileExistsError(
                f"result already exists and will not be overwritten: {result_path}"
            )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=result_path.parent,
            prefix=f".{result_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as result_file:
            temporary_path = Path(result_file.name)
            json.dump(payload, result_file, indent=1)
            result_file.flush()
            os.fsync(result_file.fileno())
        if result_path.exists():
            raise FileExistsError(
                f"result already exists and will not be overwritten: {result_path}"
            )
        os.replace(temporary_path, result_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


# ----- per-file processing -------------------------------------------------------

def process_one(
    audio_path: Path,
    outbox: Path,
    logs_dir: Path,
    device: str,
    *,
    run_worker_fn: Callable[[Path], dict[str, Any]] = run_worker,
) -> bool:
    """Run the worker on one file; True on success. Never raises -- a
    worker failure is logged and the file moved to inbox/failed/ so a
    single bad track never wedges the farm-out loop."""
    result_path = outbox / f"{audio_path.stem}.json"
    if result_path.exists():
        return _move_to_failed(
            audio_path,
            logs_dir,
            FileExistsError(f"result already exists and will not be overwritten: {result_path}"),
        )

    os.environ["MDT_VOCAL_WORKER_DEVICE"] = device
    try:
        result = run_worker_fn(audio_path)
    except Exception as exc:
        return _move_to_failed(audio_path, logs_dir, exc)

    if not isinstance(result, dict):
        return _move_to_failed(audio_path, logs_dir, ValueError("worker returned a non-object JSON payload"))
    outbox.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        return _move_to_failed(
            audio_path,
            logs_dir,
            FileExistsError(f"result already exists and will not be overwritten: {result_path}"),
        )
    wrapped: dict[str, Any] = {
        **result,
        "worker": {
            "script_sha256": _worker_script_sha256(),
            "host": socket.gethostname(),
            "device_used": result.get("device", device),
        },
    }
    try:
        _write_json_atomic(result_path, wrapped)
    except Exception as exc:
        return _move_to_failed(audio_path, logs_dir, exc)
    audio_path.unlink()
    _log(logs_dir, f"done: {audio_path.stem}")
    return True


# ----- pass / loop driver -------------------------------------------------------

def run_once(
    inbox: Path,
    outbox: Path,
    logs_dir: Path,
    device: str,
    *,
    gpu_gate: bool,
    gpu_utilization_threshold: int = GPU_UTIL_BUSY_THRESHOLD,
    gpu_memory_threshold_mb: int = GPU_MEM_BUSY_THRESHOLD_MB,
    resource_percent_file: Path | None = None,
    gpu_reader: Callable[[], GpuReading] = read_gpu_state,
    run_worker_fn: Callable[[Path], dict[str, Any]] = run_worker,
    clock_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Process every currently-pending inbox file once. Returns the count
    attempted (success + failure). A STOP sentinel or a busy GPU stops the
    pass early, leaving remaining files queued for the next call -- the
    gate is re-checked before EVERY file, not just at pass start."""
    stop_path = _stop_path(inbox)
    processed = 0
    for audio_path in _pending_files(inbox):
        if stop_path.exists():
            _log(logs_dir, "stop: STOP sentinel present, exiting before processing")
            break
        if resource_percent_file is not None:
            _read_resource_percent(resource_percent_file)
        if gpu_gate:
            reading = gpu_reader()
            if gpu_is_busy(
                reading,
                utilization_threshold=gpu_utilization_threshold,
                memory_threshold_mb=gpu_memory_threshold_mb,
            ):
                _log(
                    logs_dir,
                    f"hold: gpu busy (util={reading.utilization_pct}% "
                    f"mem={reading.memory_used_mb}MB), pausing this pass",
                )
                break
        started_at = clock_fn()
        process_one(audio_path, outbox, logs_dir, device, run_worker_fn=run_worker_fn)
        processed += 1
        if resource_percent_file is not None:
            resource_percent = _read_resource_percent(resource_percent_file)
            elapsed_seconds = clock_fn() - started_at
            duty_sleep_seconds = elapsed_seconds * (100 / resource_percent - 1)
            if duty_sleep_seconds > 0 and not _sleep_duty_cycle(
                stop_path, duty_sleep_seconds, sleep_fn,
            ):
                _log(logs_dir, "stop: STOP sentinel present during duty-cycle sleep")
                break
    return processed


def run_loop(
    inbox: Path,
    outbox: Path,
    logs_dir: Path,
    device: str,
    *,
    gpu_gate: bool,
    poll_seconds: float,
    gpu_utilization_threshold: int = GPU_UTIL_BUSY_THRESHOLD,
    gpu_memory_threshold_mb: int = GPU_MEM_BUSY_THRESHOLD_MB,
    resource_percent_file: Path | None = None,
    gpu_reader: Callable[[], GpuReading] = read_gpu_state,
    run_worker_fn: Callable[[Path], dict[str, Any]] = run_worker,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Repeat run_once until the STOP sentinel appears, sleeping
    poll_seconds between passes (this IS the "GPU busy -> sleep 60s,
    re-check" retry HANDOFF describes, at the default poll interval)."""
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError(
            f"poll seconds must be positive and finite: {poll_seconds}"
        )
    stop_path = _stop_path(inbox)
    while True:
        if stop_path.exists():
            _log(logs_dir, "stop: STOP sentinel present, exiting loop")
            return
        run_once(
            inbox, outbox, logs_dir, device,
            gpu_gate=gpu_gate,
            gpu_utilization_threshold=gpu_utilization_threshold,
            gpu_memory_threshold_mb=gpu_memory_threshold_mb,
            resource_percent_file=resource_percent_file,
            gpu_reader=gpu_reader, run_worker_fn=run_worker_fn,
        )
        if not _sleep_duty_cycle(stop_path, poll_seconds, sleep_fn):
            _log(logs_dir, "stop: STOP sentinel present during poll sleep")
            return


# ----- CLI -----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/vocal_worker_runner.py",
        description="Cross-platform demucs vocal-worker farm-out loop "
                    "(inbox scan -> worker -> outbox, HANDOFF section 4.2).",
    )
    parser.add_argument("--inbox", type=Path, required=True,
                        help="dir scanned for audio files to process")
    parser.add_argument("--outbox", type=Path, required=True,
                        help="dir results are written to as <stem>.json")
    parser.add_argument("--logs", type=Path, required=True,
                        help="dir holding worker.log")
    parser.add_argument("--device", default="auto",
                        choices=("auto", "cpu", "mps", "cuda"),
                        help="torch device preference passed to the worker")
    parser.add_argument("--gpu-gate", action="store_true",
                        help="hold while nvidia-smi reports util>=10%% or "
                             "mem>=500MB (never contend with a running game)")
    parser.add_argument("--gpu-utilization-threshold", type=int,
                        default=GPU_UTIL_BUSY_THRESHOLD,
                        help=f"GPU gate utilization threshold (default {GPU_UTIL_BUSY_THRESHOLD})")
    parser.add_argument("--gpu-memory-threshold-mb", type=int,
                        default=GPU_MEM_BUSY_THRESHOLD_MB,
                        help=f"GPU gate memory threshold MB (default {GPU_MEM_BUSY_THRESHOLD_MB})")
    parser.add_argument("--resource-percent-file", type=Path,
                        help="required 1..100 duty-cycle limit file, reread between tracks")
    parser.add_argument("--windows-below-normal", action="store_true",
                        help="set this runner and its children to BelowNormal priority")
    parser.add_argument("--cpu-affinity-mask", type=_positive_integer,
                        help="positive Windows CPU affinity bitmask, for example 0x07")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="process the current inbox contents once and "
                           "exit (the default)")
    mode.add_argument("--loop", action="store_true",
                      help="poll --inbox every --poll-seconds until the "
                           "STOP sentinel (--inbox's parent / STOP) appears")
    parser.add_argument("--poll-seconds", type=_positive_finite_float,
                        default=DEFAULT_POLL_SECONDS,
                        help=f"loop poll interval (default {DEFAULT_POLL_SECONDS}s)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_windows_resource_controls(
        below_normal=args.windows_below_normal,
        cpu_affinity_mask=args.cpu_affinity_mask,
    )
    args.inbox.mkdir(parents=True, exist_ok=True)
    if args.resource_percent_file is not None:
        _read_resource_percent(args.resource_percent_file)
    if args.loop:
        run_loop(
            args.inbox, args.outbox, args.logs, args.device,
            gpu_gate=args.gpu_gate, poll_seconds=args.poll_seconds,
            gpu_utilization_threshold=args.gpu_utilization_threshold,
            gpu_memory_threshold_mb=args.gpu_memory_threshold_mb,
            resource_percent_file=args.resource_percent_file,
        )
    else:
        run_once(
            args.inbox, args.outbox, args.logs, args.device,
            gpu_gate=args.gpu_gate,
            gpu_utilization_threshold=args.gpu_utilization_threshold,
            gpu_memory_threshold_mb=args.gpu_memory_threshold_mb,
            resource_percent_file=args.resource_percent_file,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
