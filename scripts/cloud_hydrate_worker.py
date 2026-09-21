#!/usr/bin/env python3
"""Background worker for ``cloud.hydrate`` jobs (CLOUDSYNC-10).

Reads a JSON payload from ``--payload`` and downloads one asset into the
local cache via :func:`apps.cloud.hydration.run_hydrate_job`. Emits JSON-lines
progress on stdout for the engine jobs runner.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from apps.cloud import asset_store, hydration
from apps.cloud import job as cloud_job
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.shared.state import db as state_db


def _emit(progress: float, message: str, **extra: Any) -> None:
    line = json.dumps(
        {"progress": round(float(progress), 4), "message": message, **extra},
        sort_keys=True,
    )
    print(line, flush=True)


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

    try:
        stable_id, asset_kind, machine_id, data_dir = cloud_job.parse_payload(
            payload
        )
    except cloud_job.CloudHydratePayloadError as exc:
        raise SystemExit(f"error: {exc}") from exc

    cfg = CloudConfig.from_env()
    s3 = asset_store.boto3_asset_client(cfg)
    state_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(state_path)
    try:
        hydration.run_hydrate_job(
            conn,
            cfg,
            s3,
            stable_id=stable_id,
            asset_kind=asset_kind,
            machine_id=machine_id,
            data_dir=data_dir,
            on_progress=_emit,
        )
        conn.commit()
    except HydrationError as exc:
        raise SystemExit(f"error: {exc}") from exc
    finally:
        conn.close()
    _emit(1.0, "succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
