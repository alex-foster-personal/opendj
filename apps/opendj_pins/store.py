"""SQLite-backed comment pin store for mountable services."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COMMENT_STATE_FILTERS = frozenset({"regressed", "harvested"})
_REOPENED_STATUSES = frozenset({"open", "issued", "blocked"})


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def filter_comments_by_state(items: list[dict[str, Any]], state: str | None) -> list[dict[str, Any]]:
    if state is None:
        return items
    if state not in _COMMENT_STATE_FILTERS:
        msg = f"state must be one of {sorted(_COMMENT_STATE_FILTERS)!r}, not {state!r}"
        raise ValueError(msg)
    if state == "harvested":
        return [c for c in items if c.get("status") == "harvested"]
    return [
        c
        for c in items
        if (c.get("status") or "open") in _REOPENED_STATUSES
        and c.get("fixed_in_sha")
        and (c.get("updated_at") or "") > (c.get("fixed_at") or "")
    ]


class PinsStore:
    """One SQLite file holds every pin document as JSON."""

    def __init__(
        self,
        db_path: Path,
        *,
        attachments_dir: Path,
        attachment_url_prefix: str = "/feedback/attachments",
    ) -> None:
        self._db_path = db_path
        self._attachments_dir = attachments_dir
        self._attachment_url_prefix = attachment_url_prefix.rstrip("/")
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        attachments_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def attachments_dir(self) -> Path:
        return self._attachments_dir

    def attachment_url(self, attachment_id: str) -> str:
        return f"{self._attachment_url_prefix}/{attachment_id}"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pins (
                    pin_id TEXT PRIMARY KEY NOT NULL,
                    doc TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def list_comments(self, *, state: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT doc FROM pins ORDER BY json_extract(doc, '$.created_at')").fetchall()
        items = [json.loads(row["doc"]) for row in rows]
        return filter_comments_by_state(items, state)

    def get_comment(self, pin_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT doc FROM pins WHERE pin_id = ?", (pin_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["doc"])

    def put_comment(self, pin_id: str, doc: dict[str, Any]) -> None:
        now = doc.get("updated_at") or doc.get("created_at") or utc_now()
        payload = json.dumps(doc, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pins (pin_id, doc, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(pin_id) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at
                """,
                (pin_id, payload, now),
            )
            conn.commit()

    def delete_comment(self, pin_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM pins WHERE pin_id = ?", (pin_id,))
            conn.commit()
            return cur.rowcount > 0
