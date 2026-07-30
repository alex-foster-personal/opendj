#!/usr/bin/env python3
"""Process stem inbox on a farm host: one bundle per audio file.

Expects files named ``<stable_id>.<ext>`` plus optional ``manifest.jsonl``.
Writes completed bundles as ``outbox/<stable_id>.tar`` containing the 4 wavs
+ manifest.json (Mac untars into data/state/stems/<stable_id>/).

Usage on VM:
  MDT_STEM_WORKER_PYTHON=/opt/mdt-stems/venv/bin/python \\
  python scripts/stem_farm_runner.py --inbox ... --outbox ... --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "stem_bundle_worker.py"
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif"}


def _one(audio: Path, outbox: Path, device: str, py: str) -> str:
    stable_id = audio.stem
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix=f"stem-{stable_id}-") as tmp:
        bundle = Path(tmp) / stable_id
        cmd = [
            py,
            str(WORKER),
            "--audio",
            str(audio),
            "--stable-id",
            stable_id,
            "--out-dir",
            str(bundle),
            "--device",
            device,
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{stable_id} failed: {(proc.stderr or proc.stdout)[-1500:]}"
            )
        tar_path = outbox / f"{stable_id}.tar"
        with tarfile.open(tar_path, "w") as tar:
            for name in (
                "manifest.json",
                "vocals.wav",
                "drums.wav",
                "bass.wav",
                "other.wav",
            ):
                tar.add(bundle / name, arcname=name)
        meta = json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])
        meta["wall_host_s"] = round(time.time() - t0, 1)
        (outbox / f"{stable_id}.json").write_text(
            json.dumps(meta) + "\n", encoding="utf-8"
        )
    return f"OK {stable_id} {meta.get('realtime_factor')}x"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--inbox", type=Path, required=True)
    p.add_argument("--outbox", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--device", default=os.environ.get("MDT_STEM_WORKER_DEVICE", "cpu"))
    p.add_argument(
        "--python",
        default=os.environ.get("MDT_STEM_WORKER_PYTHON")
        or os.environ.get("MDT_VOCAL_WORKER_PYTHON")
        or sys.executable,
    )
    args = p.parse_args()
    args.outbox.mkdir(parents=True, exist_ok=True)
    files = sorted(
        f
        for f in args.inbox.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )
    # Skip already completed
    pending = [f for f in files if not (args.outbox / f"{f.stem}.tar").is_file()]
    print(
        f"stem farm: {len(pending)}/{len(files)} pending workers={args.workers}",
        flush=True,
    )
    if not pending:
        return 0
    ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(_one, audio, args.outbox, args.device, args.python): audio
            for audio in pending
        }
        for fut in as_completed(futs):
            audio = futs[fut]
            try:
                print(fut.result(), flush=True)
                ok += 1
            # A worker future can surface any dependency or subprocess failure.
            # Finish collecting the batch, then return a non-zero aggregate status.
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {audio.stem}: {exc}", file=sys.stderr, flush=True)
    print(f"done ok={ok}/{len(pending)}", flush=True)
    return 0 if ok == len(pending) else 1


if __name__ == "__main__":
    raise SystemExit(main())
