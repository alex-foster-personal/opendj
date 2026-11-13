"""Control-event v2 writers, validation, ingest and coalescing (SETREC-03/04/06)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from jsonschema import Draft202012Validator, ValidationError

from . import control_ids as ids
from .manifest import now_iso

SchemaV2 = Draft202012Validator
_CONTROL_SCHEMA: SchemaV2 | None = None


def _schema() -> SchemaV2:
    global _CONTROL_SCHEMA
    if _CONTROL_SCHEMA is None:
        schema_path = Path(__file__).parent / "schemas" / "control_event.v2.schema.json"
        _CONTROL_SCHEMA = SchemaV2(json.loads(schema_path.read_text(encoding="utf-8")))
    return _CONTROL_SCHEMA


ControlKind = Literal["set", "trigger"]
ControlSource = Literal["user", "controller", "agent", "replay"]

V2_SOURCES: frozenset[str] = frozenset({"user", "controller", "agent", "replay"})
V2_KINDS: frozenset[str] = frozenset({"set", "trigger"})


@dataclass(frozen=True)
class ControlEventV2:
    session_id: str
    seq: int
    sample: int
    sample_rate: int
    wall_clock: str
    control: str
    value: Any
    kind: ControlKind
    source: ControlSource
    deck: int | None = None

    def to_json(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "v": 2,
            "session_id": self.session_id,
            "seq": self.seq,
            "sample": self.sample,
            "sample_rate": self.sample_rate,
            "wall_clock": self.wall_clock,
            "control": self.control,
            "value": self.value,
            "kind": self.kind,
            "source": self.source,
        }
        if self.deck is not None:
            row["deck"] = self.deck
        return row

    def to_dict(self) -> dict[str, Any]:
        return self.to_json()

    def validate(self) -> None:
        _schema().validate(self.to_json())


@dataclass
class RawControlStamp:
    """Engine-side stamp before session seq assignment."""

    sample: int
    sample_rate: int
    control: str
    value: float | bool | str | dict[str, Any]
    kind: ControlKind
    source: ControlSource
    deck: int | None = None
    wall_clock: str | None = None


class ControlSeqError(ValueError):
    """Gap or reorder in the per-session seq stream."""


class ControlEventsCorruptError(ValueError):
    """A persisted ``controls.jsonl`` row is unreadable or fails v2 validation."""


class ControlsJsonl:
    """Append-only ``controls.jsonl`` for one session."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._next_seq = 0

    def append(self, event: ControlEventV2) -> None:
        event.validate()
        if event.seq != self._next_seq:
            raise ControlSeqError(f"expected seq {self._next_seq}, got {event.seq} for control {event.control!r}")
        self._next_seq += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
            fh.flush()

    def read_lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out


