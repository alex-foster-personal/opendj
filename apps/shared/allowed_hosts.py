"""The public-hostname allowlist: its env var, its hostname grammar, and its parser.

Supersedes: the allowlist parser in ``apps.webui.server.request_guard``, which now
imports it from here.

Lifted out of ``apps.webui.server.request_guard`` (round 12 of the SMARTEST-CI
counter) because ``apps.sync_hub.hub_deploy`` renders the same variable into a
service unit, and a domain package importing the web UI for a parser is the
edge that put ``sync_hub`` inside the 31-scope import cycle. ``request_guard``
still re-exports these names; the middleware that USES them stays there.
"""

from __future__ import annotations

import re

ALLOWED_HOSTS_ENV = "MUSIC_DJ_ALLOWED_HOSTS"
HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)


def parse_allowed_hosts(
    raw_value: str | None,
    *,
    env_name: str = ALLOWED_HOSTS_ENV,
) -> list[str]:
    """Bare hostnames from ``MUSIC_DJ_ALLOWED_HOSTS``. Unset means loopback only."""
    if raw_value is None or raw_value.strip() == "":
        return []
    hosts = [part.strip() for part in raw_value.split(",") if part.strip()]
    if not hosts:
        raise ValueError(f"{env_name} was set but named no hostname")
    for host in hosts:
        if not HOSTNAME_RE.fullmatch(host):
            raise ValueError(f"{env_name} must list bare hostnames, got {host!r}")
    return hosts


__all__ = ["ALLOWED_HOSTS_ENV", "HOSTNAME_RE", "parse_allowed_hosts"]
