"""Reply thread helpers for comment pins."""

from __future__ import annotations

import uuid
from typing import Any


def seed_replies_from_agent_note(item: dict[str, Any], now: str) -> None:
    replies = item.get("replies")
    if isinstance(replies, list) and len(replies) > 0:
        return
    agent_note = item.get("agent_note")
    if not isinstance(agent_note, str) or agent_note.strip() == "":
        return
    item["replies"] = [
        {
            "id": uuid.uuid4().hex[:12],
            "author": "agent",
            "text": agent_note,
            "created_at": item.get("updated_at") or item.get("created_at") or now,
            "agent_kind": item.get("agent_kind"),
        }
    ]


def append_agent_note_to_replies(item: dict[str, Any], note: str, now: str) -> None:
    if not isinstance(note, str) or note.strip() == "":
        return
    seed_replies_from_agent_note(item, now)
    replies = item.setdefault("replies", [])
    last_agent_text: str | None = None
    for reply in reversed(replies):
        if reply.get("author") == "agent":
            last_agent_text = reply.get("text")
            break
    if last_agent_text == note:
        return
    replies.append(
        {
            "id": uuid.uuid4().hex[:12],
            "author": "agent",
            "text": note,
            "created_at": now,
            "agent_kind": item.get("agent_kind"),
        }
    )
