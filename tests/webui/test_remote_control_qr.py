"""REMOTE-CTL QR entry: engine renders remote/request/display URLs as SVG (#10442).

[if] the listener is on with code K7Q4MX at 192.0.2.5:47810 [then] the SVG encodes http://192.0.2.5:47810/remote#code=K7Q4MX, [else stop].
[if] the listener is off [then] the QR route answers 409 with a reason, [else stop].
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.remote_control import encode_qr_url, qr_svg_payload
from apps.webui.server.routes import performance_remote_control as remote_routes
from apps.webui.server.routes.performance_remote_control import router as remote_router

pytestmark = pytest.mark.requirement("REMOTE-CTL-10715")

BASE = "/api/v1/performance/remote-control"


@asynccontextmanager
async def _engine(state_dir: Path) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "state.db"
    db.touch()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await remote_routes.restore_remote_control(app)
        try:
            yield
        finally:
            await remote_routes.close_remote_control(app, clear_listening=True)

    app = FastAPI(lifespan=lifespan)
    app.state.state_db_path = str(db)
    app.include_router(remote_router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://engine", timeout=30) as client,
    ):
        yield app, client


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_encode_qr_url_shapes() -> None:
    """[if] a target is remote, request or display [then] the URL has that path and only remote carries the code fragment, [else stop]."""
    assert (
        encode_qr_url(target="remote", address="192.0.2.5", port=47810, code="K7Q4MX")
        == "http://192.0.2.5:47810/remote#code=K7Q4MX"
    )
    assert (
        encode_qr_url(target="request", address="192.0.2.5", port=47810, code="X") == "http://192.0.2.5:47810/request"
    )
    assert (
        encode_qr_url(target="display", address="192.0.2.5", port=47810, code="X")
        == "http://192.0.2.5:47810/now-playing"
    )
    # Code stays in the fragment so access logs never see code=.
    remote = encode_qr_url(target="remote", address="192.0.2.5", port=47810, code="K7Q4MX")
    assert "code=" not in remote.split("#", 1)[0]


def test_qr_svg_matches_segno_reference() -> None:
    """[if] a URL is rendered [then] the SVG equals segno's own medium-ECC render of it, [else stop]."""
    import segno

    url = "http://192.0.2.5:47810/remote#code=K7Q4MX"
    payload = qr_svg_payload(url)
    assert payload.startswith("<svg")
    assert payload == segno.make(url, error="m").svg_inline(scale=4)


def test_qr_route_encodes_remote_url_and_409_when_off(state_dir: Path) -> None:
    """[if] the listener is off [then] 409, and once on the SVG encodes the remote URL, [else stop]."""

    async def run() -> None:
        async with _engine(state_dir) as (app, client):
            off = await client.get(f"{BASE}/qr.svg", params={"target": "remote"})
            assert off.status_code == 409, off.text

            with patch(
                "apps.webui.server.remote_control.lan_address",
                return_value=("192.0.2.5", None),
            ):
                started = await client.post(f"{BASE}/start", json={"port": 0})
                assert started.status_code == 200, started.text
                body = started.json()
                assert body["status"] == "listening"
                port = int(body["port"])
                session = remote_routes._session(app)
                assert session is not None
                session.display_code = "K7Q4MX"
                from apps.b2b_pairing.b2b_pairing import normalize_code

                session._canonical_code = normalize_code("K7Q4MX")
                session.address = "192.0.2.5"

                svg = await client.get(f"{BASE}/qr.svg", params={"target": "remote"})
                assert svg.status_code == 200, svg.text
                assert svg.headers["content-type"].startswith("image/svg+xml")
                expected_url = encode_qr_url(
                    target="remote",
                    address="192.0.2.5",
                    port=port,
                    code="K7Q4MX",
                )
                assert expected_url == f"http://192.0.2.5:{port}/remote#code=K7Q4MX"
                assert svg.text == qr_svg_payload(expected_url)

                req = await client.get(f"{BASE}/qr.svg", params={"target": "request"})
                assert req.status_code == 200, req.text
                assert req.text == qr_svg_payload(f"http://192.0.2.5:{port}/request")

    asyncio.run(run())
