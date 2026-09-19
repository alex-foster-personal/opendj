"""Spoke client for hub stem index and bundle-presign endpoints (ADR-0051)."""
from __future__ import annotations

from typing import Any

from apps.cloud.stem_index import StemAssetIndex
from apps.sync_hub.transport import API_PREFIX, HubTransport

STEM_INDEX_PATH = f"{API_PREFIX}/stems/index"
STEM_BUNDLE_PRESIGN_PATH = f"{API_PREFIX}/stems/bundle-presign"


def fetch_hub_stem_index(transport: HubTransport, *, machine_id: str) -> StemAssetIndex:
    """Return the hub's published stem bundle index for ``machine_id``."""
    body = transport.get(STEM_INDEX_PATH, {"machine_id": machine_id})
    index = body.get("index")
    if not isinstance(index, dict):
        raise RuntimeError(f"{STEM_INDEX_PATH} returned no index object")
    return {str(sid): dict(files) for sid, files in index.items() if isinstance(files, dict)}


def fetch_hub_bundle_presign(
    transport: HubTransport,
    *,
    machine_id: str,
    stable_id: str,
) -> dict[str, Any]:
    """Return presigned GET metadata for one indexed bundle."""
    body = transport.get(
        STEM_BUNDLE_PRESIGN_PATH,
        {"machine_id": machine_id, "stable_id": stable_id},
    )
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError(f"{STEM_BUNDLE_PRESIGN_PATH} returned no files list")
    return body


__all__ = [
    "STEM_BUNDLE_PRESIGN_PATH",
    "STEM_INDEX_PATH",
    "fetch_hub_bundle_presign",
    "fetch_hub_stem_index",
]
