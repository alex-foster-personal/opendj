"""Pure playlist-edit undo/redo helpers (no I/O, no FastAPI).

Rebuilds a bounded inverse-command stack from ``playlist.edit`` /
``playlist.undo`` / ``playlist.redo`` event payloads. ``PlaylistStore``
caches the result and rebuilds on construct so a daemon restart restores
the same cursor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

HISTORY_LIMIT: int = 50

EditOp = Literal["create", "duplicate", "rename", "memberships", "delete"]
InverseOp = Literal["delete", "create", "rename", "memberships"]


class PlaylistHistoryEmptyError(Exception):
    """Undo/redo requested past the live window."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"nothing_to_{action}")

    @property
    def error_code(self) -> str:
        return f"nothing_to_{self.action}"


@dataclass
class PlaylistSnapshot:
    """Name + membership identity of a playlist at one point in time."""

    playlist_id: str
    name: str
    vendor: str
    vendor_pl_id: str
    items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "name": self.name,
            "vendor": self.vendor,
            "vendor_pl_id": self.vendor_pl_id,
            "items": list(self.items),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaylistSnapshot":
        return cls(
            playlist_id=data["playlist_id"],
            name=data["name"],
            vendor=data["vendor"],
            vendor_pl_id=data["vendor_pl_id"],
            items=list(data.get("items") or []),
        )


@dataclass
class PlaylistEditCommand:
    """One user-visible playlist mutation, with before/after snapshots."""

    command_id: str
    op: EditOp
    playlist_id: str
    ts: str
    before: Optional[PlaylistSnapshot]
    after: Optional[PlaylistSnapshot]

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "op": self.op,
            "playlist_id": self.playlist_id,
            "ts": self.ts,
            "before": None if self.before is None else self.before.to_dict(),
            "after": None if self.after is None else self.after.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaylistEditCommand":
        before_raw = data.get("before")
        after_raw = data.get("after")
        return cls(
            command_id=data["command_id"],
            op=data["op"],
            playlist_id=data["playlist_id"],
            ts=data["ts"],
            before=None if before_raw is None else PlaylistSnapshot.from_dict(before_raw),
            after=None if after_raw is None else PlaylistSnapshot.from_dict(after_raw),
        )


def rebuild_stack(
    events: list[dict[str, Any]],
) -> tuple[list[PlaylistEditCommand], int]:
    """Replay history events into a bounded stack plus cursor.

    ``cursor`` is the index of the next-redo slot (so ``stack[cursor-1]`` is
    the next undo). A new ``playlist.edit`` truncates the redo tail. Oldest
    edits fall off the live window once it exceeds ``HISTORY_LIMIT``; event
    rows are never deleted.
    """
    stack: list[PlaylistEditCommand] = []
    cursor = 0
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        if kind == "playlist.edit":
            command = PlaylistEditCommand.from_dict(payload)
            stack = stack[:cursor]
            stack.append(command)
            if len(stack) > HISTORY_LIMIT:
                stack = stack[-HISTORY_LIMIT:]
            cursor = len(stack)
        elif kind == "playlist.undo" and cursor > 0:
            cursor -= 1
        elif kind == "playlist.redo" and cursor < len(stack):
            cursor += 1
    return stack, cursor


def invert(command: PlaylistEditCommand) -> tuple[InverseOp, Optional[PlaylistSnapshot]]:
    """Return the inverse op and the snapshot that op should apply."""
    if command.op in ("create", "duplicate"):
        return "delete", command.after
    if command.op == "delete":
        return "create", command.before
    if command.op == "rename":
        return "rename", command.before
    if command.op == "memberships":
        return "memberships", command.before
    raise ValueError(f"unknown playlist edit op: {command.op!r}")


def label_for(command: PlaylistEditCommand) -> str:
    """Short ASCII label for the history panel."""
    if command.op == "create":
        name = command.after.name if command.after is not None else ""
        return f"Create '{name}'"
    if command.op == "duplicate":
        name = command.after.name if command.after is not None else ""
        return f"Duplicate '{name}'"
    if command.op == "rename":
        old = command.before.name if command.before is not None else ""
        new = command.after.name if command.after is not None else ""
        return f"Rename '{old}' to '{new}'"
    if command.op == "memberships":
        snap = command.after if command.after is not None else command.before
        name = snap.name if snap is not None else ""
        return f"Edit tracks in '{name}'"
    if command.op == "delete":
        name = command.before.name if command.before is not None else ""
        return f"Delete '{name}'"
    raise ValueError(f"unknown playlist edit op: {command.op!r}")


def snapshots_match(
    live: Optional[PlaylistSnapshot],
    expected: Optional[PlaylistSnapshot],
) -> bool:
    """True when live name + items + existence match ``expected``.

    ``None`` means the playlist is tombstoned / missing. ``updated_at`` is
    ignored; ETag is derived from it and is not the snapshot truth.
    """
    if live is None and expected is None:
        return True
    if live is None or expected is None:
        return False
    return live.name == expected.name and list(live.items) == list(expected.items)


__all__ = [
    "HISTORY_LIMIT",
    "EditOp",
    "InverseOp",
    "PlaylistEditCommand",
    "PlaylistHistoryEmptyError",
    "PlaylistSnapshot",
    "invert",
    "label_for",
    "rebuild_stack",
    "snapshots_match",
]
