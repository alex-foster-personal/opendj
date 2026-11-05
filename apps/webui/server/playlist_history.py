"""Pure playlist-edit undo/redo helpers (no I/O, no FastAPI).

Rebuilds a bounded inverse-command stack from ``playlist.edit`` /
``playlist.undo`` / ``playlist.redo`` event payloads. ``PlaylistStore``
caches the result and rebuilds on construct so a daemon restart restores
the same cursor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

HISTORY_LIMIT: int = 50

EditOp = Literal["create", "duplicate", "rename", "memberships", "delete", "add_items"]
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
    members: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "playlist_id": self.playlist_id,
            "name": self.name,
            "vendor": self.vendor,
            "vendor_pl_id": self.vendor_pl_id,
            "items": list(self.items),
        }
        if self.members is not None:
            out["members"] = list(self.members)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaylistSnapshot:
        members_raw = data.get("members")
        return cls(
            playlist_id=data["playlist_id"],
            name=data["name"],
            vendor=data["vendor"],
            vendor_pl_id=data["vendor_pl_id"],
            items=list(data.get("items") or []),
            members=None if members_raw is None else list(members_raw),
        )


@dataclass
class PlaylistEditCommand:
    """One user-visible playlist mutation, with before/after snapshots.

    An ``add_items`` command (LIBM-132) records the inserted rows in ``added``
    instead of membership snapshots; its ``after`` is the header only, so
    ``after.items`` is empty and says nothing about the membership.
    """

    command_id: str
    op: EditOp
    playlist_id: str
    ts: str
    before: PlaylistSnapshot | None
    after: PlaylistSnapshot | None
    added: list[dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "command_id": self.command_id,
            "op": self.op,
            "playlist_id": self.playlist_id,
            "ts": self.ts,
            "before": None if self.before is None else self.before.to_dict(),
            "after": None if self.after is None else self.after.to_dict(),
        }
        if self.added is not None:
            out["added"] = [dict(member) for member in self.added]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaylistEditCommand:
        before_raw = data.get("before")
        after_raw = data.get("after")
        added_raw = data.get("added")
        return cls(
            command_id=data["command_id"],
            op=data["op"],
            playlist_id=data["playlist_id"],
            ts=data["ts"],
            before=None if before_raw is None else PlaylistSnapshot.from_dict(before_raw),
            after=None if after_raw is None else PlaylistSnapshot.from_dict(after_raw),
            added=None if added_raw is None else [dict(member) for member in added_raw],
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


_INVERSE_OP: dict[EditOp, InverseOp] = {
    "create": "delete",
    "duplicate": "delete",
    "delete": "create",
    "rename": "rename",
    "memberships": "memberships",
}

_LABEL: dict[EditOp, str] = {
    "create": "Create '{after}'",
    "duplicate": "Duplicate '{after}'",
    "rename": "Rename '{before}' to '{after}'",
    "memberships": "Edit tracks in '{after}'",
    "delete": "Delete '{before}'",
    "add_items": "Add {tracks} to '{after}'",
}

# LIBUX-8626: a membership edit names what it did, so the history list reads
# "Remove 'Song' from 'INBOX'" rather than a generic "Edit tracks in" row.
_MEMBERSHIP_LABEL: dict[str, str] = {
    "add": "Add {tracks} to '{after}'",
    "remove": "Remove {tracks} from '{after}'",
    "reorder": "Reorder tracks in '{after}'",
    "mixed": "Edit tracks in '{after}'",
}

AFFECTED_TRACKS_LIMIT: int = 100
"""Most stable_ids one history entry reports for navigation."""


def _snap_name(snap: PlaylistSnapshot | None) -> str:
    return "" if snap is None else snap.name


def _multiset_minus(left: list[str], right: list[str]) -> list[str]:
    """Items of ``left`` not matched one-for-one in ``right`` (order kept)."""
    remaining: dict[str, int] = {}
    for item in right:
        remaining[item] = remaining.get(item, 0) + 1
    out: list[str] = []
    for item in left:
        if remaining.get(item, 0) > 0:
            remaining[item] -= 1
            continue
        out.append(item)
    return out


def _membership_delta(command: PlaylistEditCommand) -> tuple[list[str], list[str]]:
    """(added, removed) stable_ids of a ``memberships`` edit."""
    before = [] if command.before is None else list(command.before.items)
    after = [] if command.after is None else list(command.after.items)
    return _multiset_minus(after, before), _multiset_minus(before, after)


def affected_stable_ids(command: PlaylistEditCommand) -> list[str]:
    """Tracks an edit added or removed, unique, capped for the wire.

    Header-only ops (create, rename, delete, duplicate) and pure reorders
    report none: their navigation target is the playlist itself.
    """
    if command.op == "add_items":
        ids = [member["stable_id"] for member in command.added or []]
    elif command.op == "memberships":
        added, removed = _membership_delta(command)
        ids = removed + added
    else:
        ids = []
    return list(dict.fromkeys(ids))[:AFFECTED_TRACKS_LIMIT]


def _tracks_phrase(ids: list[str], titles: Mapping[str, str]) -> str:
    if len(ids) == 1 and titles.get(ids[0]):
        return f"'{titles[ids[0]]}'"
    return f"{len(ids)} track" if len(ids) == 1 else f"{len(ids)} tracks"


def invert(command: PlaylistEditCommand) -> tuple[InverseOp, PlaylistSnapshot | None]:
    """Return the inverse op and the snapshot that op should apply."""
    try:
        inverse = _INVERSE_OP[command.op]
    except KeyError as exc:
        raise ValueError(f"unknown playlist edit op: {command.op!r}") from exc
    snap = command.after if inverse == "delete" else command.before
    return inverse, snap


def label_for(
    command: PlaylistEditCommand,
    titles: Mapping[str, str] | None = None,
) -> str:
    """Short label for the history panel.

    ``titles`` maps stable_id -> track title; a one-track edit names the
    track when its title is known, otherwise it counts tracks.
    """
    try:
        template = _LABEL[command.op]
    except KeyError as exc:
        raise ValueError(f"unknown playlist edit op: {command.op!r}") from exc
    known = titles or {}
    after = command.after if command.after is not None else command.before
    tracks = [member["stable_id"] for member in command.added or []]
    if command.op == "memberships":
        added, removed = _membership_delta(command)
        if added and not removed:
            template, tracks = _MEMBERSHIP_LABEL["add"], added
        elif removed and not added:
            template, tracks = _MEMBERSHIP_LABEL["remove"], removed
        elif not added and not removed:
            template = _MEMBERSHIP_LABEL["reorder"]
        else:
            template = _MEMBERSHIP_LABEL["mixed"]
    return template.format(
        before=_snap_name(command.before),
        after=_snap_name(after),
        tracks=_tracks_phrase(tracks, known),
    )


def snapshots_match(
    live: PlaylistSnapshot | None,
    expected: PlaylistSnapshot | None,
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
    "AFFECTED_TRACKS_LIMIT",
    "HISTORY_LIMIT",
    "EditOp",
    "InverseOp",
    "PlaylistEditCommand",
    "PlaylistHistoryEmptyError",
    "PlaylistSnapshot",
    "affected_stable_ids",
    "invert",
    "label_for",
    "rebuild_stack",
    "snapshots_match",
]
