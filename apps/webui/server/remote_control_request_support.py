"""Helpers for the public crowd-request listener routes (issue #10445).

Cookie minting, engine reads, catalog trimming, name/text checks and the
cooldown book live here so the route module stays a thin HTTP adapter.
"""

from __future__ import annotations

import asyncio
import math
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from apps.webui.server import crowd_requests as store

COOKIE_NAME = "odj_req"
COOKIE_PATH = "/request"
COOKIE_BYTES = 16
CATALOG_CAP = 200
REQUESTS_CAP = 50
ENGINE_TIMEOUT_S = 5.0


class GuestRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_id: str | None = None
    text: str | None = None
    name: str | None = None


class PublicBooks:
    """Listener-side cooldown timestamps and the shared vote-once ledger."""

    def __init__(self) -> None:
        self.last_submit: dict[str, float] = {}
        self.votes = store.VoteLedger()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, kind: str, key: str) -> asyncio.Lock:
        slot = f"{kind}:{key}"
        lock = self._locks.get(slot)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[slot] = lock
        return lock

    def submit_lock(self, requester_key: str) -> asyncio.Lock:
        return self._lock("submit", requester_key)

    def vote_lock(self, request_id: str, requester_key: str) -> asyncio.Lock:
        return self._lock("vote", f"{request_id}\0{requester_key}")

    def retry_after_s(self, requester_key: str, cooldown_s: int, now: float) -> int | None:
        last = self.last_submit.get(requester_key)
        if last is None:
            return None
        remaining = cooldown_s - (now - last)
        if remaining <= 0:
            return None
        return max(1, math.ceil(remaining))

    def mark_submit(self, requester_key: str, now: float) -> None:
        self.last_submit[requester_key] = now


def _code(status_code: int, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, **extra})


def closed_response() -> JSONResponse:
    return _code(423, "requests_closed")


def _client_ip(request: Request) -> str:
    client = request.client
    return "0.0.0.0" if client is None else client.host


def attach_guest(request: Request, response: Response) -> str:
    """Set odj_req on first visit and return the hashed requester_key."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        cookie = secrets.token_hex(COOKIE_BYTES)
        response.set_cookie(
            key=COOKIE_NAME,
            value=cookie,
            httponly=True,
            samesite="lax",
            path=COOKIE_PATH,
        )
    return store.requester_fingerprint(cookie, _client_ip(request))


def carry_guest_cookie(source: Response, returned: Response) -> Response:
    """Copy odj_req Set-Cookie onto an explicit Response so FastAPI does not drop it."""
    if source is returned:
        return returned
    for cookie in source.headers.getlist("set-cookie"):
        returned.headers.append("set-cookie", cookie)
    return returned


def refuse_if_closed(session: Mapping[str, Any] | None) -> JSONResponse | None:
    if session is None or not session.get("open"):
        return closed_response()
    return None


def _json_body(response: httpx.Response) -> Any:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


async def fetch_engine_session(client: httpx.AsyncClient, engine_url: str) -> dict[str, Any] | None:
    try:
        response = await client.get(f"{engine_url}/api/v1/requests/session")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    payload = _json_body(response)
    return payload if isinstance(payload, dict) else None


async def fetch_catalog_name(client: httpx.AsyncClient, engine_url: str, playlist_id: str) -> str:
    if not playlist_id:
        return ""
    try:
        response = await client.get(f"{engine_url}/api/v1/playlists/{quote(playlist_id, safe='')}")
    except httpx.HTTPError:
        return ""
    if response.status_code != 200:
        return ""
    payload = _json_body(response)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("name") or "")


def _trim_catalog_row(row: Mapping[str, Any]) -> dict[str, str] | None:
    stable_id = str(row.get("stable_id") or "").strip()
    if not stable_id:
        return None
    return {
        "stable_id": stable_id,
        "title": str(row.get("title") or ""),
        "artist": str(row.get("artist") or ""),
        "artwork": f"/request/api/artwork/{stable_id}",
    }


def _rows_from_catalog_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("tracks"), list):
        return payload["tracks"]
    return []


async def fetch_catalog_tracks(client: httpx.AsyncClient, engine_url: str, playlist_id: str) -> list[dict[str, str]]:
    if not playlist_id:
        return []
    try:
        response = await client.get(
            f"{engine_url}/api/v1/playlists/{quote(playlist_id, safe='')}/tracks",
            params={"limit": CATALOG_CAP},
        )
    except httpx.HTTPError:
        return []
    if response.status_code != 200:
        return []
    out: list[dict[str, str]] = []
    for row in _rows_from_catalog_payload(_json_body(response)):
        if not isinstance(row, dict):
            continue
        trimmed = _trim_catalog_row(row)
        if trimmed is not None:
            out.append(trimmed)
        if len(out) >= CATALOG_CAP:
            break
    return out


async def load_open_session(
    client: httpx.AsyncClient,
    engine_url: str,
    *,
    tracks: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]]] | JSONResponse:
    """Return the open engine session and optional catalog, or a refusal."""
    session = await fetch_engine_session(client, engine_url)
    refused = refuse_if_closed(session)
    if refused is not None:
        return refused
    assert session is not None
    if not tracks:
        return session, []
    playlist_id = str(session.get("catalog_playlist_id") or "")
    return session, await fetch_catalog_tracks(client, engine_url, playlist_id)


def validate_name(name: str | None) -> str | JSONResponse:
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > store.NAME_MAX:
        return _code(422, "invalid_name")
    return cleaned


def parse_submit_body(body: GuestRequestIn) -> tuple[str, str] | JSONResponse:
    track = (body.stable_id or "").strip() or None
    message = (body.text or "").strip() or None
    if (track is None) == (message is None):
        return _code(422, "invalid_request")
    if message is not None and len(message) > store.TEXT_MAX:
        return _code(422, "invalid_text")
    if track is not None:
        return ("track", track)
    return ("text", message or "")


def refuse_text(message: str, session: Mapping[str, Any]) -> JSONResponse | None:
    if not session.get("free_text_allowed"):
        return _code(422, "free_text_not_allowed")
    if store.text_is_filtered(message):
        return _code(422, "filtered")
    return None


def public_request_row(row: Mapping[str, Any], catalog: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    title = ""
    artist = ""
    if row.get("kind") == "track":
        found = catalog.get(str(row.get("stable_id") or ""))
        if found is not None:
            title = found["title"]
            artist = found["artist"]
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "title": title,
        "artist": artist,
        "text": row.get("text"),
        "requester_name": row.get("requester_name"),
        "votes": row.get("votes", 0),
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=ENGINE_TIMEOUT_S)
