"""Metadata-only publishing for a finalized recorded set.

Recorded MP3 segments remain personal-review-only. This module publishes only
the stored timeline and transition metadata through the configured HTTPS web UI.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from .manifest import read_manifest, write_manifest

SET_SHARE_BASE_URL_ENV = "MUSIC_DJ_SET_SHARE_BASE_URL"
SHARE_AUTH_ENV = "MUSIC_DJ_SHARE_AUTH"


class SetShareError(ValueError):
    """A requested metadata share cannot be safely published."""


def configured_share_base_url(environ: Mapping[str, str] = os.environ) -> str:
    """Return the explicit HTTPS base URL for the browser-only share view."""
    raw_url = environ.get(SET_SHARE_BASE_URL_ENV, "").strip()
    if not raw_url:
        raise SetShareError(f"{SET_SHARE_BASE_URL_ENV} must be configured before sharing")
    parsed = urlsplit(raw_url)
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
    if environ.get(SHARE_AUTH_ENV, "").strip().lower() == "token":
        raise SetShareError(
            f"{SHARE_AUTH_ENV}=token cannot issue set links without exposing the token"
        )
    return urlunsplit(("https", parsed.netloc, "", "", ""))


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
    "SHARE_AUTH_ENV",
    "SetShareError",
    "configured_share_base_url",
    "publish_metadata_only",
    "share_url",
]
