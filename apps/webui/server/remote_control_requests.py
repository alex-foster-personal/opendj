"""Public crowd-request routes on the LAN listener (issue #10445).

Mounted at /request/api/* with no bearer token. The listener enforces session
open, catalog membership, free-text policy, the bundled denylist, per-phone
cooldown, and vote-once, then forwards accepted writes to the loopback engine
store from #10444. requester_key is sha256(cookie + source IP) and is the only
identity the engine stores.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from apps.webui.server import crowd_requests as store
from apps.webui.server.remote_control_request_support import (
    REQUESTS_CAP,
    GuestRequestIn,
    PublicBooks,
    _client,
    _code,
    _json_body,
    attach_guest,
    carry_guest_cookie,
    fetch_catalog_name,
    fetch_engine_session,
    load_open_session,
    parse_submit_body,
    public_request_row,
    refuse_if_closed,
    refuse_text,
    validate_name,
)


class PublicRequestRoutes:
    """HTTP handlers for /request/api/*, sharing one cooldown and vote book."""

    def __init__(self, engine_url: str) -> None:
        self.base = engine_url.rstrip("/")
        self.books = PublicBooks()

    async def get_session(self, request: Request, response: Response) -> dict[str, Any]:
        attach_guest(request, response)
        async with _client() as client:
            session = await fetch_engine_session(client, self.base)
            playlist_id = str((session or {}).get("catalog_playlist_id") or "")
            catalog_name = await fetch_catalog_name(client, self.base, playlist_id)
        row = session or {}
        return {
            "open": bool(row.get("open")),
            "catalog_name": catalog_name,
            "free_text_allowed": bool(row.get("free_text_allowed")),
            "cooldown_s": int(row.get("cooldown_s") or store.DEFAULT_COOLDOWN_S),
        }

    async def get_catalog(self, request: Request, response: Response) -> dict[str, Any] | JSONResponse:
        attach_guest(request, response)
        async with _client() as client:
            loaded = await load_open_session(client, self.base, tracks=True)
            if isinstance(loaded, JSONResponse):
                return carry_guest_cookie(response, loaded)
            _session, tracks = loaded
        return {"tracks": tracks}

    async def post_request(self, body: GuestRequestIn, request: Request, response: Response) -> JSONResponse:
        key = attach_guest(request, response)
        async with _client() as client:
            # A closed session answers 423 before any body validation.
            refused = refuse_if_closed(await fetch_engine_session(client, self.base))
        if refused is not None:
            return carry_guest_cookie(response, refused)
        name = validate_name(body.name)
        if isinstance(name, JSONResponse):
            return carry_guest_cookie(response, name)
        parsed = parse_submit_body(body)
        if isinstance(parsed, JSONResponse):
            return carry_guest_cookie(response, parsed)
        kind, value = parsed
        async with _client() as client:
            return carry_guest_cookie(
                response, await _submit_request(client, self.base, self.books, key, name, kind, value)
            )

    async def post_vote(self, request_id: str, request: Request, response: Response) -> dict[str, Any] | JSONResponse:
        key = attach_guest(request, response)
        async with _client() as client:
            loaded = await load_open_session(client, self.base)
            if isinstance(loaded, JSONResponse):
                return carry_guest_cookie(response, loaded)
            async with self.books.vote_lock(request_id, key):
                if self.books.votes.already_voted(request_id, key):
                    return {"already_voted": True}
                try:
                    voted = await client.post(f"{self.base}/api/v1/requests/{quote(request_id, safe='')}/vote")
                except httpx.HTTPError:
                    return carry_guest_cookie(response, _code(503, "engine_unavailable"))
                result = _vote_result(self.books, request_id, key, voted)
                if isinstance(result, JSONResponse):
                    return carry_guest_cookie(response, result)
                return result

    async def get_requests(self, request: Request, response: Response) -> dict[str, Any] | JSONResponse:
        attach_guest(request, response)
        async with _client() as client:
            loaded = await load_open_session(client, self.base, tracks=True)
            if isinstance(loaded, JSONResponse):
                return carry_guest_cookie(response, loaded)
            _session, tracks = loaded
            try:
                listed = await client.get(f"{self.base}/api/v1/requests", params={"status": "new"})
            except httpx.HTTPError:
                return carry_guest_cookie(response, _code(503, "engine_unavailable"))
        if listed.status_code != 200:
            return carry_guest_cookie(response, _code(listed.status_code, "list_failed"))
        payload = _json_body(listed)
        rows = payload.get("requests") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            rows = []
        catalog = {row["stable_id"]: row for row in tracks}
        public = [public_request_row(row, catalog) for row in rows if isinstance(row, dict)][:REQUESTS_CAP]
        return {"requests": public}

    async def get_artwork(self, stable_id: str, request: Request, response: Response) -> Response:
        attach_guest(request, response)
        async with _client() as client:
            loaded = await load_open_session(client, self.base, tracks=True)
            if isinstance(loaded, JSONResponse):
                return carry_guest_cookie(response, loaded)
            _session, tracks = loaded
            if stable_id not in {row["stable_id"] for row in tracks}:
                return carry_guest_cookie(response, JSONResponse(status_code=404, content={"code": "not_in_catalog"}))
            try:
                art = await client.get(f"{self.base}/api/v1/tracks/{quote(stable_id, safe='')}/artwork")
            except httpx.HTTPError:
                return carry_guest_cookie(
                    response, JSONResponse(status_code=404, content={"code": "artwork_unavailable"})
                )
        media = art.headers.get("content-type") or "application/octet-stream"
        return carry_guest_cookie(
            response, Response(content=art.content, status_code=art.status_code, media_type=media)
        )


def _vote_result(
    books: PublicBooks, request_id: str, requester_key: str, voted: httpx.Response
) -> dict[str, Any] | JSONResponse:
    if voted.status_code == 404:
        return _code(404, "request_not_found")
    if voted.status_code != 200:
        return _code(voted.status_code, "vote_failed")
    books.votes.mark(request_id, requester_key)
    payload = _json_body(voted)
    votes = payload.get("votes") if isinstance(payload, dict) else None
    return {"already_voted": False, "votes": votes}


def _submit_payload(
    session: Mapping[str, Any],
    kind: str,
    value: str,
    name: str,
    requester_key: str,
    tracks: list[dict[str, str]],
) -> dict[str, str] | JSONResponse:
    if kind == "track":
        if value not in {row["stable_id"] for row in tracks}:
            return _code(422, "not_in_catalog")
        return {"stable_id": value, "requester_name": name, "requester_key": requester_key}
    blocked = refuse_text(value, session)
    if blocked is not None:
        return blocked
    return {"text": value, "requester_name": name, "requester_key": requester_key}


async def _submit_request(
    client: httpx.AsyncClient,
    engine_url: str,
    books: PublicBooks,
    requester_key: str,
    name: str,
    kind: str,
    value: str,
) -> JSONResponse:
    loaded = await load_open_session(client, engine_url, tracks=kind == "track")
    if isinstance(loaded, JSONResponse):
        return loaded
    session, tracks = loaded
    payload = _submit_payload(session, kind, value, name, requester_key, tracks)
    if isinstance(payload, JSONResponse):
        return payload
    async with books.submit_lock(requester_key):
        retry = books.retry_after_s(
            requester_key, int(session.get("cooldown_s") or store.DEFAULT_COOLDOWN_S), time.monotonic()
        )
        if retry is not None:
            return JSONResponse(status_code=429, content={"retry_after_s": retry})
        return await _forward_new_request(client, engine_url, books, requester_key, payload)


async def _forward_new_request(
    client: httpx.AsyncClient,
    engine_url: str,
    books: PublicBooks,
    requester_key: str,
    payload: dict[str, str],
) -> JSONResponse:
    try:
        created = await client.post(f"{engine_url}/api/v1/requests", json=payload)
    except httpx.HTTPError:
        return _code(503, "engine_unavailable")
    if created.status_code != 201:
        body = _json_body(created)
        code = "invalid_request"
        if isinstance(body, dict):
            code = str(body.get("error") or body.get("code") or code)
        return _code(created.status_code if created.status_code >= 400 else 422, code)
    books.mark_submit(requester_key, time.monotonic())
    row = _json_body(created)
    request_id = row.get("id") if isinstance(row, dict) else None
    return JSONResponse(status_code=201, content={"id": request_id})


def mount_public_request_routes(app: FastAPI, engine_url: str) -> None:
    """Register /request/api/* on the LAN listener app."""
    api = PublicRequestRoutes(engine_url)
    app.add_api_route("/request/api/session", api.get_session, methods=["GET"])
    app.add_api_route("/request/api/catalog", api.get_catalog, methods=["GET"], response_model=None)
    app.add_api_route(
        "/request/api/requests",
        api.post_request,
        methods=["POST"],
        status_code=201,
        response_model=None,
    )
    app.add_api_route(
        "/request/api/requests/{request_id}/vote",
        api.post_vote,
        methods=["POST"],
        response_model=None,
    )
    app.add_api_route("/request/api/requests", api.get_requests, methods=["GET"], response_model=None)
    app.add_api_route(
        "/request/api/artwork/{stable_id}",
        api.get_artwork,
        methods=["GET"],
        response_model=None,
    )


__all__ = ["mount_public_request_routes"]
