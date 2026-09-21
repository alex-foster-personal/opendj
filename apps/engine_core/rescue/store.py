"""Adapter over ``apps.rescue.ring`` for RESCUE-04 list/restore HTTP and CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.rescue.ring import RING_SIZE, append_snapshot, read_index

SNAPSHOT_ID_PREFIX = "slot-"


@dataclass(frozen=True)
class RescueRingEntry:
    id: str
    captured_at_ms: int
    payload: dict[str, Any]


def snapshot_id_for_slot(slot: int) -> str:
    return f"{SNAPSHOT_ID_PREFIX}{slot}"


def slot_from_snapshot_id(snapshot_id: str) -> int | None:
    if not snapshot_id.startswith(SNAPSHOT_ID_PREFIX):
        return None
    try:
        slot = int(snapshot_id[len(SNAPSHOT_ID_PREFIX) :])
    except ValueError:
        return None
    if not 0 <= slot < RING_SIZE:
        return None
    return slot


def _read_slot_payload(data_dir: Path, slot: int) -> dict[str, Any] | None:
    path = data_dir / "state" / "rescue" / f"ring-{slot}.json"
    if not path.is_file():
        return None
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


class RescueStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def list_entries(
        self,
        *,
        now_ms: int,  # noqa: ARG002 - retained for the caller's keyword contract
    ) -> list[RescueRingEntry]:
        index = read_index(self._data_dir)
        rows: list[RescueRingEntry] = []
        for offset in range(RING_SIZE):
            slot = (index.newest_slot - offset) % RING_SIZE
            meta = index.slots[slot]
            if meta.bytes == 0:
                continue
            payload = _read_slot_payload(self._data_dir, slot)
            if payload is None:
                continue
            captured_at_ms = int(payload.get("captured_at_ms", meta.captured_at_ms))
            rows.append(
                RescueRingEntry(
                    id=snapshot_id_for_slot(slot),
                    captured_at_ms=captured_at_ms,
                    payload=payload,
                )
            )
        return rows

    def list_metadata(self, *, now_ms: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in self.list_entries(now_ms=now_ms):
            payload = entry.payload
            deck_count = 0
            decks = payload.get("decks")
            if isinstance(decks, dict):
                for deck_id in ("1", "2", "3", "4"):
                    deck = decks.get(deck_id) or decks.get(int(deck_id))
                    if isinstance(deck, dict):
                        stable_id = deck.get("stable_id")
                        if isinstance(stable_id, str) and stable_id:
                            deck_count += 1
            rows.append(
                {
                    "id": entry.id,
                    "captured_at_ms": entry.captured_at_ms,
                    "age_ms": max(0, now_ms - entry.captured_at_ms),
                    "deck_count_loaded": deck_count,
                    "playlist_id": payload.get("playlist_id")
                    if isinstance(payload.get("playlist_id"), (str, type(None)))
                    else None,
                }
            )
        return rows

    def get_entry(self, snapshot_id: str | None, *, now_ms: int) -> RescueRingEntry | None:
        entries = self.list_entries(now_ms=now_ms)
        if not entries:
            return None
        if snapshot_id is None:
            return entries[0]
        for entry in entries:
            if entry.id == snapshot_id:
                return entry
        return None

    def append(self, *, captured_at_ms: int, payload: dict[str, Any]) -> RescueRingEntry:
        if not isinstance(payload, dict):
            raise ValueError("rescue snapshot payload must be a JSON object")
        body = dict(payload)
        body["captured_at_ms"] = captured_at_ms
        if body.get("reason") not in {"periodic", "transport", "load"}:
            body["reason"] = "periodic"
        if body.get("app_posture") != "gig":
            body["app_posture"] = "gig"
        if body.get("schema") != 1:
            body["schema"] = 1
        result = append_snapshot(self._data_dir, body)
        slot = int(result["slot"])
        return RescueRingEntry(
            id=snapshot_id_for_slot(slot),
            captured_at_ms=captured_at_ms,
            payload=body,
        )
