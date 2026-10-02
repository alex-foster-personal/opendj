"""The ``cloud.hydrate`` engine job kind: background asset hydration.

Registers argv + reconcile hooks for the jobs chassis. This module imports
nothing from ``apps.engine_core`` -- the engine composition root wires it in
from :mod:`apps.engine_core.app`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apps.cloud import hydration
from apps.cloud import policy as cloud_policy
from apps.cloud.eviction import HydrationError
from apps.shared.stable_id import is_safe_stable_id_segment

JOB_KIND: str = "cloud.hydrate"
WORKER_SCRIPT: str = "scripts/cloud_hydrate_worker.py"


class CloudHydratePayloadError(ValueError):
    """The enqueue payload cannot describe a hydration run."""


def parse_payload(payload: dict[str, Any]) -> tuple[str, str, str, Path]:
    stable_id = payload.get("stable_id")
    if not isinstance(stable_id, str) or not stable_id.strip():
        raise CloudHydratePayloadError(
            f"cloud.hydrate payload needs non-empty stable_id; got {stable_id!r}"
        )
    stable_id = stable_id.strip()
    if not is_safe_stable_id_segment(stable_id):
        raise CloudHydratePayloadError(
            f"unusable stable_id {stable_id!r}: must be 1-128 URL-safe "
            "identifier characters"
        )
    asset_kind = payload.get("asset_kind")
    if not isinstance(asset_kind, str) or asset_kind not in hydration.ASSET_KINDS:
        raise CloudHydratePayloadError(
            f"asset_kind must be one of {list(hydration.ASSET_KINDS)}; "
            f"got {asset_kind!r}"
        )
    machine_id = payload.get("machine_id")
    if not isinstance(machine_id, str) or not machine_id.strip():
        raise CloudHydratePayloadError(
            f"machine_id must be a non-empty string; got {machine_id!r}"
        )
    raw_dir = payload.get("data_dir")
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise CloudHydratePayloadError(
            f"data_dir must be a non-empty path string; got {raw_dir!r}"
        )
    data_dir = Path(raw_dir)
    if not data_dir.is_absolute():
        raise CloudHydratePayloadError(
            f"data_dir must be absolute; got {raw_dir!r}"
        )
    return stable_id, asset_kind, machine_id.strip(), data_dir


def build_argv(payload: dict[str, Any]) -> list[str]:
    stable_id, asset_kind, machine_id, data_dir = parse_payload(payload)
    from apps.shared import platform_paths

    # Absolute: the engine's cwd in the installed app is /, and the payload
    # now ships this script under payload/app/scripts (issue #3421).
    return [
        sys.executable,
        str(Path(platform_paths.PROJECT_ROOT) / WORKER_SCRIPT),
        "--payload",
        json.dumps(
            {
                "stable_id": stable_id,
                "asset_kind": asset_kind,
                "machine_id": machine_id,
                "data_dir": str(data_dir),
            },
            sort_keys=True,
        ),
    ]


def reconcile_from_disk(job: dict[str, Any]) -> str:
    try:
        stable_id, asset_kind, machine_id, data_dir = parse_payload(
            job.get("payload") or {}
        )
    except CloudHydratePayloadError:
        return "unknown"
    state_path = data_dir / "state" / "state.db"
    if not state_path.is_file():
        return "failed"
    conn = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        cache_dir = cloud_policy.artifact_cache_root(data_dir, asset_kind)
        source = hydration.resolve_playback_source(
            conn,
            stable_id,
            machine_id,
            asset_kind=asset_kind,
            cache_dir=cache_dir,
            cfg=None,
        )
    except HydrationError:
        return "failed"
    finally:
        conn.close()
    if source.origin in ("local", "cache"):
        return "succeeded"
    return "failed"


__all__ = [
    "JOB_KIND",
    "WORKER_SCRIPT",
    "CloudHydratePayloadError",
    "build_argv",
    "parse_payload",
    "reconcile_from_disk",
]
