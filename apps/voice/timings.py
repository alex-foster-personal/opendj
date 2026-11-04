"""Per-stage latency tracker.

Used by the daemon main loop to record wake/vad/stt/grammar/dispatch
timings per utterance and append them to ``<data dir>/voice/timings.jsonl``
for post-set analysis (CONTEXT success criterion 7).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from apps.voice.jsonlog import READ_ONLY_LATENCY_BUDGET_MS, append_jsonl
from apps.voice.settings import coalesce_env, voice_data_dir


@dataclass
class Timings:
    """Accumulate per-stage durations and a total."""

    stages: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)

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


def budget_warning(total_ms: float, budget_ms: float = READ_ONLY_LATENCY_BUDGET_MS) -> str | None:
    """Return a warning string if ``total_ms`` exceeds the read-only budget."""
    if total_ms > budget_ms:
        return (
            f"[voice] latency {total_ms:.0f} ms exceeded budget "
            f"{budget_ms:.0f} ms (read-only intent); check warm-daemon state"
        )
    return None


def default_log_path(env: dict[str, str] | None = None) -> Path:
    """Resolve the default timings log, ``<data dir>/voice/timings.jsonl``."""
    env = coalesce_env(env)
    override = env.get("VOICE_TIMINGS_LOG")
    if override:
        return Path(override)
    return voice_data_dir() / "timings.jsonl"


__all__ = ["Timings", "append_jsonl", "budget_warning", "default_log_path"]