def coalesce_continuous(
    stamps: list[RawControlStamp],
    *,
    sample_rate: int,
) -> list[RawControlStamp]:
    """Merge continuous controls inside each 10 ms sample window (spec section 4)."""
    if not stamps:
        return []
    quantum = max(1, int(sample_rate * ids.COALESCE_QUANTUM_MS / 1000.0))
    out: list[RawControlStamp] = []
    i = 0
    while i < len(stamps):
        stamp = stamps[i]
        spec = ids.spec_for_control(stamp.control)
        if spec is None or not spec.continuous:
            out.append(stamp)
            i += 1
            continue
        window_start = (stamp.sample // quantum) * quantum
        window_end = window_start + quantum
        j = i
        bucket: list[RawControlStamp] = []
        while j < len(stamps):
            s = stamps[j]
            if s.control != stamp.control or s.sample >= window_end:
                break
            bucket.append(s)
            j += 1
        if not bucket:
            i += 1
            continue
        if len(bucket) == 1:
            out.append(bucket[0])
        else:
            out.append(bucket[0])
            if bucket[-1].sample != bucket[0].sample or bucket[-1].value != bucket[0].value:
                out.append(bucket[-1])
        i = j
    return out


def assign_session(
    session_id: str,
    stamps: list[RawControlStamp],
    *,
    start_seq: int = 0,
    wall_clock: Callable[[], str] | None = None,
) -> list[ControlEventV2]:
    """Turn engine stamps into gap-free v2 rows."""
    clock = wall_clock or now_iso
    events: list[ControlEventV2] = []
    for n, stamp in enumerate(stamps):
        events.append(
            ControlEventV2(
                session_id=session_id,
                seq=start_seq + n,
                sample=stamp.sample,
                sample_rate=stamp.sample_rate,
                wall_clock=stamp.wall_clock or clock(),
                control=stamp.control,
                value=stamp.value,
                kind=stamp.kind,
                source=stamp.source,
                deck=stamp.deck if stamp.deck is not None else ids.deck_from_control(stamp.control),
            )
        )
    return events


def assert_seq_gap_free(events: list[ControlEventV2]) -> None:
    for i, ev in enumerate(events):
        if ev.seq != i:
            raise ControlSeqError(f"seq gap at index {i}: expected {i}, got {ev.seq}")


def parse_control_event_v2(record: dict[str, Any]) -> ControlEventV2:
    if record.get("v") != 2:
        raise ValueError("control event v must be 2")
    for key in (
        "session_id",
        "seq",
        "sample",
        "sample_rate",
        "wall_clock",
        "control",
        "kind",
        "source",
    ):
        if key not in record:
            raise ValueError(f"control event missing {key}")
    if "value" not in record:
        raise ValueError("control event missing value")
    source = str(record["source"])
    if source not in V2_SOURCES:
        raise ValueError(f"control event source must be one of {sorted(V2_SOURCES)}")
    kind = str(record["kind"])
    if kind not in V2_KINDS:
        raise ValueError(f"control event kind must be one of {sorted(V2_KINDS)}")
    deck_raw = record.get("deck")
    deck: int | None
    if deck_raw is None:
        deck = None
    elif isinstance(deck_raw, int) and 1 <= deck_raw <= 4:
        deck = deck_raw
    else:
        raise ValueError("control event deck must be null or 1..4")
    return ControlEventV2(
        session_id=str(record["session_id"]),
        seq=int(record["seq"]),
        sample=int(record["sample"]),
        sample_rate=int(record["sample_rate"]),
        wall_clock=str(record["wall_clock"]),
        control=str(record["control"]),
        value=record["value"],
        kind=kind,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        deck=deck,
    )


def read_session_control_events(session_dir: Path) -> list[ControlEventV2]:
    """Return a session's persisted v2 control events, schema-validated, in ascending seq order."""
    events: list[ControlEventV2] = []
    try:
        for record in ControlsJsonl(session_dir / "controls.jsonl").read_lines():
            event = parse_control_event_v2(record)
            event.validate()
            events.append(event)
    except (ValueError, ValidationError) as exc:
        raise ControlEventsCorruptError(f"controls.jsonl in {session_dir.name!r} is corrupt: {exc}") from exc
    return sorted(events, key=lambda event: event.seq)


def append_control_jsonl(session_dir: Path, event: ControlEventV2) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "controls.jsonl"
    line = json.dumps(event.to_json(), separators=(",", ":"), ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.write("\n")


def utc_wall_clock(now: datetime | None = None) -> str:
    dt = now or datetime.now(UTC)
    return dt.isoformat().replace("+00:00", "Z")


__all__ = [
    "V2_KINDS",
    "V2_SOURCES",
    "ControlEventV2",
    "ControlEventsCorruptError",
    "ControlSeqError",
    "ControlsJsonl",
    "RawControlStamp",
    "append_control_jsonl",
    "assert_seq_gap_free",
    "assign_session",
    "coalesce_continuous",
    "parse_control_event_v2",
    "read_session_control_events",
    "utc_wall_clock",
]
