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
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from apps.shared.paths import AUDIO_EXTENSIONS
from apps.vocals.cli import WORKER_SCRIPT, run_worker

# ----- CFG -------------------------------------------------------------------
GPU_UTIL_BUSY_THRESHOLD: int = 10       # percent (HANDOFF 4.2.b)
GPU_MEM_BUSY_THRESHOLD_MB: int = 500    # MB (HANDOFF 4.2.b)
DEFAULT_POLL_SECONDS: float = 60.0
STOP_SENTINEL_NAME: str = "STOP"
FAILED_SUBDIR_NAME: str = "failed"


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


def gpu_is_busy(reading: GpuReading) -> bool:
    return (
        reading.utilization_pct >= GPU_UTIL_BUSY_THRESHOLD
        or reading.memory_used_mb >= GPU_MEM_BUSY_THRESHOLD_MB
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
    os.environ["MDT_VOCAL_WORKER_DEVICE"] = device
    try:
        result = run_worker_fn(audio_path)
    except Exception as exc:
        failed_dir = audio_path.parent / FAILED_SUBDIR_NAME
        failed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(audio_path), str(failed_dir / audio_path.name))
        _log(logs_dir, f"failed: {audio_path.name}: {exc}")
        return False

    outbox.mkdir(parents=True, exist_ok=True)
    wrapped: dict[str, Any] = {
        **result,
        "worker": {
            "script_sha256": _worker_script_sha256(),
            "host": socket.gethostname(),
            "device_used": result.get("device", device),
        },
    }
    (outbox / f"{audio_path.stem}.json").write_text(
        json.dumps(wrapped, indent=1), encoding="utf-8"
    )
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
    gpu_reader: Callable[[], GpuReading] = read_gpu_state,
    run_worker_fn: Callable[[Path], dict[str, Any]] = run_worker,
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
        if gpu_gate:
            reading = gpu_reader()
            if gpu_is_busy(reading):
                _log(
                    logs_dir,
                    f"hold: gpu busy (util={reading.utilization_pct}% "
                    f"mem={reading.memory_used_mb}MB), pausing this pass",
                )
                break
        process_one(audio_path, outbox, logs_dir, device, run_worker_fn=run_worker_fn)
        processed += 1
    return processed


def run_loop(
    inbox: Path,
    outbox: Path,
    logs_dir: Path,
    device: str,
    *,
    gpu_gate: bool,
    poll_seconds: float,
    gpu_reader: Callable[[], GpuReading] = read_gpu_state,
    run_worker_fn: Callable[[Path], dict[str, Any]] = run_worker,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Repeat run_once until the STOP sentinel appears, sleeping
    poll_seconds between passes (this IS the "GPU busy -> sleep 60s,
    re-check" retry HANDOFF describes, at the default poll interval)."""
    stop_path = _stop_path(inbox)
    while True:
        if stop_path.exists():
            _log(logs_dir, "stop: STOP sentinel present, exiting loop")
            return
        run_once(
            inbox, outbox, logs_dir, device,
            gpu_gate=gpu_gate, gpu_reader=gpu_reader, run_worker_fn=run_worker_fn,
        )
        sleep_fn(poll_seconds)


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="process the current inbox contents once and "
                           "exit (the default)")
    mode.add_argument("--loop", action="store_true",
                      help="poll --inbox every --poll-seconds until the "
                           "STOP sentinel (--inbox's parent / STOP) appears")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS,
                        help=f"loop poll interval (default {DEFAULT_POLL_SECONDS}s)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.inbox.mkdir(parents=True, exist_ok=True)
    if args.loop:
        run_loop(
            args.inbox, args.outbox, args.logs, args.device,
            gpu_gate=args.gpu_gate, poll_seconds=args.poll_seconds,
        )
    else:
        run_once(args.inbox, args.outbox, args.logs, args.device, gpu_gate=args.gpu_gate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
