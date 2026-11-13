"""RESCUE-01: rescue readback against partial and corrupt ring state.

[if] the newest indexed snapshot is valid [then] latest serves the newest snapshot, [else stop].
[if] the newest indexed snapshot is invalid JSON [then] latest falls back to the prior complete slot, [else stop].
[if] every indexed snapshot is invalid [then] latest is None and the route returns 404, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.rescue.ring import append_snapshot, read_index, read_latest
from apps.webui.server.routes.rescue_snapshots import router as rescue_router
from tests.webui.rescue_snapshot_builders import minimal_rescue_snapshot

pytestmark = pytest.mark.requirement("RESCUE-01")

LATEST_URL = "/api/v1/performance/rescue-snapshots/latest"


def _seed_ring(data_dir: Path, count: int) -> None:
    """Append ``count`` valid snapshots marked v1..vN to the ring."""
    for number in range(1, count + 1):
        append_snapshot(data_dir, minimal_rescue_snapshot(1000 * number, marker=f"v{number}"))


def _slot_path(data_dir: Path, slot: int) -> Path:
    """Return the on-disk ring file for ``slot``."""
    return data_dir / "state" / "rescue" / f"ring-{slot}.json"


def _corrupt_slot(data_dir: Path, slot: int) -> None:
    """Overwrite a ring slot with truncated, unparseable JSON."""
    _slot_path(data_dir, slot).write_text('{"schema": 1, "captured_at_ms": ', encoding="utf-8")


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """Mount the rescue router on a bare app whose data dir is ``tmp_path``."""
    application = FastAPI()
    application.state.data_dir = tmp_path
    application.include_router(rescue_router, prefix="/api/v1")
    return application


def test_latest_serves_newest_when_it_is_valid(tmp_path: Path) -> None:
    """A valid newest slot is what latest returns, not an older one."""
    _seed_ring(tmp_path, 3)
    assert read_index(tmp_path).newest_slot == 2
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v3"


def test_latest_skips_newest_slot_with_invalid_json(tmp_path: Path) -> None:
    """A malformed newest ring file falls back to the previous complete slot."""
    _seed_ring(tmp_path, 3)
    newest = read_index(tmp_path).newest_slot
    _corrupt_slot(tmp_path, newest)
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v2"


def test_latest_skips_newest_slot_with_non_object_json(tmp_path: Path) -> None:
    """A newest ring file that parses but is not an object is skipped too."""
    _seed_ring(tmp_path, 2)
    newest = read_index(tmp_path).newest_slot
    _slot_path(tmp_path, newest).write_text("[1, 2, 3]\n", encoding="utf-8")
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v1"


def test_latest_skips_several_corrupt_slots_after_wrap(tmp_path: Path) -> None:
    """After a wrap, latest walks back past every corrupt slot to the first good one."""
    _seed_ring(tmp_path, 10)
    newest = read_index(tmp_path).newest_slot
    assert newest == 1
    _corrupt_slot(tmp_path, 1)
    _corrupt_slot(tmp_path, 0)
    latest = read_latest(tmp_path)
    assert latest is not None
    assert latest["marker"] == "v8"


def test_latest_is_none_when_every_indexed_slot_is_invalid(tmp_path: Path) -> None:
    """With every indexed slot corrupt, latest reports nothing instead of raising."""
    _seed_ring(tmp_path, 3)
    for slot in range(3):
        _corrupt_slot(tmp_path, slot)
    assert read_latest(tmp_path) is None


@pytest.mark.asyncio
async def test_latest_route_falls_back_past_corrupt_newest(app: FastAPI, tmp_path: Path) -> None:
    """The latest endpoint serves the prior complete slot when the newest file is malformed."""
    _seed_ring(tmp_path, 3)
    _corrupt_slot(tmp_path, read_index(tmp_path).newest_slot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        latest = await client.get(LATEST_URL)
    assert latest.status_code == 200
    assert latest.json()["marker"] == "v2"


@pytest.mark.asyncio
async def test_latest_route_404_when_every_slot_is_corrupt(app: FastAPI, tmp_path: Path) -> None:
    """The latest endpoint answers 404 rather than 500 when no slot is readable."""
    _seed_ring(tmp_path, 2)
    for slot in range(2):
        _corrupt_slot(tmp_path, slot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        latest = await client.get(LATEST_URL)
    assert latest.status_code == 404
