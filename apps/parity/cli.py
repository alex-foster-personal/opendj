"""`python -m apps.parity score --payload FILE`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.parity.score import render_report, score_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "PARITY-01 per-lane scorer. Quotes a denominator and a date on "
            "every figure. Does not declare any lane matching a calibrated threshold."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    score = sub.add_parser("score", help="score a JSON payload extract")
    score.add_argument(
        "--payload",
        required=True,
        help="library-shaped JSON extract (schema 1, measured_at, tracks)",
    )
    args = parser.parse_args(argv)
    path = Path(args.payload)
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = score_payload(payload)
    text = render_report(report)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0
