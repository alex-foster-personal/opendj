#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.34"]
# ///
"""Re-key legacy ``stems/<preset>/<stable_id>/<file>`` R2 objects to the one
content-addressed layout (cloudsync issue #1452).

The repo used to hold two R2 layouts; content addressing won and this job
moves objects already written under the losing ``stems/...`` path layout to
``assets/<sha256[:2]>/<sha256>``. It reads each legacy body, derives the
content address (a property of the body), writes the new object, verifies the
new copy reads back byte-identical, and only then (with ``--delete-after``)
removes the legacy key.

``--delete-after`` is destructive in a way that is only safe if the old ->
new mapping survives the deletion: content-addressed keys carry no
preset/stable_id/filename, so once the legacy ``stems/...`` key is gone the
rekey ledger is the only record of which bundle an object belongs to. The job
therefore writes a durable ledger object (``--ledger-key``) BEFORE honoring
``--delete-after`` and refuses to delete if the ledger did not persist.

The core logic lives in ``apps.cloud.legacy_stems_migration`` and is tested
against a dict-backed store; this file is only the R2 adapter + CLI.

Run (R2_* from Doppler; boto3 inline so no repo venv needed):
  doppler run --project general --config dev_personal -- \
    uv run scripts/rekey_stems_layout.py --dry-run
  doppler run --project general --config dev_personal -- \
    uv run scripts/rekey_stems_layout.py --limit 20
  doppler run --project general --config dev_personal -- \
    uv run scripts/rekey_stems_layout.py --delete-after --ledger-key \\
    _migrations/rekey-<preset-tag>-<stable_id>.json

--dry-run reads and hashes every body (the plan is honest) but writes and
deletes nothing. Without ``--delete-after`` the job only uploads the new
copies and leaves the legacy keys in place, which is safe to re-run.

-Claude
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.cloud.legacy_stems_migration import LEGACY_STEMS_PREFIX, migrate_legacy_stems
from scripts.r2_stems import DEFAULT_BUCKET, R2_ENV_KEYS, r2_client


class _Boto3R2Store:
    """The narrow migration surface over boto3's S3 client (R2)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def get_object(self, bucket: str, key: str) -> bytes | None:
        try:
            return self._client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except self._client.exceptions.NoSuchKey:
            return None

    def put_object(self, bucket: str, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=body)

    def delete_object(self, bucket: str, key: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/rekey_stems_layout.py",
        description="Re-key legacy stems/<preset>/<stable_id>/<file> R2 "
        "objects onto the content-addressed asset layout",
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default=LEGACY_STEMS_PREFIX,
                        help="legacy key prefix to scan (default: stems/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="read + hash every body, write/delete nothing")
    parser.add_argument("--delete-after", action="store_true",
                        help="delete each legacy key once its new object "
                             "verifies byte-identical AND a durable rekey "
                             "ledger recording the old -> new mapping has "
                             "been written (see --ledger-key). Off by "
                             "default: a re-key that re-uploads is cheap, a "
                             "delete that was premature or unrecorded is not.")
    parser.add_argument("--ledger-key",
                        help="R2 key for the durable rekey ledger object. "
                             "REQUIRED when --delete-after is used without "
                             "--dry-run: content-addressed keys carry no "
                             "preset/stable_id/filename, so the ledger is "
                             "the only record of the bundle mapping once the "
                             "legacy keys are deleted. Must lie OUTSIDE the "
                             "scanned prefix (default stems/) so a later run "
                             "cannot re-scan and delete it. Reusing the key "
                             "across --limit batches extends the ledger "
                             "(entries union by old key); an existing ledger "
                             "for a different bucket or layout refuses the "
                             "delete.")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap objects re-keyed this run (0 = all)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        print(
            f"error: {missing} not in the environment. Run under:\n"
            "  doppler run --project general --config dev_personal -- "
            "uv run scripts/rekey_stems_layout.py ...",
            file=sys.stderr,
        )
        return 1

    store = _Boto3R2Store(r2_client("scripts/rekey_stems_layout.py"))
    legacy_keys = store.list_objects(args.bucket, args.prefix)
    if not legacy_keys:
        print(f"no objects under {args.bucket}/{args.prefix}; nothing to re-key")
        return 0
    print(f"found {len(legacy_keys)} legacy object(s) under "
          f"{args.bucket}/{args.prefix}")

    limited = legacy_keys[: args.limit] if args.limit > 0 else None
    result = migrate_legacy_stems(
        store, args.bucket,
        dry_run=args.dry_run, delete_after=args.delete_after,
        prefix=args.prefix, keys=limited, ledger_key=args.ledger_key,
    )
    mode = "dry-run (nothing written or deleted)" if args.dry_run else "live"
    print(f"{mode}: migrated={result.migrated} deleted={result.deleted} "
          f"failures={len(result.failures)}")
    if result.ledger_key:
        print(f"ledger written to {result.ledger_key}")
    for old_key, new_key in result.rekeyed[:20]:
        print(f"  {old_key} -> {new_key}")
    if len(result.rekeyed) > 20:
        print(f"  ... and {len(result.rekeyed) - 20} more")
    for failure in result.failures[:10]:
        print(f"  FAILED {failure}", file=sys.stderr)
    if result.failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
