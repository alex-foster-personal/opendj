#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Survey the whole library for beat-grid ground truth, and name the denominator.

THIS SCRIPT EXISTS TO MAKE THE DENOMINATOR HONEST. Every later figure in the
beat-mapping benchmark is a rate, and a rate is meaningless until the base is
stated. The house rule is explicit about this: never quote a match rate against
a base padded with rows that have no resolvable audio. So before a single
analyzer runs, this walks the entire track set and sorts every row into exactly
one bucket, and those bucket counts are what the report divides by.

GROUND TRUTH IS REKORDBOX ITSELF. The KPI is agreement with rekordbox's own
grids, so rekordbox's ANLZ PQTZ beat grid is the reference, not a hand label.
That grid is read through the lane daemon's /anlz endpoint rather than off
disk, for two reasons. The daemon owns the path healing that turns a stale
rekordbox FolderPath into a file that actually exists, and it owns the ANLZ
sibling resolution (.DAT/.EXT/.2EX). Reimplementing either here would be a
second source of truth that silently drifts from the one the app uses.

WHY A FULL SCAN AND NOT A SAMPLE. A sample can tell you a rate but it cannot
tell you a count, and the interesting population here is rare: rekordbox
dynamic grids (tracks whose PQTZ carries more than one tempo) are a small
fraction of a library but they are exactly where a fixed-BPM analyzer flatters
itself. Finding enough of them to split the metrics on requires looking at all
of them. At the measured ~0.4s per ANLZ fetch a full pass is minutes with
modest concurrency, so there is no reason to guess.

CONCURRENCY IS DELIBERATELY MODEST. The daemon is shared with other lane work.
The default worker count trades a slower scan for not starving a neighbour.

The output is per-track and machine readable so the fixture picker downstream
never has to re-derive any of it, and so a later round can diff against it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

# ----- Configuration ------------------------------------------------------

# points=100 is the endpoint's documented minimum. The waveform arrays scale
# with it and we throw them away, so asking for the floor keeps the payload
# roughly 25 KB instead of roughly 1 MB. The beat grid is unaffected by points.
ANLZ_POINTS = 100

# Two grid bpm values are treated as the same tempo when they agree at the
# resolution rekordbox itself stores. PQTZ tempo is an integer of BPM x100, so
# 0.01 is the storage granularity and anything finer would be inventing detail.
BPM_QUANTUM = 0.01


# ----- Data shapes --------------------------------------------------------


@dataclass
class TempoChange:
    """One point where the rekordbox grid switches tempo."""

    at_s: float
    from_bpm: float
    to_bpm: float


@dataclass
class GridSummary:
    """Everything the fixture picker needs about one track's rekordbox grid."""

    stable_id: str
    title: str
    duration_ms: int | None
    rb_bpm: float | None
    beat_count: int
    distinct_bpms: list[float] = field(default_factory=list)
    tempo_changes: list[TempoChange] = field(default_factory=list)
    first_beat_s: float | None = None
    last_beat_s: float | None = None
    downbeat_count: int = 0
    error: str | None = None

    @property
    def is_dynamic(self) -> bool:
        """A dynamic grid carries more than one tempo across its beats."""
        return len(self.distinct_bpms) > 1

    @property
    def has_grid(self) -> bool:
        return self.beat_count > 0 and self.error is None


# ----- HTTP ---------------------------------------------------------------


def _get_json(base: str, path: str, timeout: float = 60.0) -> Any:
    """GET one JSON document, failing loud with the server's own detail."""
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} on {path}: {body}") from exc


def _iter_tracks(base: str, available: str) -> Iterator[dict[str, Any]]:
    """Page the whole track set.

    The available filter is applied by the server AFTER cursor pagination, so a
    page can come back shorter than the limit while next_cursor still advances.
    That means the only correct stop condition is a null cursor, never a short
    page.
    """
    cursor: str | None = None
    while True:
        qs = urllib.parse.urlencode(
            {"limit": 1000, "available": available, **({"cursor": cursor} if cursor else {})}
        )
        page = _get_json(base, f"/api/v1/tracks?{qs}")
        yield from page.get("items", [])
        cursor = page.get("next_cursor")
        if not cursor:
            return


