#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13"]
# ///
"""Live Rich dashboard for the Modal vocal farm, rendered from its JSONL stream.

Reads ``data/state/farm-logs/<run-id>.jsonl`` and nothing else. It holds no
private channel to the farm, so what a human watches here and what an agent
gets from ``tail -f`` on that same file are the same numbers reduced by the
same code (``scripts/farm_progress.py::FarmState``).

The panel layout, ``_render_*`` helpers and Progress column recipe are
vendored patterns rather than an import from an installable package.

WHY BINDING IS THE HEADLINE: the farm exists to keep GPUs busy. The one
decision a reader must be able to make at a glance is whether the run is
paced by the GPUs (healthy) or by a local transfer (the bug this pipeline was
reshaped to remove). That verdict sits in the header, coloured, not buried in
a column of numbers.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 renders from the JSONL file alone, with no farm process attached.
    [if] the log is from a finished run [then] it renders the final state
    [if] the file does not exist yet [then] it waits rather than crashing
  ✔︎ ✅ 🎯 --once prints the agent-readable snapshot and exits 0.
    [if] an agent runs --once mid-run [then] it gets current stage counts
  ✔︎ ✅ 🎯 --demo proves the renderer with synthetic events, no Modal spend.
    [if] --demo runs [then] bars fill, throughput and ETA populate, exit 0

Run:
  uv run scripts/farm_dashboard.py --demo
  uv run scripts/farm_dashboard.py --latest
  uv run scripts/farm_dashboard.py --follow data/state/farm-logs/<id>.jsonl
  uv run scripts/farm_dashboard.py --latest --once     # agents

-Claude
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.farm_progress import (
    STAGES,
    FarmState,
    latest_log,
    read_events,
    reduce_events,
    snapshot_text,
)

# Modal's published L4 rate, mirrored from scripts/modal_vocal_farm.py so the
# dashboard can price a run it is only watching.
L4_USD_PER_S: float = 0.80 / 3600
REFRESH_PER_S: int = 4
POLL_S: float = 0.25
EVENT_LINES: int = 12

STAGE_STYLE: dict[str, str] = {
    "upload": "cyan",
    "gpu": "magenta",
    "stems": "yellow",
    "written": "green",
}
# Kept terse on purpose: the stage bars must survive an 80-column terminal
# without Rich eliding the in-flight count, which is the number that localises
# a stall.
STAGE_BLURB: dict[str, str] = {
    "upload": "-> Modal",
    "gpu": "demucs L4",
    "stems": "-> store",
    "written": "-> cache",
}


def _hms(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


# ----- panels ---------------------------------------------------------------

def _render_header(state: FarmState) -> Panel:
    binding_style = "bold green" if state.binding == "GPU" else "bold red"
    verdict = (
        "GPU-bound (healthy: transfer is keeping up)"
        if state.binding == "GPU"
        else "TRANSFER-bound (GPUs are waiting on the pipe)"
    )
    head = Text.assemble(
        ("run ", "dim"), (state.run_id or "waiting", "bold white"),
        ("  preset ", "dim"), (state.preset or "-", "white"),
        ("  dest ", "dim"), (state.dest or "-", "white"),
        ("  elapsed ", "dim"), (_hms(state.elapsed_s), "white"),
        ("  ", ""), ("ENDED" if state.ended else "LIVE",
                     "dim" if state.ended else "bold green"),
    )
    return Panel(
        Group(head, Text(verdict, style=binding_style)),
        title="[bold]modal vocal farm[/bold]",
        border_style="blue",
        padding=(0, 1),
    )


def _sync_bars(
    progress: Progress, tasks: dict[str, TaskID], state: FarmState
) -> Panel:
    """One bar per stage. Description carries in-flight, which is the number
    that tells you where a stall is: a stage with a fat in-flight and a
    stalled bar is the one holding the run up."""
    for stage in STAGES:
        progress.update(
            tasks[stage],
            completed=state.done[stage],
            total=max(1, state.tracks),
            description=(
                f"[{STAGE_STYLE[stage]}]{stage:<7}[/]"
                f"[dim]{STAGE_BLURB[stage]:<10}[/]"
                f"fly[bold]{state.in_flight(stage):>3}[/]"
            ),
        )
    return Panel(
        progress, title="[bold]stages[/bold]", border_style="green", padding=(0, 1)
    )


def _render_metrics(state: FarmState) -> Panel:
    table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
    table.add_column("k", style="dim", no_wrap=True)
    table.add_column("v", justify="right", no_wrap=True)

    table.add_row("trk/min", f"{state.tracks_per_min:.1f}")
    table.add_row("settled", f"{state.settled}/{state.tracks}")
    table.add_row("eta", _hms(state.eta_s))
    table.add_row(
        "up MB/s",
        f"{state.mb_per_s('upload'):.1f}"
        if state.bytes_done["upload"]
        else "[dim]-[/dim]",
    )
    table.add_row(
        "stem MB/s",
        f"{state.mb_per_s('stems'):.1f}"
        if state.bytes_done["stems"]
        else "[dim]-[/dim]",
    )
    table.add_row("stems GB", f"{state.bytes_done['stems'] / 1e9:.2f}")
    table.add_row("gpu s", f"{state.container_s:,.0f}")
    table.add_row("cost", f"${state.usd(L4_USD_PER_S):.2f}")
    table.add_row(
        "failed",
        f"[red]{state.failed}[/red]" if state.failed else "0",
    )
    return Panel(
        table, title="[bold]rates + cost[/bold]", border_style="cyan", padding=(0, 1)
    )


def _render_events(lines: Deque[str]) -> Panel:
    body = "\n".join(lines) if lines else "[dim]no events yet[/dim]"
    return Panel(
        body, title="[bold]recent[/bold]", border_style="yellow", padding=(0, 1)
    )


def _render_failures(state: FarmState) -> Panel:
    if not state.failures:
        body = "[dim]none[/dim]"
    else:
        body = "\n".join(
            f"[red]{f['stable_id']}[/red] [dim]{f['stage']}[/dim] "
            f"{f['error'][:70]}"
            for f in state.failures[-6:]
        )
    return Panel(
        body,
        title=f"[bold]failures ({state.failed})[/bold]",
        border_style="red" if state.failures else "dim",
        padding=(0, 1),
    )


def _event_line(event: dict[str, Any]) -> str | None:
    """One human line per interesting event. Returns None for the noisy ones
    so the panel stays readable at 10 containers of throughput."""
    kind = event.get("event")
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("ts", time.time())))
    track = str(event.get("stable_id", ""))[:16]
    if kind == "gpu":
        return (
            f"[dim]{stamp}[/dim] [magenta]gpu[/magenta] {track} "
            f"sep={event.get('separate_s', 0):.0f}s "
            f"cov={event.get('coverage_pct', 0):.0f}%"
        )
    if kind == "stems":
        mega = event.get("bytes", 0) / 1e6
        secs = max(1e-6, event.get("s", 0.0))
        return (
            f"[dim]{stamp}[/dim] [yellow]stems[/yellow] {track} "
            f"{mega:.0f}MB @ {mega / secs:.0f}MB/s -> {event.get('dest', '?')}"
        )
    if kind == "written":
        return (
            f"[dim]{stamp}[/dim] [green]written[/green] {track} "
            f"regions={event.get('regions', 0)}"
        )
    if kind == "failed":
        return (
            f"[dim]{stamp}[/dim] [red]FAIL[/red] {track} "
            f"{event.get('stage', '?')}: {str(event.get('error', ''))[:50]}"
        )
    if kind == "run_start":
        return f"[dim]{stamp}[/dim] [blue]run start[/blue] {event.get('tracks')} tracks"
    if kind == "run_end":
        return (
            f"[dim]{stamp}[/dim] [blue]run end[/blue] "
            f"written={event.get('written')} failed={event.get('failed')}"
        )
    return None


def _build_layout() -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body", ratio=1),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(
        Layout(name="stages", size=8),
        Layout(name="events", ratio=1),
    )
    layout["right"].split_column(
        Layout(name="metrics", ratio=3),
        Layout(name="failures", ratio=2),
    )
    return layout


def _paint(
    layout: Layout,
    progress: Progress,
    tasks: dict[str, TaskID],
    state: FarmState,
    lines: Deque[str],
) -> None:
    layout["header"].update(_render_header(state))
    layout["stages"].update(_sync_bars(progress, tasks, state))
    layout["events"].update(_render_events(lines))
    layout["metrics"].update(_render_metrics(state))
    layout["failures"].update(_render_failures(state))


# ----- run modes ---------------------------------------------------------------

def render_live(path: Path, exit_on_end: bool = True) -> int:
    """Tail the stream and repaint until run_end (or Ctrl-C).

    Re-reads and re-reduces the whole file each tick rather than holding an
    incremental cursor. A farm run is thousands of lines, not millions, so the
    cost is trivial, and a full re-reduce means the display cannot drift out of
    agreement with the file the way a long-lived incremental cursor can.
    """
    console = Console()
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        expand=True,
    )
    tasks = {stage: progress.add_task(stage, total=1) for stage in STAGES}
    layout = _build_layout()
    lines: Deque[str] = deque(maxlen=EVENT_LINES)

    seen = 0
    with Live(layout, console=console, refresh_per_second=REFRESH_PER_S,
              screen=False):
        while True:
            events = read_events(path)
            for event in events[seen:]:
                line = _event_line(event)
                if line:
                    lines.append(line)
            seen = len(events)
            state = reduce_events(events)
            _paint(layout, progress, tasks, state, lines)
            if state.ended and exit_on_end:
                break
            time.sleep(POLL_S)
    console.print(snapshot_text(reduce_events(read_events(path)), L4_USD_PER_S))
    return 0


def _demo(console: Console) -> int:
    """Synthetic events into a scratch log, then the real renderer over it.

    This proves the RENDERER and the reducer, not the farm. The events are
    fabricated, deliberately including two failures and a transfer slow enough
    to flip the binding verdict, so the red path is exercised too.
    """
    from scripts.farm_progress import ProgressLog, new_run_id

    scratch = Path(__file__).resolve().parent.parent / ".tmp/farm-demo"
    scratch.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id()
    path = scratch / f"{run_id}.jsonl"
    console.print(f"[dim]demo stream -> {path}[/dim]")

    tracks = 24
    ids = [f"demo{i:03d}" for i in range(tracks)]
    log = ProgressLog(path, run_id)
    log.emit(
        "run_start", tracks=tracks, preset="htdemucs-ov0.1", dest="r2",
        max_containers=10, source_bytes=tracks * 9_000_000,
    )

    import threading

    def produce() -> None:
        written = 0
        failed = 0
        started = time.time()
        for index, stable_id in enumerate(ids):
            time.sleep(0.10)
            log.emit("upload", stable_id=stable_id, bytes=9_000_000,
                     s=random.uniform(0.2, 0.5))
            if index == 7:
                log.emit("failed", stable_id=stable_id, stage="gpu",
                         error="RuntimeError: ffprobe failed on corrupt mp3")
                failed += 1
                continue
            log.emit(
                "gpu", stable_id=stable_id,
                separate_s=round(random.uniform(18, 34), 1),
                container_s=round(random.uniform(25, 45), 1),
                coverage_pct=round(random.uniform(30, 70), 1),
                regions=random.randint(8, 30),
            )
            log.emit("stems_queued", stable_id=stable_id, bytes=74_000_000)
            time.sleep(0.05)
            if index == 15:
                log.emit("failed", stable_id=stable_id, stage="stems",
                         error="ClientError: PutObject 503 SlowDown")
                failed += 1
                continue
            log.emit("stems", stable_id=stable_id, bytes=74_000_000,
                     s=round(random.uniform(0.6, 1.4), 2), dest="r2")
            log.emit("written", stable_id=stable_id,
                     regions=random.randint(8, 30))
            written += 1
        log.emit("run_end", written=written, failed=failed,
                 wall_s=round(time.time() - started, 1),
                 container_s=written * 35.0, usd=round(written * 35.0 * L4_USD_PER_S, 3))
        log.close()

    thread = threading.Thread(target=produce, daemon=True)
    thread.start()
    code = render_live(path)
    thread.join(timeout=5)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/farm_dashboard.py",
        description="Live Rich dashboard over the vocal farm's JSONL stream",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--follow", type=Path, help="path to a run's .jsonl")
    source.add_argument("--latest", action="store_true",
                        help="attach to the most recent run log")
    source.add_argument("--demo", action="store_true",
                        help="render synthetic events; contacts Modal not at all")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--once", action="store_true",
                        help="print the agent-readable snapshot and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    if args.demo:
        return _demo(console)

    if args.follow is not None:
        path = args.follow
    else:
        data_dir = args.data_dir
        if data_dir is None:
            from apps.shared.paths import DATA_DIR

            data_dir = DATA_DIR
        found = latest_log(data_dir)
        if found is None:
            raise SystemExit(
                f"error: no farm logs under {data_dir}/state/farm-logs/. "
                "Run the farm first, or pass --follow."
            )
        path = found

    if args.once:
        if not path.is_file():
            raise SystemExit(f"error: no such run log: {path}")
        print(snapshot_text(reduce_events(read_events(path)), L4_USD_PER_S))
        return 0
    return render_live(path)


if __name__ == "__main__":
    raise SystemExit(main())
