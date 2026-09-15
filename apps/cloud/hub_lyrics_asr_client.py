"""Spoke client for hub ASR lyric transcript presign endpoints (ADR-0049)."""
from __future__ import annotations

from typing import Any

from apps.sync_hub.transport import API_PREFIX, HubTransport

LYRICS_ASR_PRESIGN_PATH = f"{API_PREFIX}/lyrics-asr"


def fetch_lyrics_asr_presign(
    transport: HubTransport,
    *,
    machine_id: str,
    stable_id: str,
) -> dict[str, Any]:
    """Return presigned GET metadata for one ASR transcript JSON object."""
    body = transport.get(
        f"{LYRICS_ASR_PRESIGN_PATH}/{stable_id}",
        {"machine_id": machine_id},
    )
    url = body.get("url")
    content_hash = body.get("content_hash")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"{LYRICS_ASR_PRESIGN_PATH} returned no url")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise RuntimeError(f"{LYRICS_ASR_PRESIGN_PATH} returned no content_hash")
    return body


__all__ = ["LYRICS_ASR_PRESIGN_PATH", "fetch_lyrics_asr_presign"]
