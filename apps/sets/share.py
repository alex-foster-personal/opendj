"""Metadata-only publishing for a finalized recorded set.

Recorded MP3 segments remain personal-review-only. This module publishes only
the stored timeline and transition metadata through the configured HTTPS web UI.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from .manifest import read_manifest, write_manifest

SET_SHARE_BASE_URL_ENV = "MUSIC_DJ_SET_SHARE_BASE_URL"


class SetShareError(ValueError):
    """A requested metadata share cannot be safely published."""


@dataclass(frozen=True)
class SetShareConfig:
    """The published HTTPS origin for metadata-only set history links."""

    base_url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url.strip())
        try:
            _port = parsed.port
        except ValueError as exc:
            raise SetShareError(
                f"{SET_SHARE_BASE_URL_ENV} must have a valid HTTPS port"
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise SetShareError(
                f"{SET_SHARE_BASE_URL_ENV} must be an HTTPS origin without "
                "credentials, query, or fragment"
            )
        object.__setattr__(self, "base_url", urlunsplit(("https", parsed.netloc, "", "", "")))

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] = os.environ) -> SetShareConfig | None:
        raw_url = environ.get(SET_SHARE_BASE_URL_ENV, "").strip()
        return cls(raw_url) if raw_url else None


def configured_share_base_url(
    config: SetShareConfig | None,
    *,
    share_host: str,
    share_auth: str,
) -> str:
    """Return a share origin only when it is guarded by the live share gate."""
    if config is None:
        raise SetShareError(f"{SET_SHARE_BASE_URL_ENV} must be configured before sharing")
    configured_host = urlsplit(config.base_url).hostname
    if not share_host or configured_host != share_host:
        raise SetShareError(f"{SET_SHARE_BASE_URL_ENV} hostname must match configured share host")
    if share_auth == "token":
        raise SetShareError("token share authentication cannot issue set links")
    return config.base_url


def share_url(session_id: str, base_url: str) -> str:
    """Build a path-contained public link for one validated session ID."""
    return f"{base_url}/sets/shared/{quote(session_id, safe='')}"


def publish_metadata_only(session_dir: Path) -> None:
    """Mark a finalized session as shared without publishing its MP3 segments."""
    manifest = read_manifest(session_dir)
    if manifest.ended_at is None:
        raise SetShareError("only finalized sets can be shared")
    manifest.share_state = "shared_cloud"
    write_manifest(session_dir, manifest)


__all__ = [
    "SET_SHARE_BASE_URL_ENV",
    "SetShareConfig",
    "SetShareError",
    "configured_share_base_url",
    "publish_metadata_only",
    "share_url",
]
