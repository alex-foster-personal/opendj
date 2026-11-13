"""Spoke client for the hub lyrics pair upload-presign endpoint (LYRICS-13411).

A spoke without direct R2 credentials registers a lyric stem pair by asking the
hub for short-lived PUT URLs keyed by content digest, uploading the bytes, then
asking again so the hub's HEAD confirms every object landed. The spoke never
holds bucket credentials or bucket keys.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.cloud.asset_store import AssetStoreError, validate_content_hash
from apps.sync_hub.transport import API_PREFIX, HubTransport

LYRICS_PAIR_UPLOAD_PRESIGN_PATH = f"{API_PREFIX}/lyrics-pair/upload-presign"
LYRICS_PAIR_PRESIGN_FAILED = "LYRICS_PAIR_PRESIGN_FAILED"
LYRICS_PAIR_DIGEST_MISMATCH = "LYRICS_PAIR_DIGEST_MISMATCH"
LYRICS_PAIR_HUB_UNAVAILABLE = "LYRICS_PAIR_HUB_UNAVAILABLE"
PRESIGNED_PUT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class PairUploadTarget:
    """One file's answer from the hub: already in R2, or a PUT URL to fill it."""

    content_hash: str
    present: bool
    url: str | None


def fetch_lyrics_pair_upload_presign(
    transport: HubTransport,
    *,
    machine_id: str,
    stable_id: str,
    files: list[tuple[str, int]],
) -> list[PairUploadTarget]:
    """Ask the hub which of ``files`` (digest, size) are present and presign PUTs for the rest."""
    body = transport.post(
        LYRICS_PAIR_UPLOAD_PRESIGN_PATH,
        {
            "machine_id": machine_id,
            "stable_id": stable_id,
            "files": [{"content_hash": digest, "size_bytes": size} for digest, size in files],
        },
    )
    return _parse_targets(body, [digest for digest, _size in files])


def _parse_targets(body: dict[str, Any], wanted: list[str]) -> list[PairUploadTarget]:
    """Validate the hub answer names exactly the requested digests, each present or with a URL."""
    rows = body.get("files")
    if not isinstance(rows, list):
        raise TypeError(f"{LYRICS_PAIR_UPLOAD_PRESIGN_PATH} returned no files list")
    targets: list[PairUploadTarget] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"{LYRICS_PAIR_UPLOAD_PRESIGN_PATH} returned a non-object file row")
        digest = row.get("content_hash")
        present = row.get("present")
        url = row.get("url")
        if not isinstance(digest, str) or not isinstance(present, bool):
            raise TypeError(f"{LYRICS_PAIR_UPLOAD_PRESIGN_PATH} returned a malformed file row")
        if not present and (not isinstance(url, str) or not url):
            raise RuntimeError(f"{LYRICS_PAIR_UPLOAD_PRESIGN_PATH} returned no presigned target for {digest}")
        targets.append(PairUploadTarget(content_hash=digest, present=present, url=url if not present else None))
    if sorted(t.content_hash for t in targets) != sorted(wanted):
        raise RuntimeError(f"{LYRICS_PAIR_UPLOAD_PRESIGN_PATH} answered for other digests than requested")
    return targets


def put_presigned_file(url: str, path: Path, content_hash: str, *, timeout_s: float = PRESIGNED_PUT_TIMEOUT_S) -> None:
    """PUT ``path`` to a hub-minted presigned URL; any non-2xx raises :class:`AssetStoreError`."""
    digest = validate_content_hash(content_hash)
    body = Path(path).read_bytes()
    request = urllib.request.Request(url, data=body, method="PUT")
    request.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status >= 400:
                raise AssetStoreError(f"presigned PUT failed with HTTP {response.status} for {digest}")
    except urllib.error.HTTPError as exc:
        raise AssetStoreError(f"presigned PUT failed with HTTP {exc.code} for {digest}") from exc
    except urllib.error.URLError as exc:
        raise AssetStoreError(f"presigned PUT unreachable for {digest}: {exc.reason}") from exc
    except OSError as exc:
        raise AssetStoreError(f"presigned PUT unreachable for {digest}: {exc}") from exc


__all__ = [
    "LYRICS_PAIR_DIGEST_MISMATCH",
    "LYRICS_PAIR_HUB_UNAVAILABLE",
    "LYRICS_PAIR_PRESIGN_FAILED",
    "LYRICS_PAIR_UPLOAD_PRESIGN_PATH",
    "PairUploadTarget",
    "fetch_lyrics_pair_upload_presign",
    "put_presigned_file",
]
