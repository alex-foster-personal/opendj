"""Shared edge sampler test library: an in-memory sources seam, a seeded state.db, a rekordbox DB.

Imported by the catalogue tests here and by the later sampler route and clip tests, so they all
read the same library. Only the sources seam is doubled (a real 120 BPM grid and a deterministic
waveform); state.db, the cue store and the rekordbox master DB are real sqlite files in tmp.
"""

from __future__ import annotations

import math
import sqlite3
import struct
import wave
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import cue_store
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.webui.server import sampler_catalogue
from apps.webui.server.backend import StateBackend
from apps.webui.server.sampler_core import TrackAnalysis
from apps.webui.server.sampler_sources import TrackMeta
from apps.webui.server.sqlite_backend import make_backend

BEAT_MS = 500.0  # 120 BPM grid from 0 ms
DURATION_MS = 240_000
GRID, NO_GRID = "a" * 40, "b" * 40
VENDOR_ID = "900"


class FakeSources:
    """A real 120 BPM grid and a deterministic 0..1 waveform; NO_GRID has no beats."""

    def __init__(self, audio: Path) -> None:
        self.audio = audio
        self.analysis_calls: list[str] = []
        self.stems: dict[str, dict[str, Path]] = {}  # stable_id -> sampler part -> file
        self.crate_members: list[tuple[str, ...]] = []  # each crate's stable ids, crates in library order

    def analysis(self, request: Request, stable_id: str) -> TrackAnalysis:
        """The fake grid and waveform, counting every read."""
        self.analysis_calls.append(stable_id)
        beats = [] if stable_id == NO_GRID else [i * BEAT_MS for i in range(int(DURATION_MS / BEAT_MS))]
        band = [0.5 + 0.5 * math.sin(i / 7) for i in range(4000)]
        return TrackAnalysis(beats_ms=beats, bands={"low": band, "mid": band, "high": band}, duration_ms=DURATION_MS)

    def meta(self, request: Request, backend: StateBackend, stable_ids: Sequence[str]) -> dict[str, TrackMeta]:
        """One listing row per asked track."""
        flags = {"broken": False, "remix": False, "vocals": True, "offline": True}
        return {
            sid: TrackMeta(title=f"Track {sid[:4]}", artist="Artist", key="8A", energy=6.0, flags=flags)
            for sid in stable_ids
        }

    def audio_path(self, request: Request, stable_id: str) -> Path:
        """The one test audio file."""
        return self.audio

    def stem_parts(self, request: Request, stable_id: str) -> tuple[str, ...] | None:
        """The parts registered in ``self.stems`` for the track, None when it has none."""
        parts = self.stems.get(stable_id)
        return None if parts is None else tuple(parts)

    def stem_part_path(self, request: Request, stable_id: str, part: str) -> Path | None:
        """The registered file of one part."""
        return self.stems.get(stable_id, {}).get(part)

    def crates(self, request: Request, backend: StateBackend) -> list[tuple[str, ...]]:
        """The crates registered in ``self.crate_members``, in library order."""
        return list(self.crate_members)


def wav(path: Path, seconds: float = 6.0, rate: int = 44_100) -> Path:
    """A real stereo 16-bit 220 Hz sine WAV whose level rises across the file.

    The rise matters: two regions a whole number of cycles apart would otherwise hold the same
    samples, and a deterministic encoder would then answer the same bytes for both.
    """
    total = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        samples = (int((4000 + 4000 * i / total) * math.sin(2 * math.pi * 220 * i / rate)) for i in range(total))
        w.writeframes(b"".join(struct.pack("<hh", v, v) for v in samples))
    return path


