"""The live-engine fixtures, outside conftest.py on purpose (BigRfc-1).

These fixtures build the production app through create_app, which imports
every router. A conftest that imports create_app puts every test beneath
it in the reach of every server module, so the PR-head selector
(scripts/ci_test_selection_rules.py, "conftest reach") used to select all
of tests/opendj_cli for any change under apps/webui/server. Here the edge
is explicit: a test module that wants the live engine imports the fixture
by name,

    from tests.opendj_cli.engine_fixtures import engine_fixture  # noqa: F401  (pytest fixture)

Each fixture is registered under its pytest name= and defined as
<name>_fixture, so the import does not shadow the test's engine parameter
(ruff F811).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest
import uvicorn
from fastapi import Depends, Request

from apps.engine_core.app import ENGINE_VERSION, HEALTH_PATH, EngineHealthOut, _drop_or_raise
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, StateBackend, Track
from apps.webui.server.deps import get_read_state
from apps.webui.server.routes.health import health as legacy_health
from tests.opendj_cli.conftest import Engine
from tests.waits import start_uvicorn_in_thread

__all__ = ["_add_test_health_route", "big_library_engine_fixture", "engine_fixture"]


def _add_test_health_route(app: Any, boot_id: str) -> None:
    """Expose engine-style health so lock boot_id verification can pass."""
    _drop_or_raise(app, HEALTH_PATH, "legacy health route")
    app.state.engine_boot_id = boot_id
    app.state.contract_rev = "test-contract-rev"

    @app.get(
        HEALTH_PATH,
        response_model=EngineHealthOut,
        tags=["health"],
        name="engine_health",
    )
    async def engine_health(
        request: Request,
        backend: Annotated[StateBackend, Depends(get_read_state)],
    ) -> EngineHealthOut:
        # #5494 made routes.health.health `async def`; this fixture mirrors
        # apps/engine_core/app.py's own engine_health wrapper (fixed in
        # e8202e746) and had the identical bug: calling the now-async
        # legacy handler without awaiting it returns a coroutine, and
        # `.model_dump()` on that raises AttributeError -- which the CLI
        # then reports as a dead/erroring engine rather than real health
        # data ((private path), test_cli_*).
        base = await legacy_health(request, backend)
        return EngineHealthOut(
            **base.model_dump(),
            contract_rev=app.state.contract_rev,
            engine_version=ENGINE_VERSION,
            boot_id=app.state.engine_boot_id,
        )


def _run_engine(tmp_path: Path, backend: InMemoryBackend) -> Iterator[Engine]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=backend,
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors",
        client_event_log_dir=tmp_path / "client-events",
    )
    _add_test_health_route(app, "test-boot")
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="the engine", sockets=[listener]
    )
    lock_path = tmp_path / ".engine.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": port,
                "boot_id": "test-boot",
            }
        ),
        encoding="utf-8",
    )
    engine = Engine(base_url=f"http://127.0.0.1:{port}", port=port, lock_path=lock_path, app=app)
    try:
        yield engine
    finally:
        try:
            engine.stop_clients()
        finally:
            # Graceful first, then insist. A test that WEDGES the page leaves the
            # engine still awaiting a future no page will ever complete, and
            # uvicorn's graceful shutdown waits out that connection: measured 10s
            # of teardown per wedged test, four of them, 40s of a 75s suite spent
            # waiting for a server everyone had finished with. Nothing here asserts
            # anything about graceful shutdown, so there is nothing to lose by
            # stopping asking nicely after half a second.
            server.should_exit = True
            thread.join(timeout=0.5)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=10)


def _seed_large_library(backend: InMemoryBackend, count: int) -> None:
    """``count`` tracks shaped like a real library row (issue #2878 fixture)."""
    created = "2026-04-17T10:00:00.000000Z"
    for i in range(count):
        backend.seed_track(
            Track(
                stable_id=f"trk-{i:06d}",
                title=f"Track Title Number {i:06d} (Extended Mix)",
                artist=f"Artist Collective {i % 733:04d}",
                album=f"Album {i % 211:04d}",
                duration_ms=180_000 + (i % 600) * 1000,
                bpm=float(90 + (i % 60)),
                key=f"{(i % 12) + 1}A",
                rating=(i % 5) + 1,
                tags=["house", "peak-time", f"tag-{i % 40:03d}"],
                notes=f"Imported from rekordbox library export batch {i % 17}",
                created_at=created,
                updated_at=created,
            )
        )


@pytest.fixture(name="engine")
def engine_fixture(tmp_path: Path) -> Iterator[Engine]:
    yield from _run_engine(tmp_path, InMemoryBackend())


@pytest.fixture(name="big_library_engine")
def big_library_engine_fixture(tmp_path: Path) -> Iterator[Engine]:
    """A real engine over a library the size issue #2878 measured (8418 tracks)."""
    backend = InMemoryBackend()
    _seed_large_library(backend, 8500)
    yield from _run_engine(tmp_path, backend)
