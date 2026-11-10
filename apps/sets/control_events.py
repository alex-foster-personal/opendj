"""Control-event v2 ingest helpers (SETREC-03/06)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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
