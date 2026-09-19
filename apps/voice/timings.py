"""Per-stage latency tracker.

Used by the daemon main loop to record wake/vad/stt/grammar/dispatch
timings per utterance and append them to ``data/voice/timings.jsonl``
for post-set analysis (CONTEXT success criterion 7).
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Timings:
    """Accumulate per-stage durations and a total."""

    stages: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)
    _open: dict[str, float] = field(default_factory=dict, repr=False)

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            # If this stage already has a value (e.g. retries), accumulate.
            self.stages[stage] = self.stages.get(stage, 0.0) + dur_ms

    def record(self, stage: str, ms: float) -> None:
        self.stages[stage] = self.stages.get(stage, 0.0) + ms

    def total_ms(self) -> float:
        return sum(self.stages.values())

    def to_dict(self) -> dict[str, float]:
        out = {k: round(v, 2) for k, v in self.stages.items()}
        out["total_ms"] = round(self.total_ms(), 2)
        return out


def append_jsonl(path: Path, entry: dict) -> None:
    """Append a single JSON line to ``path`` (creating parent dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True))
        fh.write("\n")


def budget_warning(total_ms: float, budget_ms: float = 1_800.0) -> str | None:
    """Return a warning string if ``total_ms`` exceeds the read-only budget."""
    if total_ms > budget_ms:
        return (
            f"[voice] latency {total_ms:.0f} ms exceeded budget "
            f"{budget_ms:.0f} ms (read-only intent); check warm-daemon state"
        )
    return None


def default_log_path(env: dict[str, str] | None = None) -> Path:
    """Resolve the default timings log under ``data/voice/timings.jsonl``."""
    env = env if env is not None else dict(os.environ)
    override = env.get("VOICE_TIMINGS_LOG")
    if override:
        return Path(override)
    # Assumes repo layout: apps/voice/timings.py -> parents[2] is the root.
    root = Path(__file__).resolve().parents[2]
    return root / "data" / "voice" / "timings.jsonl"
