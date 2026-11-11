#!/usr/bin/env python3
"""Background worker for ``anlz.cache_rebuild`` jobs.

Reads a JSON payload from ``--payload`` and rebuilds schema-versioned entries
under ``(private path)/``. Emits JSON-lines progress on stdout for the
engine jobs runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

EmitFn = Any


def _emit_factory() -> EmitFn:
    def emit(progress: float, message: str, **extra: Any) -> None:
        line = json.dumps(
            {"progress": round(float(progress), 4), "message": message, **extra},
            sort_keys=True,
        )
        print(line, flush=True)

    return emit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: invalid payload JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("error: payload must be a JSON object")

    # Env contract before any apps imports (paths bind at import time).
    data_dir_raw = payload.get("data_dir")
    if not isinstance(data_dir_raw, str) or not data_dir_raw.strip():
        raise SystemExit("error: payload needs absolute data_dir")
    data_dir = Path(data_dir_raw)
    if not data_dir.is_absolute():
        raise SystemExit(f"error: data_dir must be absolute; got {data_dir_raw!r}")
    os.environ["MDT_DATA_DIR"] = str(data_dir)
    crate_root = payload.get("mdt_crate_root")
    if isinstance(crate_root, str) and crate_root.strip():
        os.environ["MDT_LIBRARY_MODE"] = "remote"
        os.environ["MDT_CRATE_ROOT"] = crate_root.strip()

    from apps.shared.library_mode import apply_library_env
    from apps.shared import platform_paths

    apply_library_env()
    platform_paths.refresh_share_root()

    from apps.analysis import anlz_cache_job
    from apps.webui.server.rb_vendor_pkg import anlz_cache_rebuild

    emit = _emit_factory()
    try:
        parsed_data_dir, points, stable_ids = anlz_cache_job.parse_payload(payload)
        anlz_cache_rebuild.run_rebuild(
            data_dir=parsed_data_dir,
            points=points,
            stable_ids=stable_ids,
            on_progress=emit,
        )
    except (anlz_cache_job.AnlzCacheRebuildPayloadError, RuntimeError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    emit(1.0, "succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
