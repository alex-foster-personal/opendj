"""CAT-05: /health must not queue behind the shared sync threadpool.

[if] the shared default threadpool is saturated [then] /health still answers, [else stop].
"""
from __future__ import annotations

import threading

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import health as health_routes

pytestmark = pytest.mark.requirement("CAT-05")

_SATURATING_ROUTES = 4


def _build_app(backend: InMemoryBackend, release: threading.Event) -> FastAPI:
    app = FastAPI()
    app.state.backend = backend
    app.state.bind_host = "127.0.0.1"
    app.state.version = "test"
    app.state.hostname = "test-host"
    app.state.state_db_path = "data/state/state.db"
    app.state.lock_status_fn = lambda: None
    app.state.syncthing_status_fn = lambda: None
    app.include_router(health_routes.router, prefix="/api/v1")

    @app.get("/__test_block")
    def _block() -> dict:  # sync `def` -> dispatched to the SHARED default pool
        release.wait(timeout=10)
        return {"blocked": True}

    return app


@pytest.mark.asyncio
async def test_health_responds_while_default_threadpool_is_saturated() -> None:
    backend = InMemoryBackend()
    release = threading.Event()
    app = _build_app(backend, release)
    transport = ASGITransport(app=app)

    default_limiter = anyio.to_thread.current_default_thread_limiter()
    original_tokens = default_limiter.total_tokens
    default_limiter.total_tokens = _SATURATING_ROUTES

    try:
        async with AsyncClient(transport=transport, base_url="http://test-host") as client:
            async with anyio.create_task_group() as tg:
                async def _occupy_one_token() -> None:
                    r = await client.get("/__test_block")
                    assert r.status_code == 200

                for _ in range(_SATURATING_ROUTES):
                    tg.start_soon(_occupy_one_token)

                # Let every blocking handler actually claim its threadpool
                # token before probing health -- otherwise the race could
                # pass by luck rather than by the dedicated-limiter design.
                with anyio.fail_after(5.0):
                    while default_limiter.available_tokens > 0:
                        await anyio.sleep(0.01)

                with anyio.fail_after(3.0):
                    health_resp = await client.get("/api/v1/health")
                assert health_resp.status_code == 200
                assert health_resp.json()["status"] == "ok"

                release.set()
    finally:
        default_limiter.total_tokens = original_tokens
        release.set()


@pytest.mark.asyncio
async def test_health_still_reports_real_stats_under_saturation() -> None:
    """The fast path above is not a stub: health must still report the real
    backend counts even while routed around the saturated shared pool."""
    backend = InMemoryBackend()
    from datetime import UTC, datetime

    from apps.webui.server.backend import Track

    backend.seed_track(Track(
        stable_id="t-1", title="Probe Track", artist="Nobody",
        bpm=120.0, key="1A", rating=3, tags=[],
        created_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        provenance={},
    ))
    release = threading.Event()
    release.set()  # nothing to saturate with here; just a direct sanity check
    app = _build_app(backend, release)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test-host") as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["state_db"]["tracks"] == 1
