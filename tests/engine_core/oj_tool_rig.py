"""A real engine on loopback over a seeded library, for OJ typed-tool tests.

Every ``tests/engine_core/test_oj_tools_<domain>.py`` uses this instead of its
own fixture: ``create_app`` over a fresh sqlite state.db holding
:data:`SEED_TRACKS`, served by uvicorn on an ephemeral loopback port, and
:meth:`OjRig.run_tool`, which runs the tool's real ``run`` from ``ALL_TOOLS``
(so an unregistered tool fails the lookup) exactly as the agent loop does once
a call is allowed. No mocks, no model calls::

    from tests.engine_core.oj_tool_rig import oj_rig

    def test_something(tmp_path: Path) -> None:
        with oj_rig(tmp_path) as rig:
            result = rig.run_tool("library_search", {"q": "night"})

Pass ``tracks=`` to seed a different library. A tool failure surfaces as the
:class:`ToolFailure` it raised.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from apps.engine_core.assistant.agent_contract import ToolContext, ToolSpec
from apps.engine_core.assistant.api import ALL_TOOLS
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

SEEDED_AT = "2026-11-13T00:00:00+00:00"


@dataclass(frozen=True)
class SeedTrack:
    """One library track as the seed writes it: a tracks row plus its rekordbox-sourced fields."""

    stable_id: str
    title: str
    artist: str
    bpm: float | None = None
    rating: int | None = None
    tags: tuple[str, ...] = ()
    duration_ms: int = 300_000


#: The default library: two "Night" titles far apart in BPM, two artists, two tags.
SEED_TRACKS: tuple[SeedTrack, ...] = (
    SeedTrack("oj-001", "Night Drive", "Tamsin Quell", bpm=124.0, rating=4, tags=("deep-house",)),
    SeedTrack("oj-002", "Night Shift", "Oxide Lane", bpm=140.0, rating=2, tags=("techno",)),
    SeedTrack("oj-003", "Morning Glass", "Tamsin Quell", bpm=122.0, rating=5, tags=("deep-house", "opener")),
)


@dataclass(frozen=True)
class OjRig:
    """A running engine and the way to call one registered tool against it."""

    base_url: str
    audible: bool = False

    def tool(self, name: str) -> ToolSpec:
        """The registered tool called ``name``; KeyError when ALL_TOOLS has none."""
        return {spec.name: spec for spec in ALL_TOOLS}[name]

    def run_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Run one registered tool's real ``run`` against this engine and return its result."""
        spec = self.tool(name)
        ctx = ToolContext(base_url=self.base_url, audible=self.audible)

        async def _call() -> Any:
            return await spec.run(ctx, args)

        return asyncio.run(_call())


def _field(conn: Any, stable_id: str, name: str, value: Any, source: str) -> None:
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (stable_id, name, json.dumps(value), source, 1.0, SEEDED_AT),
    )


def seed_library(db: Path, tracks: Sequence[SeedTrack]) -> None:
    """Write ``tracks`` into a fresh state.db at ``db``."""
    conn = state_db.open_rw(db, apply_schema=True)
    try:
        for track in tracks:
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, duration_ms, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    track.stable_id,
                    "inferred",
                    track.title,
                    json.dumps([track.artist]),
                    track.duration_ms,
                    SEEDED_AT,
                    SEEDED_AT,
                ),
            )
            if track.bpm is not None:
                _field(conn, track.stable_id, "bpm", track.bpm, "rekordbox")
            if track.rating is not None:
                _field(conn, track.stable_id, "rating", track.rating, "rekordbox")
            if track.tags:
                _field(conn, track.stable_id, "tags", list(track.tags), "manual")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def oj_rig(tmp_path: Path, tracks: Sequence[SeedTrack] = SEED_TRACKS) -> Iterator[OjRig]:
    """Serve the webui app over a state.db seeded with ``tracks`` on loopback for the block's length."""
    db = tmp_path / "state.db"
    seed_library(db, tracks)
    app = create_app(
        backend=SqliteBackend(db),
        hostname="test-host",
        state_db_path=str(db),
        mount_frontend=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the OJ tool rig engine")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield OjRig(base_url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=15.0)
