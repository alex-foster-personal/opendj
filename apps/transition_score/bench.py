"""Bench entry point: try one pair end to end and print the ranked table (TRYER-04).

    python -m apps.transition_score.bench --pair OUT_ID IN_ID --data-dir data [-n 6] [--context-bars 4] [--json]

Needs a running engine (profiles come from its ``/tracks/{id}/anlz``, as
``opendj dj`` reads them) and an ``odj-audio`` build (``ODJ_AUDIO_BIN`` or
``apps/audio-engine/target``). Exits 1 with the reason when either is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from apps.transition_score.contract import TryResult
from apps.transition_score.tryer import try_pair

_COLUMNS = (
    "rank",
    "id",
    "style",
    "bars",
    "M",
    "k/n",
    "vetoes",
    "p95 ms",
    "bar lag",
    "LU gap",
    "both-full beats",
    "render ms",
    "score ms",
)


def table(result: TryResult) -> str:
    rows = [_COLUMNS]
    for rank, c in enumerate(result.candidates, start=1):
        m = c.metrics
        rows.append(
            (
                str(rank),
                c.id,
                c.style,
                str(c.bars),
                "unscored" if c.score is None else f"{c.score:.3f}",
                f"{c.measured.k}/{c.measured.n}",
                ",".join(v.name for v in c.vetoes) or "-",
                str(m.get("beat_offset_ms_p95", "?")),
                str(m.get("bar_lag_beats", "?")),
                str(m.get("loudness_gap_lu", "?")),
                str(m.get("bass_both_full_beats", "?")),
                "refused" if c.render_ms is None else f"{c.render_ms:.0f}",
                f"{c.score_ms:.0f}",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    lines = [" | ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in rows]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.transition_score.bench", description=__doc__.splitlines()[0])
    parser.add_argument("--pair", nargs=2, metavar=("OUT_ID", "IN_ID"), required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("-n", type=int, default=6)
    parser.add_argument("--context-bars", type=int, default=4)
    parser.add_argument("--json", action="store_true", help="print the TryResult JSON instead of the table")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        result = try_pair(args.pair[0], args.pair[1], n=args.n, context_bars=args.context_bars, data_dir=args.data_dir)
    except Exception as error:  # noqa: BLE001 - a bench reports why it could not measure, then exits non-zero
        print(f"[ERROR] {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    wall_ms = (time.perf_counter() - started) * 1000.0
    if args.json:
        print(json.dumps({**result.to_json(), "wall_ms": round(wall_ms, 1)}, indent=2))
        return 0
    print(f"{args.pair[0]} -> {args.pair[1]}  scorer {result.scorer_version}  wall {wall_ms:.0f} ms")
    print(table(result))
    for note in result.notes:
        print(f"note: {note}")
    unknown = {k: v for c in result.candidates for k, v in c.unknown.items()}
    for name, why in sorted(unknown.items()):
        print(f"unknown {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
