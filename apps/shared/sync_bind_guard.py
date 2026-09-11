"""Bind guard: the sync hub serves only on loopback unless trust is explicit.

``/api/v1/sync/*`` has no authentication while enrollment is OBSERVE-only
(``specs/design_decision_12.md``, now ADR-0012): anything that can open a
socket to the hub can pull the whole library or push tombstones. So a process
that mounts the sync router refuses to boot on a non-loopback bind unless the
operator states, in the environment, that the network in front of it is
trusted: ``MDT_SYNC_TRUST_TAILNET=1``.

The supported tailnet exposure never needs the flag: bind 127.0.0.1 and put
``tailscale serve`` in front (docs/cloudsync/hub-runbook.md). The flag exists
for a host where that proxy is unavailable, and naming it is the point.

Where it runs: the engine's CLI preflight and ``create_app`` (from
``--host``), the legacy webui CLI (``--host``) and its import entry
``apps.webui.server.app:app`` (from ``MUSIC_DJ_BIND_HOST``). A bare
``uvicorn apps.webui.server.app:app --host 0.0.0.0`` is NOT seen: uvicorn
never hands its bind address to the app, so launch through the module CLI or
set ``MUSIC_DJ_BIND_HOST`` to match.

Fails closed. A hostname other than ``localhost`` counts as non-loopback,
because resolving it would make the verdict depend on DNS at boot time. A
flag value other than ``1`` / ``0`` / unset raises rather than reading as
false, the same rule ``MDT_IS_HUB`` follows.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping

TRUST_TAILNET_ENV: str = "MDT_SYNC_TRUST_TAILNET"
ADR_REFERENCE: str = "specs/design_decision_12.md (docs/decisions/ADR-0012-machine-enrollment.md)"
_LOOPBACK_NAMES: frozenset[str] = frozenset({"localhost"})


class SyncBindRefused(RuntimeError):
    """The bind host would expose the unauthenticated sync routes."""


def is_loopback_host(host: str) -> bool:
    """True only for a loopback IP literal or the name ``localhost``."""
    candidate = host.strip().strip("[]").lower()
    if candidate in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def tailnet_trusted(environ: Mapping[str, str] | None = None) -> bool:
    """Read ``MDT_SYNC_TRUST_TAILNET``: exactly ``1`` trusts, unset or ``0`` does not."""
    env = os.environ if environ is None else environ
    raw = env.get(TRUST_TAILNET_ENV, "")
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise SyncBindRefused(f"{TRUST_TAILNET_ENV}={raw!r} is not understood; use 1 or 0.")


def assert_sync_bind_allowed(host: str, environ: Mapping[str, str] | None = None) -> None:
    """Refuse a non-loopback bind for a process serving ``/api/v1/sync/*``."""
    if is_loopback_host(host) or tailnet_trusted(environ):
        return
    raise SyncBindRefused(
        f"refusing to serve /api/v1/sync/* on non-loopback bind {host!r}: the "
        f"sync hub is unauthenticated while enrollment is OBSERVE-only, see "
        f"{ADR_REFERENCE}. Bind 127.0.0.1 and expose it with `tailscale "
        f"serve`, or set {TRUST_TAILNET_ENV}=1 to accept the exposure explicitly."
    )


__all__ = [
    "ADR_REFERENCE",
    "TRUST_TAILNET_ENV",
    "SyncBindRefused",
    "assert_sync_bind_allowed",
    "is_loopback_host",
    "tailnet_trusted",
]
