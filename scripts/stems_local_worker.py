#!/usr/bin/env python3
"""Engine job worker: separate N tracks locally, one bundle at a time.

Process behind ``stems.separate`` when CloudSync policy forbids remote
processing (or farm transport is refused). Same stdout protocol and bundle
shape as ``scripts/stems_modal_worker.py``; heavy ML stays in
``scripts/stem_bundle_worker.py`` (PEP 723).

Part-1 engine: htdemucs via stem_bundle_worker (tiers.py LOCAL rung). Torchaudio
Hybrid Demucs is the documented first swap candidate once Air MPS is measured.

Requirements (mini-PRD):
  ✔︎ fresh bundle -> skipped: up to date
  ✔︎ low priority (nice), cancellable, per-session wall budget
  ✔︎ one progress line per completed track with stems_ready

-Claude
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.stems_modal_worker import (  # noqa: E402
    EXIT_BAD_INPUT,
    PROGRESS_CEILING,
    TrackJob,
    _Protocol,
    select_tracks,
)

WORKER_SCRIPT = REPO_ROOT / "scripts" / "stem_bundle_worker.py"
UV_BIN = os.environ.get("MDT_UV_BIN", "uv")
NICE_LEVEL = int(os.environ.get("MDT_LOCAL_STEMS_NICE", "10"))
SESSION_BUDGET_S = float(os.environ.get("MDT_LOCAL_STEMS_SESSION_BUDGET_S", "86400"))
PROGRESS_TRACK_KEY = "stems_ready"
_CANCEL_STATE: dict[str, bool] = {"value": False}


def _on_term(_signum: int, _frame: object) -> None:
    _CANCEL_STATE["value"] = True


def _stems_root(data_dir: Path) -> Path:
    return data_dir / "state" / "stems"


def _bundle_fresh(stable_id: str, root: Path) -> bool:
    from apps.stems.selection import has_bundle

    return has_bundle(stable_id, root)


def _bundle_worker_prefix() -> list[str]:
    """What runs the PEP 723 Demucs worker: the payload's interpreter, or uv.

    In the installed app there is no uv (issue #3421); the launcher names the
    payload interpreter, whose pylib carries demucs and torch through the
    ``vocals`` extra, exactly as local vocals already run.
    """
    from apps.stems.worker_launch import packaged_python

    packaged = packaged_python()
    if packaged is not None:
        return [packaged]
    return [UV_BIN, "run", "--no-sync"]


def _run_one(
    track: TrackJob,
    *,
    out_dir: Path,
    device: str,
    timeout_s: float,
) -> dict[str, Any]:
    cmd = [
        *_bundle_worker_prefix(),
        str(WORKER_SCRIPT),
        "--audio",
        str(track.audio_path),
        "--stable-id",
        track.stable_id,
        "--out-dir",
        str(out_dir),
        "--device",
        device,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"stem worker failed ({proc.returncode}): {tail}")
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("stem worker produced no stdout JSON")
    return json.loads(lines[-1])


def run(
    tracks: list[TrackJob],
    data_dir: Path,
    protocol: _Protocol,
    *,
    device: str = "auto",
    track_timeout_s: float = 3600.0,
) -> int:
    """Separate every track locally, skipping fresh bundles."""
    with contextlib.suppress(OSError):
        os.nice(NICE_LEVEL)

    root = _stems_root(data_dir)
    total = len(tracks)
    protocol.emit(0.0, f"0/{total} local stems (htdemucs, nice={NICE_LEVEL})")

    done = 0
    skipped = 0
    failures: list[str] = []
    session_started = time.perf_counter()

    for _index, track in enumerate(tracks, 1):
        if _CANCEL_STATE["value"]:
            print("[stems-local] cancelled", file=sys.stderr, flush=True)
            return EXIT_BAD_INPUT
        elapsed = time.perf_counter() - session_started
        if elapsed > SESSION_BUDGET_S:
            failures.append(
                f"session budget {SESSION_BUDGET_S:.0f}s exceeded at track {track.stable_id}"
            )
            break

        out_dir = root / track.stable_id
        if _bundle_fresh(track.stable_id, root):
            skipped += 1
            protocol.emit(
                min(PROGRESS_CEILING, (done + skipped + len(failures)) / total),
                f"skipped: up to date ({track.stable_id})",
            )
            continue

        try:
            _run_one(track, out_dir=out_dir, device=device, timeout_s=track_timeout_s)
        except Exception as exc:
            failures.append(f"{track.stable_id}: {exc}")
            protocol.emit(
                min(PROGRESS_CEILING, (done + skipped + len(failures)) / total),
                f"{done}/{total} separated, {len(failures)} failed",
            )
            continue

        done += 1
        protocol.emit(
            min(PROGRESS_CEILING, (done + skipped + len(failures)) / total),
            f"{done}/{total} local stems separated",
            **{PROGRESS_TRACK_KEY: track.stable_id},
        )

    print(
        f"[stems-local] {done}/{total} separated, {skipped} skipped, "
        f"{len(failures)} failed in {time.perf_counter() - session_started:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    if failures:
        print(
            "[stems-local] failures:\n  " + "\n  ".join(failures),
            file=sys.stderr,
            flush=True,
        )
        return EXIT_BAD_INPUT
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/stems_local_worker.py",
        description="Separate named tracks locally for the stems.separate job",
    )
    parser.add_argument("--stable-id", action="append", dest="stable_ids", default=None)
    parser.add_argument(
        "--scope",
        default=None,
        choices=("pending",),
        help="separate every pending track (resolved at run time)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override data dir (default MDT_DATA_DIR)",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("MDT_STEM_WORKER_DEVICE", "auto"),
        choices=("auto", "cpu", "cuda", "mps"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    args = build_parser().parse_args(argv)
    protocol = _Protocol()
    try:
        if args.data_dir is not None:
            data_dir = args.data_dir
        else:
            from apps.shared.platform_paths import DATA_DIR

            data_dir = DATA_DIR
        tracks = select_tracks(args.stable_ids, args.scope, data_dir)
        if not tracks:
            protocol.emit(PROGRESS_CEILING, "0 tracks need local stems")
            return 0
        return run(tracks, data_dir, protocol, device=args.device)
    finally:
        protocol.close()


if __name__ == "__main__":
    raise SystemExit(main())
