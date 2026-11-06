"""B2BNET-14: one paired channel for every peer read, whichever path carries it.

The door's SERVER side was already one table for both paths (``door.serve_door_request``); this
is the CLIENT side. A peer feature asks ``channel.get(path)`` / ``channel.post(path, body)`` and
gets a real ``httpx.Response`` back, so the LAN door (HTTP to ``http://host:port`` with the
pairing's bearer token) and the internet door (a sealed door message through the relay room) are
the same to every caller: PEER row, deck artwork, cursor, shouts, the multicast probe, the Link
relay and the booth signalling all keep their existing response handling. A partner that cannot
be reached on either path raises ``ChannelUnreachable``, an ``httpx.TransportError``, so the
existing ``except httpx.HTTPError`` handlers name it exactly as they name a LAN partner that is
down. Nothing here picks a path; ``routes/b2b_channel.py`` does (LAN first, else internet).

The Link relay uses ``post_sealed`` for frames that authenticate themselves with a key derived
from the local pairing token. On LAN that operation omits the ordinary bearer header, so the
token never crosses the plaintext HTTP connection; the internet door keeps its existing sealed
room transport.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import httpx

from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_transport.door import DoorAnswer, DoorRequest

ChannelKind = Literal["lan", "internet"]
#: Seconds, or an ``httpx.Timeout`` (the Link relay keeps its connect timeout apart).
Timeout = float | httpx.Timeout
#: What a peer answer names as "where the partner is" on the internet path. It has no host on
#: purpose: a feature that needs the partner's LAN address (WebRTC host candidates) must refuse
#: rather than aim at the relay.
INTERNET_LABEL = "internet relay"
#: PairingError codes from the internet door that mean "the partner is not reachable right now".
_UNREACHABLE_CODES = frozenset({"peer_unreachable", "not_paired"})


class ChannelUnreachable(httpx.TransportError):
    """The partner did not answer on this channel (relay down, partner not on the relay)."""


class PairedChannel(Protocol):
    @property
    def kind(self) -> ChannelKind: ...

    @property
    def label(self) -> str:
        """The LAN URL, or ``INTERNET_LABEL``."""
        ...

    @property
    def token(self) -> str | None:
        """The pairing token: the Link relay's frame key derives from it."""
        ...

    def get(self, path: str, timeout: Timeout) -> httpx.Response: ...

    def post(self, path: str, body: bytes, timeout: Timeout) -> httpx.Response: ...

    def post_sealed(self, path: str, body: bytes, timeout: Timeout) -> httpx.Response: ...

    def bind(self, client: httpx.Client) -> PairedChannel: ...


@dataclass(frozen=True)
class LanChannel:
    """The LAN door at ``url`` through ``client`` (kept connections, so a ping pays no handshake)."""

    url: str
    token: str | None
    client: httpx.Client
    kind: ChannelKind = "lan"

    @property
    def label(self) -> str:
        return self.url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, path: str, timeout: Timeout) -> httpx.Response:
        try:
            return self.client.get(f"{self.url}{path}", headers=self._headers(), timeout=timeout)
        except httpx.RemoteProtocolError:
            # The door closed the kept connection as this read went out on it (its idle limit, a
            # restart): that is not an unreachable partner. Once more, on a new connection.
            return self.client.get(f"{self.url}{path}", headers=self._headers(), timeout=timeout)

    def post(self, path: str, body: bytes, timeout: Timeout) -> httpx.Response:
        return self._post(path, body, timeout, {**self._headers(), "content-type": "application/json"})

    def post_sealed(self, path: str, body: bytes, timeout: Timeout) -> httpx.Response:
        return self._post(path, body, timeout, {"content-type": "application/json"})

    def _post(self, path: str, body: bytes, timeout: Timeout, headers: dict[str, str]) -> httpx.Response:
        return self.client.post(f"{self.url}{path}", content=body, headers=headers, timeout=timeout)

    def bind(self, client: httpx.Client) -> LanChannel:
        return replace(self, client=client)


@dataclass(frozen=True)
class InternetChannel:
    """The internet door: ``send`` is ``InternetService.request`` (a sealed door message)."""

    send: Callable[[DoorRequest, float], DoorAnswer]
    token: str | None
    kind: ChannelKind = "internet"
    label: str = INTERNET_LABEL

    def get(self, path: str, timeout: Timeout) -> httpx.Response:
        route, _, query = path.partition("?")
        return self._ask(DoorRequest("GET", route, query), timeout)

    def post(self, path: str, body: bytes, timeout: Timeout) -> httpx.Response:
        return self._ask(DoorRequest("POST", path, "", body), timeout)

    def post_sealed(self, path: str, body: bytes, timeout: Timeout) -> httpx.Response:
        return self._ask(DoorRequest("POST", path, "", body), timeout)

    def bind(self, client: httpx.Client) -> InternetChannel:  # noqa: ARG002 - the Protocol's signature
        return self  # the relay room is the connection; there is no client to bind

    def _ask(self, request: DoorRequest, timeout: Timeout) -> httpx.Response:
        seconds = _seconds(timeout)
        try:
            answer = self.send(request, seconds)
        except PairingError as exc:
            if exc.code in _UNREACHABLE_CODES:
                raise ChannelUnreachable(f"{exc.code}: {exc}") from exc
            raise
        url = f"internet://relay{request.path}" + (f"?{request.query}" if request.query else "")
        return httpx.Response(answer.status, content=answer.body, headers={"content-type": answer.content_type},
                              request=httpx.Request(request.method, url))


def _seconds(timeout: Timeout) -> float:
    """The whole-answer wait an internet door message gets: a timeout's read budget."""
    if isinstance(timeout, httpx.Timeout):
        if timeout.read is None:
            raise ValueError("an internet door message needs a finite read timeout")
        return float(timeout.read)
    return float(timeout)
