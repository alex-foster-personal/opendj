"""PARITY-01 vocal producer: run vocal_region_worker, map stdout to payload shape."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = _REPO / "scripts" / "vocal_region_worker.py"


def _worker_command(audio_path: Path) -> list[str]:
    device = os.environ.get("MDT_VOCAL_WORKER_DEVICE", "auto")
    override_python = os.environ.get("MDT_VOCAL_WORKER_PYTHON")
    if override_python:
        return [
            override_python,
            str(WORKER_SCRIPT),
            "--device",
            device,
            str(audio_path),
        ]
    return [
        "uv",
        "run",
        "--no-sync",
        "--script",
        str(WORKER_SCRIPT),
        "--device",
        device,
        str(audio_path),
    ]


def worker_stdout_to_payload(stdout: dict[str, Any]) -> dict[str, Any]:
    """Map worker JSON to PARITY-01 own_vocal_regions + duration_s."""
    regions_raw = stdout.get("regions")
    if not isinstance(regions_raw, list):
        raise TypeError("worker stdout regions must be a list")
    regions: list[dict[str, float]] = []
    for item in regions_raw:
        if not isinstance(item, dict):
            raise TypeError("each worker region must be a dict")
        start = item.get("start_s")
        end = item.get("end_s")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise TypeError("worker region start_s/end_s must be numbers")
        regions.append({"start_s": float(start), "end_s": float(end)})
    duration_s = stdout.get("duration_s")
    if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool):
        raise TypeError("worker stdout duration_s must be a number")
    return {
        "own_vocal_regions": regions,
        "duration_s": float(duration_s),
    }


def run_vocal(audio_path: Path, *, timeout_s: float = 3600.0) -> dict[str, Any]:
    """Invoke vocal_region_worker and return mapped payload fields."""
    proc = subprocess.run(
        _worker_command(audio_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"vocal_region_worker failed (exit {proc.returncode}) for {audio_path}: "
            f"{proc.stderr.strip()}"
        )
    stdout = json.loads(proc.stdout)
    return worker_stdout_to_payload(stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "PARITY-01 vocal producer adapter: run demucs worker and write "
            "own_vocal_regions JSON for scoring."
        )
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--stable-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload_fields = run_vocal(args.audio)
        out_payload = {"stable_id": args.stable_id, **payload_fields}
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        out_payload = {
            "stable_id": args.stable_id,
            "own_vocal_regions": None,
            "duration_s": None,
            "error": str(exc),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out_payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
