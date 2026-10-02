"""Persistent voice-daemon settings.

Tiny SQLite key/value store for state that must survive a daemon
restart: ``mute_until`` (so a 30-minute mute doesn't vanish if the
process crashes), preferred backends, the last-trained wake-word
model path.

The default store is ``<data dir>/voice/settings.sqlite``, resolved at
construction from ``apps.shared.platform_paths.DATA_DIR``. In a checkout
that is ``<repo>/data/voice/settings.sqlite`` exactly as before; in the
packaged app it is the app's data dir, never the signed payload
(INSTALL-30). The pre-INSTALL-30 default, ``<source root>/data/voice/
settings.sqlite``, is read ONCE as a fallback: when the data-dir store does
not exist yet and the old file does, its rows are copied across through a
read-only, immutable connection, so the old location is never written.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.shared import platform_paths

log = logging.getLogger(__name__)

#: The engine's source root (``payload/app`` in the installed app). Only ever
#: READ, as the location the store used before INSTALL-30.
SOURCE_ROOT: Path = Path(__file__).resolve().parents[2]
SETTINGS_FILENAME: str = "settings.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def voice_data_dir() -> Path:
    """``<data dir>/voice``, read at call time: every voice file lives here."""
    return Path(platform_paths.DATA_DIR) / "voice"


def default_db_path() -> Path:
    """The store's default home, under the data dir."""
    return voice_data_dir() / SETTINGS_FILENAME


def legacy_db_path() -> Path:
    """Where the store lived before INSTALL-30: under the source tree."""
    return SOURCE_ROOT / "data" / "voice" / SETTINGS_FILENAME


def _adopt_legacy(target: Path, legacy: Path) -> bool:
    """Copy ``legacy`` into a not-yet-existing ``target``; never write legacy.

    Returns True when rows were adopted. A legacy file that cannot be read is
    logged and skipped: these are mute and debounce timestamps, and losing
    them must not stop the voice probe from answering.
    """
    if target.exists() or not legacy.is_file():
        return False
    try:
        if legacy.resolve() == target.resolve():
            return False
    except OSError:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # immutable=1: sqlite takes no lock and creates no journal beside the
    # source, so reading the old copy cannot add a file to the signed bundle.
    source = sqlite3.connect(f"{legacy.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        log.warning("voice settings: legacy store %s not adopted: %s", legacy, exc)
        return False
    finally:
        source.close()
    log.info("voice settings: adopted legacy store %s into %s", legacy, target)
    return True


@dataclass
class SettingsStore:
    """Thin key/value wrapper on SQLite."""

    path: Path = field(default_factory=default_db_path)

    def __post_init__(self) -> None:
        if self.path == default_db_path():
            _adopt_legacy(self.path, legacy_db_path())
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
