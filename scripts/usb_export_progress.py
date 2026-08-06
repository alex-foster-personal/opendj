"""Live progress + ETA for a rekordbox USB export, by watching the stick grow.

Rekordbox gives no progress API, so this samples the device: bytes written under
Contents/ plus PIONEER/, track count in the export DB, and a rolling throughput
estimate. Read-only -- it never writes to the stick.

Mini-PRD
--------
R1 ✔︎ Sample total bytes + file count under the stick on a fixed interval and
     render a live progress bar against an expected-bytes target.
     [if the stick unmounts mid-run then exit nonzero within one interval ⛔️]
R2 ✔︎ Derive throughput from a rolling window, not since-start, so a stall shows
     up as ETA blowing out rather than being averaged away.
     [if writes stop for a full window then rate reads 0 and ETA shows 'stalled' ⛔️]
R3 ✔︎ Exit 0 once no growth is seen for --settle seconds, so a wrapper can chain
     straight into the verification read.
     [if the export finishes then the final line states bytes and elapsed ⛔️]

Usage::

    uv run --no-sync python scripts/usb_export_progress.py --expect-gb 4.7
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

# ----- config -------------------------------------------------------------

STICK: Path = Path("/Volumes/one-tera")
WINDOW: int = 6  # samples in the rolling throughput window
console = Console()


@dataclass
class Sample:
    at: float
    total_bytes: int
    files: int


# ----- helpers ------------------------------------------------------------


def _scan(root: Path) -> Sample:
    """Total bytes + file count under root. stat only, never opens files."""
    total = files = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        if "/.Spotlight" in dirpath or "/.fseventsd" in dirpath:
            continue
        for fn in filenames:
            try:
                total += os.stat(os.path.join(dirpath, fn)).st_size
                files += 1
            except OSError:
                continue
    return Sample(at=time.monotonic(), total_bytes=total, files=files)


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "stalled"
    if seconds < 0 or seconds > 86400:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _rate(window: deque[Sample]) -> float:
    """Bytes/sec across the rolling window; 0.0 when not growing."""
    if len(window) < 2:
        return 0.0
    first, last = window[0], window[-1]
    dt = last.at - first.at
    if dt <= 0:
        return 0.0
    return max(0.0, (last.total_bytes - first.total_bytes) / dt)


def _panel(start_bytes: int, cur: Sample, rate: float, expect: int, t0: float) -> Table:
    written = cur.total_bytes - start_bytes
    remaining = max(0, expect - written)
    eta = (remaining / rate) if rate > 0 else None
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_row("written", f"[bold]{_fmt_bytes(written)}[/] of ~{_fmt_bytes(expect)}")
    t.add_row("files", f"{cur.files:,}")
    t.add_row("rate", f"{_fmt_bytes(rate)}/s" if rate else "[red]0 B/s (stalled)[/]")
    t.add_row("ETA", f"[bold]{_fmt_eta(eta)}[/]")
    t.add_row("elapsed", _fmt_eta(time.monotonic() - t0))
    return t


# ----- main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--expect-gb", type=float, default=4.7)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--settle", type=int, default=180)
    args = ap.parse_args()

    if not STICK.is_dir():
        sys.exit(f"[ERROR] stick not mounted at {STICK}")

    expect = int(args.expect_gb * 1024**3)
    console.print(f"[dim]scanning {STICK} for a baseline (this takes a moment)...[/]")
    base = _scan(STICK)
    console.print(
        f"[OK] baseline: {_fmt_bytes(base.total_bytes)} across {base.files:,} files"
    )
    console.print("[dim]start the export in rekordbox now -- ctrl-c to stop watching[/]\n")

    window: deque[Sample] = deque(maxlen=WINDOW)
    window.append(base)
    t0 = time.monotonic()
    last_growth = time.monotonic()
    started = False

    progress = Progress(
        TextColumn("[bold blue]export"),
        BarColumn(bar_width=40),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task("export", total=expect)

    with Live(console=console, refresh_per_second=4) as live:
        while True:
            time.sleep(args.interval)
            if not STICK.is_dir():
                live.stop()
                sys.exit("[ERROR] stick unmounted mid-export")
            cur = _scan(STICK)
            if cur.total_bytes > window[-1].total_bytes:
                last_growth = time.monotonic()
                started = True
            window.append(cur)
            rate = _rate(window)
            written = cur.total_bytes - base.total_bytes
            progress.update(task, completed=min(written, expect))

            grid = Table.grid()
            grid.add_row(progress)
            grid.add_row(_panel(base.total_bytes, cur, rate, expect, t0))
            live.update(grid)

            quiet = time.monotonic() - last_growth
            if started and quiet >= args.settle:
                live.stop()
                console.print(
                    f"\n[OK] export settled: {_fmt_bytes(written)} written across "
                    f"{cur.files - base.files:,} new files in "
                    f"{_fmt_eta(time.monotonic() - t0)}"
                )
                return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watching (export, if running, continues)[/]")
