"""The engine's SPA mount must own "/" over the legacy JSON placeholder."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from apps.engine_core.app import _drop_root_placeholder


def _placeholder() -> dict[str, str]:
    return {"status": "ok"}


def test_drop_root_placeholder_removes_only_the_exact_root_route() -> None:
    app = FastAPI()
    app.get("/", include_in_schema=False)(_placeholder)
    app.get("/api/v1/tracks")(_placeholder)
    before = len(app.router.routes)
    _drop_root_placeholder(app)
    paths = [r.path for r in app.router.routes if isinstance(r, APIRoute)]
    assert "/" not in paths
    assert "/api/v1/tracks" in paths
    assert len(app.router.routes) == before - 1


def test_drop_root_placeholder_is_a_noop_without_a_root_route() -> None:
    app = FastAPI()
    app.get("/api/v1/tracks")(_placeholder)
    before = len(app.router.routes)
    _drop_root_placeholder(app)
    assert len(app.router.routes) == before
