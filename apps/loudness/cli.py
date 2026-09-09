"""CLI for apps.loudness: scan one or more audio files for EBU R128 loudness.

    python -m apps.loudness scan TRACK [TRACK ...] [--target-lufs -14] [--json]

Exit codes: 0 all files scanned, 1 any file failed. A failure prints the reason
and does not stop the remaining files, so a batch reports every problem in one
run rather than one per invocation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.loudness.scan import (
    DEFAULT_CEILING_DBTP,
    DEFAULT_TARGET_LUFS,
    LoudnessError,
    scan_file,
)

# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _render_human(path: Path, scan_json: dict[str, object]) -> str:
    clamped = " (clamped by peak ceiling)" if scan_json["gain_clamped"] else ""
    return (
        f"{path.name}\n"
        f"  integrated   {scan_json['integrated_lufs']:>8} LUFS\n"
        f"  range        {scan_json['loudness_range_lu']:>8} LU\n"
        f"  true peak    {scan_json['true_peak_dbtp']:>8} dBTP\n"
        f"  gain         {scan_json['gain_db']:>8} dB{clamped}"
    )


def _scan_to_dict(path: Path, target_lufs: float, ceiling_dbtp: float) -> dict[str, object]:
    scan = scan_file(path)
    gain_db, clamped = scan.gain_db(target_lufs=target_lufs, ceiling_dbtp=ceiling_dbtp)
    return {
        "path": str(path),
        "integrated_lufs": scan.integrated_lufs,
        "loudness_range_lu": scan.loudness_range_lu,
        "true_peak_dbtp": scan.true_peak_dbtp,
        "target_lufs": target_lufs,
        "ceiling_dbtp": ceiling_dbtp,
        "gain_db": round(gain_db, 2),
        "gain_clamped": clamped,
    }


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.loudness")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_cmd = sub.add_parser("scan", help="measure EBU R128 loudness for files")
    scan_cmd.add_argument("tracks", nargs="+", type=Path)
    scan_cmd.add_argument(
        "--target-lufs",
        type=float,
        default=DEFAULT_TARGET_LUFS,
        help=f"loudness target for the derived gain (default {DEFAULT_TARGET_LUFS})",
    )
    scan_cmd.add_argument(
        "--ceiling-dbtp",
        type=float,
        default=DEFAULT_CEILING_DBTP,
        help=f"true-peak ceiling the gain may not breach (default {DEFAULT_CEILING_DBTP})",
    )
    scan_cmd.add_argument("--json", action="store_true", help="emit JSON lines")
    args = parser.parse_args(argv)

    failed = False
    for track in args.tracks:
        try:
            payload = _scan_to_dict(track, args.target_lufs, args.ceiling_dbtp)
        except LoudnessError as exc:
            print(f"[ERROR] {track}: {exc}", file=sys.stderr)
            failed = True
            continue
        if args.json:
            print(json.dumps(payload))
        else:
            print(_render_human(track, payload))
    return 1 if failed else 0
