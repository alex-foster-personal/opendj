"""The ``anlz.cache_rebuild`` engine job kind: background ANLZ JSON cache writes.

Registers argv + reconcile hooks for the jobs chassis. This module imports
nothing from ``apps.engine_core`` -- the engine composition root wires it in
from :mod:`apps.engine_core.app`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import sys
from pathlib import Path
from typing import Any

from apps.shared import platform_paths
from apps.shared.runtime_policy import (
    ANLZ_POINTS_DEFAULT,
    ANLZ_POINTS_MAX,
    ANLZ_POINTS_MIN,
)

JOB_KIND: str = "anlz.cache_rebuild"
WORKER_SCRIPT: str = "scripts/anlz_cache_rebuild_worker.py"
# Payloads travel through a file, never argv: a library-wide rebuild names
# thousands of stable IDs and one argv string that long hits E2BIG (#12155).
PAYLOAD_DIR_NAME: str = "anlz-cache-jobs"


class AnlzCacheRebuildPayloadError(ValueError):
    """The enqueue payload cannot describe a cache rebuild run."""


def _library_data_dir(raw: object) -> Path:
    if isinstance(raw, Path):
        return raw.expanduser().resolve(strict=False)
    if not isinstance(raw, str) or not raw.strip():
        raise AnlzCacheRebuildPayloadError(f"data_dir must be a non-empty path string; got {raw!r}")
    data_dir = Path(raw.strip())
    if not data_dir.is_absolute():
        raise AnlzCacheRebuildPayloadError(f"data_dir must be absolute; got {raw!r}")
    return data_dir


def _parse_points(raw: object) -> int:
    points = ANLZ_POINTS_DEFAULT if raw is None else raw
    if not isinstance(points, int):
        raise AnlzCacheRebuildPayloadError(
            f"points must be an int between {ANLZ_POINTS_MIN} and {ANLZ_POINTS_MAX}; got {points!r}"
        )
    if not ANLZ_POINTS_MIN <= points <= ANLZ_POINTS_MAX:
        raise AnlzCacheRebuildPayloadError(
            f"points must be between {ANLZ_POINTS_MIN} and {ANLZ_POINTS_MAX}; got {points}"
        )
    return points


def _parse_stable_ids(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise AnlzCacheRebuildPayloadError(f"stable_ids must be a non-empty list when provided; got {raw!r}")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AnlzCacheRebuildPayloadError(f"stable_ids entries must be non-empty strings; got {item!r}")
        sid = item.strip()
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    if not out:
        raise AnlzCacheRebuildPayloadError("stable_ids must name at least one track")
    return sorted(out)


def list_targets(data_dir: Path) -> list[str]:
    """Rekordbox-mapped tracks that still have AnalysisDataPath on disk."""
    state_path = data_dir / "state" / "state.db"
    master_path = data_dir / "master.plain.db"
    if not state_path.is_file() or not master_path.is_file():
        return []
    state = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    master = sqlite3.connect(f"file:{master_path}?mode=ro", uri=True)
    try:
        mappings = state.execute(
            "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = 'rekordbox'"
        ).fetchall()
        targets: list[str] = []
        for stable_id, vendor_id in mappings:
            row = master.execute(
                "SELECT AnalysisDataPath FROM djmdContent WHERE ID = ? AND +rb_local_deleted = 0",
                (str(vendor_id),),
            ).fetchone()
            if row and row[0]:
                targets.append(str(stable_id))
        return sorted(targets)
    finally:
        master.close()
        state.close()


def parse_payload(
    payload: dict[str, Any],
    *,
    default_data_dir: Path | None = None,
) -> tuple[Path, int, list[str]]:
    data_dir = _library_data_dir(payload.get("data_dir", default_data_dir))
    points = _parse_points(payload.get("points"))
    stable_ids = _parse_stable_ids(payload.get("stable_ids"))
    if stable_ids is None:
        stable_ids = list_targets(data_dir)
    return data_dir, points, stable_ids


def canonical_payload(
    payload: dict[str, Any],
    *,
    default_data_dir: Path,
) -> dict[str, Any]:
    """Normalize the enqueue body; expand omitted ``stable_ids`` to every target."""
    data_dir, points, stable_ids = parse_payload(payload, default_data_dir=default_data_dir)
    out: dict[str, Any] = {
        "data_dir": str(data_dir),
        "points": points,
        "stable_ids": stable_ids,
    }
    crate_root = payload.get("mdt_crate_root")
    if isinstance(crate_root, str) and crate_root.strip():
        out["mdt_crate_root"] = crate_root.strip()
    return out


def build_argv(payload: dict[str, Any]) -> list[str]:
    data_dir, points, stable_ids = parse_payload(payload)
    wire: dict[str, Any] = {
        "data_dir": str(data_dir),
        "points": points,
        "stable_ids": stable_ids,
    }
    crate_root = payload.get("mdt_crate_root")
    if isinstance(crate_root, str) and crate_root.strip():
        wire["mdt_crate_root"] = crate_root.strip()
    payload_file = write_payload_file(data_dir, wire)
    return [
        sys.executable,
        str(Path(platform_paths.PROJECT_ROOT) / WORKER_SCRIPT),
        "--payload-file",
        str(payload_file),
    ]


def write_payload_file(data_dir: Path, wire: dict[str, Any]) -> Path:
    """Write the worker payload to a content-addressed file under the library state dir."""
    text = json.dumps(wire, sort_keys=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    directory = data_dir / "state" / PAYLOAD_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "JOB_KIND",
    "PAYLOAD_DIR_NAME",
    "WORKER_SCRIPT",
    "AnlzCacheRebuildPayloadError",
    "build_argv",
    "canonical_payload",
    "list_targets",
    "parse_payload",
    "write_payload_file",
]