# ----- Grid extraction ----------------------------------------------------


def summarise_grid(track: dict[str, Any], anlz: dict[str, Any]) -> GridSummary:
    """Reduce one ANLZ payload to the grid facts, dropping the waveform."""
    beats = (anlz.get("beatgrid") or {}).get("beats") or []
    summary = GridSummary(
        stable_id=track["stable_id"],
        title=(track.get("title") or "")[:120],
        duration_ms=track.get("duration_ms"),
        rb_bpm=track.get("bpm"),
        beat_count=len(beats),
    )
    if not beats:
        return summary

    summary.first_beat_s = float(beats[0]["t"])
    summary.last_beat_s = float(beats[-1]["t"])
    summary.downbeat_count = sum(1 for b in beats if int(b.get("n", 0)) == 1)

    seen: list[float] = []
    prev: float | None = None
    for beat in beats:
        bpm = round(float(beat["bpm"]), 2)
        if not any(abs(bpm - s) < BPM_QUANTUM for s in seen):
            seen.append(bpm)
        if prev is not None and abs(bpm - prev) >= BPM_QUANTUM:
            summary.tempo_changes.append(
                TempoChange(at_s=float(beat["t"]), from_bpm=prev, to_bpm=bpm)
            )
        prev = bpm
    summary.distinct_bpms = sorted(seen)
    return summary


def _fetch_one(base: str, track: dict[str, Any]) -> GridSummary:
    """Fetch and summarise one track, converting any failure into a bucket."""
    try:
        anlz = _get_json(
            base, f"/api/v1/tracks/{track['stable_id']}/anlz?points={ANLZ_POINTS}"
        )
    except Exception as exc:
        return GridSummary(
            stable_id=track["stable_id"],
            title=(track.get("title") or "")[:120],
            duration_ms=track.get("duration_ms"),
            rb_bpm=track.get("bpm"),
            beat_count=0,
            error=str(exc)[:200],
        )
    return summarise_grid(track, anlz)


# ----- Entry point --------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8685")
    ap.add_argument("--out", required=True, help="per-track survey JSON")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="0 means the whole library")
    args = ap.parse_args()

    started = time.time()
    print(f"[survey] listing tracks from {args.base}", flush=True)
    all_rows = list(_iter_tracks(args.base, "all"))
    available_ids = {t["stable_id"] for t in _iter_tracks(args.base, "true")}
    print(
        f"[survey] {len(all_rows)} rows total, {len(available_ids)} with audio present on disk",
        flush=True,
    )

    pool = [t for t in all_rows if t["stable_id"] in available_ids]
    if args.limit:
        pool = pool[: args.limit]
    print(f"[survey] fetching ANLZ grids for {len(pool)} rows, {args.workers} workers", flush=True)

    results: list[GridSummary] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool_exec:
        for i, summary in enumerate(pool_exec.map(lambda t: _fetch_one(args.base, t), pool), 1):
            results.append(summary)
            if i % 500 == 0:
                rate = i / max(time.time() - started, 1e-9)
                print(f"[survey]   {i}/{len(pool)} at {rate:.1f}/s", flush=True)

    with_grid = [r for r in results if r.has_grid]
    dynamic = [r for r in with_grid if r.is_dynamic]
    errored = [r for r in results if r.error]

    payload = {
        "schema": 1,
        "base": args.base,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - started, 1),
        "denominator": {
            "rows_total": len(all_rows),
            "rows_audio_present": len(available_ids),
            "rows_surveyed": len(pool),
            "rows_with_rbx_grid": len(with_grid),
            "rows_grid_fixed": len(with_grid) - len(dynamic),
            "rows_grid_dynamic": len(dynamic),
            "rows_no_grid": len(results) - len(with_grid) - len(errored),
            "rows_anlz_error": len(errored),
        },
        "tracks": [asdict(r) for r in results],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    d = payload["denominator"]
    print(f"[survey] done in {payload['elapsed_s']}s -> {args.out}", flush=True)
    for k, v in d.items():
        print(f"[survey]   {k:24s} {v}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
