"""Is this request the machine's own operator? One predicate, no HTTP policy.

Used by routes that act with machine authority (CloudSync sync, fleet,
enrollment grants), whose CLI twins need a shell on the machine. A request
passes only if EVERY one of these holds:

1. The TCP peer is a loopback address (IPv4-mapped IPv6 unwrapped). A tailnet
   or LAN peer is somebody else's machine.
2. No reverse-proxy header is present (``Forwarded``, ``X-Forwarded-*``,
   ``X-Real-IP``, Tailscale Serve's ``Tailscale-User-Login``). cloudflared,
   Tailscale Serve and friends connect FROM the loopback on behalf of another
   machine; their headers are the only thing that says so. Tailscale Serve
   omits its identity header for tagged devices, which is why a forwarding
   header alone is enough to refuse. uvicorn's default ``proxy_headers`` also
   rewrites the peer from ``X-Forwarded-For``; this check does not rely on it.
3. The ``Host`` header names a loopback authority (``127.0.0.1``,
   ``localhost``, ``[::1]``, any port). This is the DNS-rebinding guard: a
   hostile page whose domain was rebound to 127.0.0.1 connects from the
   loopback, but its browser still sends the attacker's domain as ``Host``.
   It also covers the share host, which is never a loopback name.
4. An ``Origin`` header, when present, is an http(s) loopback origin. The
   SvelteKit dev server, the mounted SPA and the desktop shell all load from
   loopback origins; ``null`` and every other origin are refused.
5. ``Sec-Fetch-Site`` is not ``cross-site``.

A caller with no ``Origin`` (curl, an agent, the CLI's own HTTP) passes on
1-3 alone.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import Request

LOOPBACK_HOSTNAME: str = "localhost"
LOCAL_ORIGIN_SCHEMES: frozenset[str] = frozenset({"http", "https"})
#: Headers a forwarding proxy adds; any one of them on a loopback peer means
#: the peer is relaying another machine. ``tailscale-user-login`` is set by
#: ``tailscale serve`` for user (not tagged) devices.
PROXY_HEADERS: tuple[str, ...] = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-real-ip",
    "tailscale-user-login",
)


def is_loopback_ip(host: str | None) -> bool:
    """True iff ``host`` is an IP literal on the loopback (``::ffff:127.x`` too).

    The explicit ``ipv4_mapped`` unwrap is for older CPython patch releases,
    where ``IPv6Address.is_loopback`` ignored the mapped IPv4 address; newer
    ones (3.11.15 measured) already unwrap it, so there it is a no-op.
    """
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def is_loopback_hostname(hostname: str | None) -> bool:
    """True for ``localhost`` or a loopback IP literal (brackets already stripped)."""
    return hostname == LOOPBACK_HOSTNAME or is_loopback_ip(hostname)


def _hostname_of_authority(authority: str) -> str | None:
    """``host[:port]`` or ``[v6][:port]`` to its lowercased hostname."""
    return urlsplit(f"//{authority.strip()}").hostname


def _is_loopback_origin(origin: str) -> bool:
    parsed = urlsplit(origin.strip())
    return parsed.scheme in LOCAL_ORIGIN_SCHEMES and is_loopback_hostname(parsed.hostname)


def local_operator_refusal(request: Request) -> str | None:
    """None when the caller is the local operator, else why it is not."""
    peer = request.client.host if request.client is not None else None
    if not is_loopback_ip(peer):
        return f"caller {peer!r} is not on the loopback"
    forwarded = [name for name in PROXY_HEADERS if name in request.headers]
    if forwarded:
        return (
            f"the request carries proxy header(s) {forwarded}; the loopback "
            "peer is relaying another machine"
        )
    host = request.headers.get("host", "")
    if not is_loopback_hostname(_hostname_of_authority(host)):
        return f"Host {host!r} is not a loopback authority"
    origin = request.headers.get("origin")
    if origin is not None and not _is_loopback_origin(origin):
        return f"Origin {origin!r} is not a loopback origin"
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return "Sec-Fetch-Site is cross-site"
    return None


__all__ = [
    "PROXY_HEADERS",
    "is_loopback_hostname",
    "is_loopback_ip",
    "local_operator_refusal",
]
