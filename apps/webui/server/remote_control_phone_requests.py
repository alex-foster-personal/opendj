"""Paired phone remote request routes and Requests tab (issue #10448).

The LAN listener forwards token-authenticated /remote/api/requests traffic to
the loopback engine store from #10444 and serves the Requests tab files under
/remote. requester_key never leaves the listener toward the phone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

ENGINE_TIMEOUT_S = 5.0
PHONE_REMOTE_PATH = "/remote"
PHONE_ROW_KEYS = (
    "id",
    "created_at",
    "kind",
    "stable_id",
    "text",
    "requester_name",
    "votes",
    "status",
    "status_at",
)
REMOTE_PAGE_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "remote.js": "text/javascript; charset=utf-8",
    "remote.css": "text/css; charset=utf-8",
    "remote-tabs.js": "text/javascript; charset=utf-8",
    "requests-tab.js": "text/javascript; charset=utf-8",
    "requests-tab-render.js": "text/javascript; charset=utf-8",
    "requests-tab.css": "text/css; charset=utf-8",
}

BearerReader = Callable[[Request], str | None]


def remote_page_dir() -> Path:
    """Prefer a built copy of the phone page, else the static source."""
    built = Path(__file__).resolve().parents[1] / "frontend" / "build" / "remote"
    if (built / "index.html").is_file():
        return built
    return Path(__file__).resolve().parents[1] / "frontend" / "static" / "remote"


def serve_remote_page_file(name: str) -> FileResponse:
    """Return one allowlisted file from the phone remote page directory."""
    if name not in REMOTE_PAGE_ASSETS:
        raise HTTPException(status_code=404, detail="Not Found")
    path = remote_page_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        path,
        media_type=REMOTE_PAGE_ASSETS[name],
        headers={"Cache-Control": "no-cache"},
    )


def phone_request_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy DJ-facing fields and resolved title/artist; never requester_key."""
    out = {key: row.get(key) for key in PHONE_ROW_KEYS}
    if row.get("kind") == "track":
        out["title"] = str(row.get("title") or "")
        out["artist"] = str(row.get("artist") or "")
    else:
        out["title"] = ""
        out["artist"] = ""
    return out


def _json_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _json_response(response: httpx.Response, payload: Any) -> JSONResponse:
    if payload is None:
        payload = {"error": "engine_unavailable", "message": "engine returned a non-JSON body"}
    return JSONResponse(status_code=response.status_code, content=payload)


class PhoneRequestRoutes:
    """Paired forwards from /remote/api/requests* to the loopback engine."""

    def __init__(self, session: Any, engine_url: str, bearer_token: BearerReader) -> None:
        self.session = session
        self.base = engine_url.rstrip("/")
        self.bearer_token = bearer_token

    def _auth(self, request: Request) -> None:
        self.session.authenticate(self.bearer_token(request))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(trust_env=False, timeout=ENGINE_TIMEOUT_S)

    async def _call(self, method: str, path: str, json: Any | None = None) -> httpx.Response | JSONResponse:
        try:
            async with self._client() as client:
                return await client.request(method, f"{self.base}{path}", json=json)
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={"error": "engine_unavailable", "message": "engine unavailable"},
            )

    async def get_requests(self, request: Request) -> JSONResponse:
        self._auth(request)
        listed = await self._call("GET", "/api/v1/requests?status=new")
        if isinstance(listed, JSONResponse):
            return listed
        payload = _json_payload(listed)
        rows = payload.get("requests") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        return _json_response(listed, {"requests": [phone_request_row(row) for row in rows if isinstance(row, dict)]})

    async def get_session(self, request: Request) -> JSONResponse:
        self._auth(request)
        return await self._forward_raw("GET", "/api/v1/requests/session")

    async def put_session(self, request: Request) -> JSONResponse:
        self._auth(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return await self._forward_raw("PUT", "/api/v1/requests/session", json=body)

    async def post_dismiss(self, request_id: str, request: Request) -> JSONResponse:
        self._auth(request)
        return await self._forward_row("POST", f"/api/v1/requests/{quote(request_id, safe='')}:dismiss")

    async def post_loaded(self, request_id: str, request: Request) -> JSONResponse:
        self._auth(request)
        return await self._forward_row("POST", f"/api/v1/requests/{quote(request_id, safe='')}:loaded")

    async def _forward_raw(self, method: str, path: str, json: Any | None = None) -> JSONResponse:
        response = await self._call(method, path, json=json)
        if isinstance(response, JSONResponse):
            return response
        payload = _json_payload(response)
        return _json_response(response, payload)

    async def _forward_row(self, method: str, path: str) -> JSONResponse:
        response = await self._call(method, path)
        if isinstance(response, JSONResponse):
            return response
        payload = _json_payload(response)
        if response.status_code < 400 and isinstance(payload, dict):
            payload = phone_request_row(payload)
        return _json_response(response, payload)


def get_remote_page() -> FileResponse:
    return serve_remote_page_file("index.html")


def get_remote_page_asset(name: str) -> FileResponse:
    return serve_remote_page_file(name)


def mount_phone_request_routes(app: FastAPI, session: Any, engine_url: str) -> None:
    """Register /remote page assets and paired /remote/api/requests forwards."""
    from apps.webui.server.remote_control import _bearer_token

    api = PhoneRequestRoutes(session, engine_url, _bearer_token)
    app.add_api_route(PHONE_REMOTE_PATH, get_remote_page, methods=["GET"])
    app.add_api_route(f"{PHONE_REMOTE_PATH}/", get_remote_page, methods=["GET"])
    app.add_api_route("/remote/api/requests", api.get_requests, methods=["GET"], response_model=None)
    app.add_api_route("/remote/api/requests/session", api.get_session, methods=["GET"], response_model=None)
    app.add_api_route("/remote/api/requests/session", api.put_session, methods=["PUT"], response_model=None)
    app.add_api_route(
        "/remote/api/requests/{request_id}:dismiss",
        api.post_dismiss,
        methods=["POST"],
        response_model=None,
    )
    app.add_api_route(
        "/remote/api/requests/{request_id}:loaded",
        api.post_loaded,
        methods=["POST"],
        response_model=None,
    )
    app.add_api_route(f"{PHONE_REMOTE_PATH}/{{name}}", get_remote_page_asset, methods=["GET"])


__all__ = ["mount_phone_request_routes", "phone_request_row"]
