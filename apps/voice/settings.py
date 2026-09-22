"""Persistent voice-daemon settings.

Tiny SQLite key/value store for state that must survive a daemon
restart: ``mute_until`` (so a 30-minute mute doesn't vanish if the
process crashes), preferred backends, the last-trained wake-word
model path.

We write to ``data/voice/settings.sqlite`` by default. If the Phase 5
state layer ships a shared SQLite handle later, we can flip to that
without changing the public API.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "voice" / "settings.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class SettingsStore:
    """Thin key/value wrapper on SQLite."""

    path: Path = _DEFAULT_DB

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ----- key/value API --------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def delete(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def all(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows}

    # ----- typed helpers --------------------------------------------------

    def get_float(self, key: str, default: float | None = None) -> float | None:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def get_int(self, key: str, default: int | None = None) -> int | None:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(float(raw))
        except ValueError:
            return default


def load_mute_until(store: SettingsStore) -> float | None:
    """Return the persisted ``mute_until`` epoch seconds, or ``None``."""
    return store.get_float("mute_until")


def save_mute_until(store: SettingsStore, mute_until: float | None) -> None:
    if mute_until is None:
        store.delete("mute_until")
    else:
        store.set("mute_until", float(mute_until))


def load_last_dispatch_at(store: SettingsStore) -> float | None:
    """Return the persisted last-dispatch epoch seconds, or ``None``."""
    return store.get_float("last_dispatch_at")


def save_last_dispatch_at(
    store: SettingsStore,
    last_dispatch_at: float | None,
) -> None:
    if last_dispatch_at is None:
        store.delete("last_dispatch_at")
    else:
        store.set("last_dispatch_at", float(last_dispatch_at))
