"""HTTP helpers for dialing the other laptop's B2B door."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

import httpx

from apps.b2b_pairing.b2b_pairing import PairingError

from .b2b_nearby_protocol import DOOR_TIMEOUT_S, NearbyPeer


def _answer(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PairingError("peer_error", f"The other laptop answered {response.status_code}, not JSON", 502) from exc
    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and "code" in detail:
            raise PairingError(str(detail["code"]), str(detail.get("message", "")), response.status_code)
        raise PairingError("peer_error", f"The other laptop answered {response.status_code}", 502)
    if not isinstance(payload, dict):
        raise PairingError("peer_error", "The other laptop answered a non-object", 502)
    return payload


def _post(url: str, body: dict[str, Any], timeout_s: float = DOOR_TIMEOUT_S) -> dict[str, Any]:
    try:
        response = httpx.post(url, json=body, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise PairingError("peer_unreachable", f"The other laptop did not answer: {type(exc).__name__}", 502) from exc
    return _answer(response)


def _get(url: str, timeout_s: float = DOOR_TIMEOUT_S) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise PairingError("peer_unreachable", f"The other laptop did not answer: {type(exc).__name__}", 502) from exc
    return _answer(response)


def _first(peer: NearbyPeer, call: Callable[[str], httpx.Response]) -> tuple[dict[str, Any], str, str]:
    """The first ranked address that answers at all; its answer, address and path kind."""
    if not peer.addresses:
        raise PairingError("peer_unreachable", f"{peer.label} has no LAN address (bridge, wired or Wi-Fi)", 502)
    for ip, kind in peer.addresses:
        try:
            response = call(ip)
        except httpx.HTTPError:
            continue
        return _answer(response), ip, kind
    raise PairingError(
        "peer_unreachable", f"{peer.label} did not answer on any of {len(peer.addresses)} addresses", 502
    )


def _post_first(peer: NearbyPeer, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    return _first(peer, lambda ip: httpx.post(f"http://{ip}:{peer.port}{path}", json=body, timeout=DOOR_TIMEOUT_S))
