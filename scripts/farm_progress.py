#!/usr/bin/env python3
"""JSONL progress stream for the Modal vocal farm, plus its reducer.

ONE stream, two readers. The farm appends a line per state change to
``data/state/farm-logs/<run-id>.jsonl``; the Rich dashboard
(``scripts/farm_dashboard.py``) renders that file, and an agent tails the
same file with ``tail -f``. Neither reader gets a private channel, so what
a human sees on screen and what an agent parses can never disagree.

Why a file and not stdout: the farm's stdout is already a per-track log a
human reads while it runs. Multiplexing machine events into it would force
every reader to filter, and a dashboard repainting over that stdout would
destroy it. A sidecar file also survives the run for post-hoc analysis.

Stage model (a track moves left to right, and may fail at any stage):

    upload -> gpu -> stems -> written

``FarmState`` is the SINGLE definition of how events reduce to stage counts,
throughput and ETA. The dashboard renders it; ``snapshot_text`` prints it.
An agent that wants its own view should import this reducer rather than
re-deriving the arithmetic, so a "bad throughput" call means the same thing
to every reader.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 every emitted line is one complete JSON object with ts/run_id/event.
    [if] a reader tails mid-write [then] it sees whole lines, never a fragment
    [if] two threads emit at once [then] lines never interleave
  ✔︎ ✅ 🎯 the reducer derives in-flight counts from completions alone, so a
    reader that starts late still reports a consistent picture.
    [if] events are replayed from the top [then] final state matches the run
  ✔︎ ✅ 🎯 an unknown event type is a hard error, not a silently ignored line.
    [if] the farm emits a typo'd event [then ⛔️] the reducer raises

-Claude
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA: int = 1
LOG_DIR: str = "farm-logs"  # under data/state/

# Below this the elapsed window is too short to divide by: a burst of events
# landing in the same instant would otherwise report a five-figure rate, which
# reads as a great result rather than as "no measurement yet". Module-level
# rather than a FarmState attribute because a bare annotation inside the
# dataclass becomes a constructor field, ClassVar or not.
MIN_RATE_WINDOW_S: float = 1.0

# A track's stages, in order. The reducer counts completions per stage and
# infers in-flight as "finished the previous stage but not this one".
STAGES: tuple[str, ...] = ("feed_stall", "gpu", "stems", "written")

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_start",   # {tracks, preset, dest, max_containers, source_bytes}
        "feed_stall",  # {stable_id, bytes, s}  feeder stall, NOT wire transfer
        "gpu",         # {stable_id, separate_s, container_s, coverage_pct, regions}
        "stems_queued",  # {stable_id, bytes}   accepted for transfer
        "stems",       # {stable_id, bytes, s, dest}  transfer complete
        "written",     # {stable_id, regions}
        "failed",      # {stable_id, stage, error}
        "run_end",     # {written, failed, wall_s, container_s, usd}
    }
)


def log_path(data_dir: Path, run_id: str) -> Path:
    return data_dir / "state" / LOG_DIR / f"{run_id}.jsonl"


def new_run_id() -> str:
    """Sortable and unique-per-process: a second-resolution stamp plus pid.

    Two farms started in the same second (a staged runner escalating
    10 -> 50 -> full can do exactly that) must not share a log file.
    """
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


class ProgressLog:
    """Append-only JSONL writer. Thread-safe; one line per state change.

    Every write is a single ``write`` of a string ending in a newline, under
    a lock and followed by a flush. That is what lets a concurrent ``tail -f``
    or the dashboard read the file at any instant and see only whole lines.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._handle = path.open("a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        if event not in EVENT_TYPES:
            raise ValueError(
                f"unknown farm event {event!r}; known: {sorted(EVENT_TYPES)}"
            )
        line = json.dumps(
            {"ts": time.time(), "run_id": self.run_id, "schema": SCHEMA,
             "event": event, **fields},
            separators=(",", ":"),
        )
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()

    def __enter__(self) -> "ProgressLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ----- reading -----------------------------------------------------------------

def read_events(path: Path) -> list[dict[str, Any]]:
    """Every complete event in the file. A trailing partial line is dropped.

    A partial last line is normal, not corruption: the writer may be mid-flush.
    Dropping it is safe because the next read picks it up whole.
    """
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # only ever the in-flight tail
    return events


def follow_events(path: Path, poll_s: float = 0.25) -> Iterator[dict[str, Any]]:
    """Yield events as they land, from the top of the file, forever.

    Polls rather than using an OS watch API: the file is local, appended a
    few times per second at most, and a poll keeps this stdlib-only so the
    farm can import this module without adding a dependency.
    """
    offset = 0
    pending = ""
    while True:
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
            pending += chunk
            *lines, pending = pending.split("\n")
            for line in lines:
                line = line.strip()
                if line:
                    yield json.loads(line)
        time.sleep(poll_s)


# ----- reduction ---------------------------------------------------------------

@dataclass
class FarmState:
    """Events reduced to what a reader needs to judge the run.

    Deliberately derived from COMPLETIONS only. An in-flight count is
    "completed the previous stage, has not completed this one", which stays
    correct even if a reader joins late or an event is lost, and never goes
    negative in a way that hides a stall.
    """

    run_id: str = ""
    preset: str = ""
    dest: str = ""
    tracks: int = 0
    max_containers: int = 0
    source_bytes: int = 0

    done: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STAGES})
    bytes_done: dict[str, int] = field(
        default_factory=lambda: {"feed_stall": 0, "stems": 0}
    )
    seconds_in: dict[str, float] = field(
        default_factory=lambda: {"feed_stall": 0.0, "stems": 0.0}
    )
    stems_queued: int = 0
    container_s: float = 0.0
    separate_s: float = 0.0
    coverage_sum: float = 0.0
    failures: list[dict[str, Any]] = field(default_factory=list)
    failed_at: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STAGES})

    started_ts: float = 0.0
    last_ts: float = 0.0
    ended: bool = False
    final: dict[str, Any] = field(default_factory=dict)

    # ----- reduce
    def apply(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind not in EVENT_TYPES:
            raise ValueError(f"unknown farm event {kind!r} in stream")
        self.last_ts = event.get("ts", self.last_ts)
        if kind == "run_start":
            self.run_id = event.get("run_id", "")
            self.preset = event.get("preset", "")
            self.dest = event.get("dest", "")
            self.tracks = event.get("tracks", 0)
            self.max_containers = event.get("max_containers", 0)
            self.source_bytes = event.get("source_bytes", 0)
            self.started_ts = event.get("ts", 0.0)
        elif kind == "feed_stall":
            self.done["feed_stall"] += 1
            self.bytes_done["feed_stall"] += event.get("bytes", 0)
            self.seconds_in["feed_stall"] += event.get("s", 0.0)
        elif kind == "gpu":
            self.done["gpu"] += 1
            self.container_s += event.get("container_s", 0.0)
            self.separate_s += event.get("separate_s", 0.0)
            self.coverage_sum += event.get("coverage_pct", 0.0)
        elif kind == "stems_queued":
            self.stems_queued += 1
        elif kind == "stems":
            self.done["stems"] += 1
            self.bytes_done["stems"] += event.get("bytes", 0)
            self.seconds_in["stems"] += event.get("s", 0.0)
        elif kind == "written":
            self.done["written"] += 1
        elif kind == "failed":
            stage = event.get("stage", "?")
            self.failures.append(
                {
                    "stable_id": event.get("stable_id", "?"),
                    "stage": stage,
                    "error": event.get("error", ""),
                }
            )
            if stage in self.failed_at:
                self.failed_at[stage] += 1
        elif kind == "run_end":
            self.ended = True
            self.final = {k: v for k, v in event.items()
                          if k not in ("ts", "run_id", "schema", "event")}

    # ----- derive
    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def elapsed_s(self) -> float:
        if not self.started_ts:
            return 0.0
        return max(0.0, self.last_ts - self.started_ts)

    @property
    def settled(self) -> int:
        """Tracks that will not move again: published, or failed anywhere."""
        return self.done["written"] + self.failed

    def in_flight(self, stage: str) -> int:
        """How many tracks sit between the previous stage and this one.

        Failures are subtracted PER STAGE, not in bulk. A track that failed at
        stage X completed every stage before X, so charging its failure to the
        earlier stages would understate their in-flight, and charging it to
        later ones leaves a phantom backlog that never drains. That phantom is
        exactly what a reader would misread as a stalled transfer.
        """
        if stage == "feed_stall":
            return max(0, self.tracks - self.done["feed_stall"]
                       - self.failed_at["feed_stall"])
        if stage == "gpu":
            return max(0, self.done["feed_stall"] - self.done["gpu"]
                       - self.failed_at["gpu"])
        if stage == "stems":
            return max(0, self.stems_queued - self.done["stems"]
                       - self.failed_at["stems"])
        return max(0, self.done["gpu"] - self.done["written"]
                   - self.failed_at["stems"] - self.failed_at["written"])

    @property
    def tracks_per_min(self) -> float:
        if self.elapsed_s < MIN_RATE_WINDOW_S or self.settled == 0:
            return 0.0
        return self.settled * 60.0 / self.elapsed_s

    def mb_per_s(self, stage: str) -> float:
        """Transfer rate for a stage, over the time SPENT transferring.

        Wall-clock would understate a pipelined transfer: if a push overlaps
        GPU work, dividing by wall makes a healthy pipe look slow. Dividing
        by summed transfer seconds answers the question actually being asked,
        which is how fast the pipe is when it is being used.
        """
        spent = self.seconds_in.get(stage, 0.0)
        if spent <= 0:
            return 0.0
        return self.bytes_done.get(stage, 0) / 1e6 / spent

    @property
    def eta_s(self) -> float:
        remaining = self.tracks - self.settled
        if remaining <= 0 or self.settled == 0 or self.elapsed_s <= 0:
            return 0.0
        return remaining * (self.elapsed_s / self.settled)

    def usd(self, usd_per_s: float) -> float:
        """GPU-time spend so far. A lower bound on the invoice: Modal also
        bills the scaledown window, which no event here can see."""
        return self.container_s * usd_per_s

    @property
    def binding(self) -> str:
        """Which resource is pacing the run, GPU or TRANSFER.

        GPU seconds are divided by container count because that many run at
        once; local transfer seconds are serial on one uplink. If transfer
        exceeds concurrent GPU wall, the GPUs are waiting on the Mac, which
        is precisely the failure mode the async rework exists to remove.
        """
        if self.done["stems"] == 0:
            return "GPU"
        gpu_wall = self.container_s / max(1, self.max_containers)
        return "TRANSFER" if self.seconds_in["stems"] > gpu_wall else "GPU"


def reduce_events(events: list[dict[str, Any]]) -> FarmState:
    state = FarmState()
    for event in events:
        state.apply(event)
    return state


def _hms(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def snapshot_text(state: FarmState, usd_per_s: float) -> str:
    """One compact block an agent can read without rendering anything.

    This is the same reduction the dashboard draws, so an agent reading this
    and a human watching the bars are looking at identical numbers.
    """
    header = (
        f"run {state.run_id} preset={state.preset} dest={state.dest} "
        f"tracks={state.tracks} elapsed={_hms(state.elapsed_s)}"
        f"{' ENDED' if state.ended else ''}"
    )
    lines = [header]
    for stage in STAGES:
        lines.append(
            f"  {stage:<8} done={state.done[stage]:>4}/{state.tracks:<4} "
            f"in_flight={state.in_flight(stage):>3}"
        )
    lines.append(
        f"  rate     {state.tracks_per_min:.1f} tracks/min  "
        f"upload={state.mb_per_s('upload'):.1f} MB/s  "
        f"stems={state.mb_per_s('stems'):.1f} MB/s  binding={state.binding}"
    )
    lines.append(
        f"  cost     ${state.usd(usd_per_s):.2f} gpu-time  "
        f"eta={_hms(state.eta_s)}  failed={state.failed}"
    )
    for failure in state.failures[-5:]:
        lines.append(
            f"  FAIL     {failure['stable_id']} at {failure['stage']}: "
            f"{failure['error'][:80]}"
        )
    return "\n".join(lines)


def latest_log(data_dir: Path) -> Path | None:
    """Most recently modified run log, or None. Lets a dashboard or an agent
    attach to "the run that is happening" without being told the run id."""
    directory = data_dir / "state" / LOG_DIR
    if not directory.is_dir():
        return None
    logs = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None
