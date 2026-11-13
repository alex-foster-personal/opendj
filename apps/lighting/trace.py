"""Record and replay a lighting feed trace as NDJSON (LIGHT-08, issue #9865).

A trace stores the audio engine's own ``state`` lines, unchanged, each with the
monotonic ``received_ns`` the feed stamped on it: exactly the two arguments
:func:`apps.lighting.engine_feed.parse_state_line` takes. One NDJSON line is
``{"received_ns": <int>, "line": <engine state line>}``. The writer refuses a
line ``parse_state_line`` would refuse, so a trace can never hold an invented
sample shape, and a replayed entry parses back into the same ``FeedBatch``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from apps.lighting.engine_feed import EngineFeedError, parse_state_line


@dataclass(frozen=True)
class TraceEntry:
    """One recorded engine ``state`` line and the monotonic time the feed received it."""

    received_ns: int
    line: dict[str, Any]


def _dumps(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class TraceWriter:
    """Append one engine ``state`` line per NDJSON line with a monotonic ``received_ns``."""

    def __init__(self, fh: IO[str], clock: Callable[[], int] = time.monotonic_ns) -> None:
        self._fh = fh
        self._clock = clock
        self._last_t: int | None = None

    def write(self, line: Mapping[str, Any], received_ns: int | None = None) -> TraceEntry:
        """Record one engine state line; raise if it is not one ``parse_state_line`` accepts."""
        t = self._clock() if received_ns is None else int(received_ns)
        if line.get("type") != "state":
            raise EngineFeedError(f"a trace records engine state lines, got type {line.get('type')!r}")
        parse_state_line(line, t, {}, {})
        if self._last_t is not None and t < self._last_t:
            raise ValueError(f"received_ns went backwards: {t} < {self._last_t}")
        self._last_t = t
        entry = TraceEntry(received_ns=t, line=dict(line))
        self._fh.write(_dumps({"received_ns": t, "line": entry.line}) + "\n")
        return entry


class TraceReader:
    """Read an NDJSON trace and replay its engine lines with scaled timing."""

    def __init__(
        self,
        path: str | Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._path = Path(path)
        self._sleep = sleep
        self._clock = clock

    def read(self) -> list[TraceEntry]:
        """Parse every line; raise ValueError or TypeError naming the bad line number."""
        out: list[TraceEntry] = []
        with self._path.open(encoding="utf-8") as fh:
            for lineno, text in enumerate(fh, start=1):
                if not text.strip():
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{self._path.name}: line {lineno}: invalid JSON: {exc.msg}") from exc
                if not isinstance(obj, dict):
                    raise TypeError(f"{self._path.name}: line {lineno}: not a JSON object")
                received_ns, line = obj.get("received_ns"), obj.get("line")
                if not isinstance(received_ns, int) or not isinstance(line, dict):
                    raise TypeError(f"{self._path.name}: line {lineno}: want received_ns (int) and line (object)")
                out.append(TraceEntry(received_ns=received_ns, line=line))
        return out

    def replay(self, speed: float = 1.0) -> Iterator[TraceEntry]:
        """Yield entries with original spacing divided by ``speed`` (0 = no sleep)."""
        if speed < 0:
            raise ValueError("speed must be >= 0")
        start = self._clock()
        first_t: int | None = None
        for entry in self.read():
            if first_t is None:
                first_t = entry.received_ns
            elif speed > 0:
                due_ns = start + (entry.received_ns - first_t) / speed
                wait = (due_ns - self._clock()) / 1e9
                if wait > 0:
                    self._sleep(wait)
            yield entry
