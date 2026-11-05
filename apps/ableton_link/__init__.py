"""Ableton Link tempo sync through a Carabiner sidecar (LINK-10, LINK-11).

Open DJ never links the Ableton Link SDK (GPL-2.0-or-later) into its own
binary (LINK-05, decision D1 = sidecar). The user runs Carabiner (Deep
Symmetry, GPLv2), which joins the Link session on the LAN and exposes it as a
line protocol on ``127.0.0.1:17000``; :class:`CarabinerClient` speaks that
protocol. See ``(private path)``.
"""

from apps.ableton_link.carabiner import (
    CARABINER_PORT_ENV,
    DEFAULT_CARABINER_PORT,
    CarabinerClient,
    CarabinerUnavailable,
    LinkSnapshot,
    parse_status_line,
)

__all__ = [
    "CARABINER_PORT_ENV",
    "DEFAULT_CARABINER_PORT",
    "CarabinerClient",
    "CarabinerUnavailable",
    "LinkSnapshot",
    "parse_status_line",
]
