"""Modal GPU transport preflight shared by plan API and executor routing."""

from __future__ import annotations


def stems_transport_state() -> tuple[str, str | None]:
    """The build's GPU transport, and why it cannot be used (or None)."""
    from apps.stems.job import (
        DEFAULT_TRANSPORT,
        StemsJobPayloadError,
        resolve_transport,
    )

    try:
        transport = resolve_transport()
    except StemsJobPayloadError as exc:
        return DEFAULT_TRANSPORT, str(exc)
    if transport != "relay":
        return transport, None
    from apps.stems.relay.client import RelayUnavailable, identity_token, relay_base_url

    try:
        relay_base_url()
        identity_token()
    except RelayUnavailable as exc:
        return transport, str(exc)
    return transport, None


__all__ = ["stems_transport_state"]
