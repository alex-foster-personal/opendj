"""Deterministic publish/read coordination for the entry-gated peak cache contract.

Production peaks live in ``apps.analysis_waveform.local_waveform`` (strip written
before entry; reads gate on the entry file). This module is test infrastructure
only: it models that two-phase publication on a disposable directory with
threading.Events so handshake tests never sleep-race. Issue #13135.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

PUBLISH_BARRIER_TIMEOUT_S = 60.0


class PeakCachePhase(StrEnum):
    ABSENT = "absent"
    PUBLISHING = "publishing"
    PUBLISHED = "published"


@dataclass
class PeakCacheCoordinator:
    """Entry-gated peak cache on ``root`` with publish barriers for handshake tests."""

    root: Path
    stable_id: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    strip_ready: threading.Event = field(default_factory=threading.Event, repr=False)
    release_publish: threading.Event = field(default_factory=threading.Event, repr=False)
    publish_complete: threading.Event = field(default_factory=threading.Event, repr=False)

    def entry_path(self) -> Path:
        return self.root / f"{self.stable_id}.json"

    def strip_path(self) -> Path:
        return self.root / f"{self.stable_id}.strip.json"

    def phase(self) -> PeakCachePhase:
        with self._lock:
            strip = self.strip_path().is_file()
            entry = self.entry_path().is_file()
        if entry:
            return PeakCachePhase.PUBLISHED
        if strip:
            return PeakCachePhase.PUBLISHING
        return PeakCachePhase.ABSENT

    def read_committed(self) -> dict[str, Any] | None:
        """None unless the entry commit marker is present (matches production gating)."""
        if not self.entry_path().is_file():
            return None
        return json.loads(self.entry_path().read_text(encoding="utf-8"))

    def publish(self, *, strip: dict[str, Any], entry: dict[str, Any]) -> None:
        """Strip first, optional barrier, then entry (production order)."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.strip_path().write_text(json.dumps(strip), encoding="utf-8")
        self.strip_ready.set()
        if not self.release_publish.wait(timeout=PUBLISH_BARRIER_TIMEOUT_S):
            raise TimeoutError("peak cache publish barrier timed out")
        self.entry_path().write_text(json.dumps(entry), encoding="utf-8")
        self.publish_complete.set()

    def reset_barriers(self) -> None:
        """Clear handshake events before another coordinated publish."""
        self.strip_ready.clear()
        self.release_publish.clear()
        self.publish_complete.clear()
