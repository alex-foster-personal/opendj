"""CROWDREQ-10725: the listener serves the crowd request page at /request and /now-playing.

[if] GET /request is asked with no token [then] 200 text/html, traversal is refused, [else stop].
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from apps.webui.server.remote_control import RemoteSession

pytestmark = pytest.mark.requirement("CROWDREQ-10725")


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


async def _open_session(state_dir: Path) -> RemoteSession:
    session = RemoteSession(state_dir=state_dir, requested_port=0)
    await session.open()
    return session


def test_request_page_is_html_without_a_token(state_dir: Path) -> None:
    """[if] GET /request is asked with no token [then] 200 text/html, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                page = await lan.get(_url(port, "/request"), headers={"host": f"127.0.0.1:{port}"})
                assert page.status_code == 200, page.text
                content_type = page.headers.get("content-type", "")
                assert "text/html" in content_type
                assert b"request.js" in page.content
                assert b"<html" in page.content.lower()
                scripts = await lan.get(_url(port, "/request/request.js"), headers={"host": f"127.0.0.1:{port}"})
                assert scripts.status_code == 200, scripts.text
                assert "javascript" in scripts.headers.get("content-type", "")
                css = await lan.get(_url(port, "/request/request.css"), headers={"host": f"127.0.0.1:{port}"})
                assert css.status_code == 200, css.text
                assert "text/css" in css.headers.get("content-type", "")
        finally:
            await session.close()

    asyncio.run(run())


def test_request_page_refuses_parent_traversal(state_dir: Path) -> None:
    """[if] GET /request/../ is asked [then] the listener refuses it, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                traversal = await lan.get(_url(port, "/request/../"), headers={"host": f"127.0.0.1:{port}"})
                assert traversal.status_code in {400, 403, 404}
                encoded = await lan.get(
                    _url(port, "/request/%2e%2e/remote_control.py"),
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert encoded.status_code in {400, 403, 404}
                assert b"RemoteSession" not in encoded.content
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.requirement("NOWPLAY-12084")
def test_now_playing_page_is_html_without_a_token(state_dir: Path) -> None:
    """[if] GET /now-playing is asked with no token [then] 200 html and its assets, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                host = {"host": f"127.0.0.1:{port}"}
                page = await lan.get(_url(port, "/now-playing"), headers=host)
                assert page.status_code == 200, page.text
                assert "text/html" in page.headers.get("content-type", "")
                assert b"display.js" in page.content
                js = await lan.get(_url(port, "/now-playing/display.js"), headers=host)
                assert js.status_code == 200
                assert "javascript" in js.headers.get("content-type", "")
                render_js = await lan.get(_url(port, "/now-playing/display-render.js"), headers=host)
                assert render_js.status_code == 200
                assert "javascript" in render_js.headers.get("content-type", "")
                css = await lan.get(_url(port, "/now-playing/display.css"), headers=host)
                assert css.status_code == 200
                assert "text/css" in css.headers.get("content-type", "")
                missing = await lan.get(_url(port, "/now-playing/nope.js"), headers=host)
                assert missing.status_code == 404
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.requirement("NOWPLAY-12084")
def test_now_playing_page_refuses_traversal_and_external_refs(state_dir: Path) -> None:
    """[if] /now-playing traversal is asked [then] listener refuses and bodies lack ext refs, [else stop]."""

    async def run() -> None:
        session = await _open_session(state_dir)
        port = session.port or 0
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                host = {"host": f"127.0.0.1:{port}"}
                traversal = await lan.get(_url(port, "/now-playing/../"), headers=host)
                assert traversal.status_code in {400, 403, 404}
                encoded = await lan.get(
                    _url(port, "/now-playing/%2e%2e/remote_control.py"),
                    headers=host,
                )
                assert encoded.status_code in {400, 403, 404}
                assert b"RemoteSession" not in encoded.content
                paths = [
                    "/now-playing",
                    "/now-playing/display.js",
                    "/now-playing/display-render.js",
                    "/now-playing/display.css",
                ]
                for path in paths:
                    resp = await lan.get(_url(port, path), headers=host)
                    assert resp.status_code == 200, path
                    assert b"http://" not in resp.content
                    assert b"https://" not in resp.content
                    if path.endswith(".css"):
                        assert b"@font-face" not in resp.content
                        assert b"url(" not in resp.content
        finally:
            await session.close()

    asyncio.run(run())
