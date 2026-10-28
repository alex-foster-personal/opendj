"""A real library on disk for the energy backfill tests (no mocks).

Shared by the planner, route and MCP tool tests: real audio-shaped files under
a mounted root, real ``tracks`` + ``track_availability`` rows whose stored
paths sit under a home directory, exactly the shape the sources map joins.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from apps.mik import backfill
from apps.mik import backfill_settings as settings
from apps.shared.state import provenance as _prov

SEGMENTS = [(0.0, 60.0, 5), (60.0, 30.0, 8)]


def sid(letter: str) -> str:
    return letter * 40


class Lib:
    """A real library on disk: audio files under a mounted root, rows under a home."""

    def __init__(self, tmp_path: Path, conn: sqlite3.Connection, add_track, data_dir: Path) -> None:
        self.root = tmp_path / "air-library"
        self.ingest = tmp_path / "ingest"
        self.conn = conn
        self.add_track = add_track
        self.library = backfill.Library.at(data_dir)
        self.configure(min_free_bytes=0)

    def configure(self, *, min_free_bytes: int) -> None:
        """Write this library's real settings file (``state/mik-backfill.json``).

        The floor is ALWAYS set here, explicitly: a test about staging must not
        pass or fail on how full the machine running it happens to be. Zero
        means "no floor"; the floor tests set their own number.
        """
        settings.settings_path(self.library.data_dir).write_text(
            json.dumps({"ingest_root": str(self.ingest), "min_free_bytes": min_free_bytes}), encoding="utf-8"
        )

    def track(
        self,
        letter: str,
        rel: str,
        *,
        state: str = "present",
        title: str | None = None,
        artist: str | None = None,
        duration_ms: int | None = None,
        on_disk: bool = True,
        stored: str | None = None,
    ) -> Path:
        """``stored`` overrides the row's recorded path (default: ``rel`` under a home)."""
        path = self.root / rel
        if on_disk:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"audio {letter}".encode())
        stable_id = sid(letter)
        self.add_track(stable_id, title=title or letter, artist=artist or letter, file_path=stored or f"/Users/old/{rel}")
        self.conn.execute("UPDATE tracks SET duration_ms = ? WHERE stable_id = ?", (duration_ms, stable_id))
        self.conn.execute(
            "INSERT INTO track_availability(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, state, str(path) if state == "present" else None, "2026-10-13T00:00:00+00:00"),
        )
        self.conn.commit()
        return path

    def series(
        self, letter: str, source: str = "mik", modified_at: str = "2026-01-01T00:00:00+00:00", start_index: int = 0
    ) -> None:
        """Give a row an energy series of ``source`` (two segments from ``start_index``), analysed at ``modified_at``."""
        for seq, (start, length, value) in enumerate(SEGMENTS, start=start_index):
            self.conn.execute(
                "INSERT INTO track_energy_segments(stable_id, seq, start_ms, length_ms, energy, source, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid(letter), seq, int(start * 1000), int(length * 1000), value, source, modified_at),
            )
        self.conn.commit()

    def energy(self, letter: str, value: int, source: str = "mik") -> None:
        _prov.write_field(
            self.conn,
            stable_id=sid(letter),
            field_name="energy",
            value=value,
            source=source,  # type: ignore[arg-type]
            modified_at="2026-01-01T00:00:00+00:00",
            confidence=0.95,
            actor="test",
        )
        self.conn.commit()