def seed(state: Path, tracks: dict[str, list[tuple[str, int, int]]], colors: dict[str, int] | None = None) -> None:
    """Live tracks with loop hot cues in the own cue store: stable_id -> [(slot, in_ms, out_ms)].

    ``colors`` maps ``"<stable_id>:<slot>"`` to the cue's ColorTableIndex; unlisted cues have none.
    """
    conn = state_db.open_rw(state)
    try:
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, '2026-01-01', '2026-01-01')",
            [(sid, DURATION_MS) for sid in tracks],
        )
        conn.commit()
        with StateWriter(conn, actor="test") as writer, writer.transaction():
            for sid, cues in tracks.items():
                blob = {
                    "v": 1,
                    "generations": {},
                    "reversals": {},
                    "cues": [
                        {
                            "kind": "hot_cue",
                            "slot": slot,
                            "in_ms": a,
                            "out_ms": b,
                            "active_loop": False,
                            "color_table_index": (colors or {}).get(f"{sid}:{slot}"),
                        }
                        for slot, a, b in cues
                    ],
                }
                writer.set_field(sid, cue_store.CUE_FIELD, blob, source="webui", modified_at="2026-01-01T00:00:00Z")
    finally:
        conn.close()


def master(path: Path, cues: list[tuple[int, int, int]], colors: dict[int, int] | None = None) -> Path:
    """A rekordbox master DB holding (Kind, InMsec, OutMsec) cues for VENDOR_ID; ``colors``: cue index -> colour."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, FolderPath VARCHAR(255))")
    conn.execute(
        "CREATE TABLE djmdCue (ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), InMsec INTEGER, InFrame INTEGER, "
        "InMpegFrame INTEGER, InMpegAbs INTEGER, OutMsec INTEGER, OutFrame INTEGER, Kind INTEGER, Color INTEGER, "
        "ColorTableIndex INTEGER, ActiveLoop INTEGER, Comment VARCHAR(255), BeatLoopSize INTEGER, "
        "rb_local_deleted TINYINT(1) DEFAULT 0, created_at DATETIME, updated_at DATETIME)"
    )
    conn.execute("INSERT INTO djmdContent (ID) VALUES (?)", (VENDOR_ID,))
    conn.executemany(
        "INSERT INTO djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ColorTableIndex, ActiveLoop) VALUES (?,?,?,?,?,?,0)",
        [(str(i), VENDOR_ID, a, b, kind, (colors or {}).get(i)) for i, (kind, a, b) in enumerate(cues)],
    )
    conn.commit()
    conn.close()
    return path


def map_vendor(state: Path, sid: str) -> None:
    """Map ``sid`` to the rekordbox content VENDOR_ID."""
    conn = state_db.open_rw(state)
    try:
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)", (sid, VENDOR_ID)
        )
        conn.commit()
    finally:
        conn.close()


def make_app(state: Path, sources: FakeSources) -> FastAPI:
    """An app carrying the sampler's state (backend, state.db, sources); route tests add the router."""
    app = FastAPI()
    app.state.backend = make_backend()
    app.state.state_db_path = state
    app.state.sampler_sources = sources
    return app


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """The sampler test library as a plain generator, so every sampler test module can share it."""
    sampler_catalogue.reset_caches()
    # rekordbox: hot loop A (hidden by the own slot A), hot loop C, one memory loop, one point cue.
    rb_cues = [(1, 50_000, 52_000), (3, 40_010, 42_000), (0, 60_000, 64_000), (2, 70_000, 0)]
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master(tmp_path / "master.db", rb_cues))
    state = tmp_path / "state" / "state.db"
    state.parent.mkdir()
    # A 4-bar loop cue 30 ms late on A, a 1-bar cue on B; the no-grid track has a loop cue too.
    seed(state, {GRID: [("A", 4030, 12000), ("B", 20000, 22010)], NO_GRID: [("A", 1000, 3000)]})
    map_vendor(state, GRID)
    sources = FakeSources(wav(tmp_path / "src.wav"))
    app = make_app(state, sources)
    request = Request({"type": "http", "app": app, "headers": []})
    yield SimpleNamespace(app=app, request=request, state=state, sources=sources, tmp=tmp_path)
    sampler_catalogue.reset_caches()
