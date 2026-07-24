#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.34"]
# ///
"""Move stem bundles from Cloudflare R2 to the bifrost2 asset store.

A SEPARATE JOB ON PURPOSE. The farm's business is keeping L4s busy; long-haul
archival is not on that critical path and must never share a process with it.
Splitting them means a GPU run cannot be slowed, stalled or failed by a
transfer, and the archive can be caught up later, from any machine, at any
time, without a GPU.

Leaving bundles on R2 is a legitimate end state. R2 is cheap, durable and has
no egress fee, so this job is an OPTION, not a step the farm is incomplete
without.

RESUMABILITY comes from comparing two listings rather than from a cursor file:
what R2 holds, minus what bifrost2 already holds, is the work. That makes
every run idempotent and every interruption free - kill it at any moment and
the next run recomputes exactly the remaining set. There is no state to
corrupt and nothing to clean up but a scratch directory.

ORDER OF OPERATIONS per bundle, and why: download to scratch -> push to
bifrost2 -> verify byte sizes -> delete scratch -> (optionally) delete from
R2. The R2 delete is LAST and gated on the size check, so an interrupted or
short transfer can only ever cost a repeat, never the only copy.

Requirements (mini-PRD):
  ✔︎ ✅ the work set is (R2 bundles) - (bifrost2 bundles), recomputed per run.
    [if] the job is killed and re-run [then] it resumes with no duplicate work
    [if] every bundle is already archived [then] it does nothing and exits 0
  ✔︎ ✅ a bundle is deleted from R2 only after bifrost2 reports matching sizes.
    [if] any size differs [then ⛔️] raise, keep the R2 copy, report the bundle
  ✔︎ ✅ --delete-after is opt-in; the default leaves R2 untouched.
    [if] --delete-after is absent [then] no R2 object is ever deleted
  ✔︎ ✅ scratch never accumulates: one bundle at a time, removed after push.
    [if] interrupted mid-download [then] the next run rebuilds that bundle

Run (R2_* come from Doppler; boto3 is inline, so no repo venv needed):
  doppler run --project general --config dev_personal -- \
    uv run scripts/r2_stem_sync.py --preset htdemucs-ov0.1 --dry-run
  doppler run --project general --config dev_personal -- \
    uv run scripts/r2_stem_sync.py --preset htdemucs-ov0.1 --limit 20

-Claude
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.b2listing import remote_dirs, remote_sizes
from scripts.b2store import Store

R2_ENV_KEYS: tuple[str, str, str] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)
DEFAULT_BUCKET: str = "music-dj-audio"
R2_STEM_PREFIX: str = "stems"
STEMS_REMOTE_ROOT: str = "stems"  # -> D:/asset-store/stems/<preset>/<stable_id>
SCRATCH: Path = Path(__file__).resolve().parent.parent / ".tmp/r2-sync"


@dataclass
class Bundle:
    """One stable_id's objects in R2."""

    stable_id: str
    keys: dict[str, int]  # key -> size

    @property
    def total_bytes(self) -> int:
        return sum(self.keys.values())


def r2_client() -> Any:
    """S3 client for R2. Fails naming the missing key, not with a bare
    NoCredentialsError from three frames deeper."""
    import boto3
    from botocore.config import Config

    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"error: {missing} not in the environment. Run under:\n"
            "  doppler run --project general --config dev_personal -- "
            "uv run scripts/r2_stem_sync.py ...\n"
            "If those secrets do not exist, R2 must first be enabled on the "
            "Cloudflare account and an R2 API token created."
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def list_r2_bundles(client: Any, bucket: str, preset: str) -> list[Bundle]:
    """Every bundle under stems/<preset>/, grouped by stable_id.

    Paginated because a full gap is ~989 bundles x 5 objects, comfortably past
    the 1000-key cap on a single ListObjectsV2 response. Silently taking the
    first page would make the sync job under-report its own backlog.
    """
    prefix = f"{R2_STEM_PREFIX}/{preset}/"
    grouped: dict[str, dict[str, int]] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            rest = key[len(prefix):]
            if "/" not in rest:
                continue  # not <stable_id>/<file>, so not part of a bundle
            stable_id = rest.split("/", 1)[0]
            grouped.setdefault(stable_id, {})[key] = item["Size"]
    return [Bundle(sid, keys) for sid, keys in sorted(grouped.items())]


def archived_ids(store: Store, preset: str) -> set[str]:
    """What bifrost2 already holds for this preset.

    ONE `dir` of the preset root rather than a call per bundle: at ~989
    bundles the SSH handshakes would cost more than the transfers.

    A missing directory means nothing archived yet, which is a normal first
    run, not an error - hence the narrow catch on that one message.
    """
    try:
        listing = store.ls(f"{STEMS_REMOTE_ROOT}/{preset}")
    except RuntimeError as exc:
        if "File Not Found" in str(exc) or "cannot find" in str(exc).lower():
            return set()
        raise
    return remote_dirs(listing)


def _download(client: Any, bucket: str, bundle: Bundle, into: Path) -> dict[str, int]:
    """Pull one bundle's objects into a scratch dir. Returns {filename: bytes}."""
    into.mkdir(parents=True, exist_ok=True)
    local: dict[str, int] = {}
    for key, size in sorted(bundle.keys.items()):
        name = key.rsplit("/", 1)[-1]
        target = into / name
        client.download_file(bucket, key, str(target))
        got = target.stat().st_size
        if got != size:
            raise RuntimeError(
                f"r2 download short for {key}: got {got} bytes, expected {size}"
            )
        local[name] = got
    return local


def sync_bundle(
    client: Any,
    store: Store,
    bucket: str,
    preset: str,
    bundle: Bundle,
    delete_after: bool,
) -> float:
    """Download, push, verify, clean. Returns transfer seconds.

    The scratch dir is removed in a finally, so an exception anywhere leaves
    no partial bundle behind for the next run to trip over.
    """
    scratch = SCRATCH / bundle.stable_id
    if scratch.exists():
        shutil.rmtree(scratch)
    started = time.perf_counter()
    try:
        local_sizes = _download(client, bucket, bundle, scratch)
        store.save(str(scratch), f"{STEMS_REMOTE_ROOT}/{preset}")
        remote = remote_sizes(
            store.ls(f"{STEMS_REMOTE_ROOT}/{preset}/{bundle.stable_id}")
        )
        if remote != local_sizes:
            raise RuntimeError(
                f"bifrost2 copy of {bundle.stable_id} does not match R2: "
                f"remote {remote} vs local {local_sizes}"
            )
        if delete_after:
            # Only reachable after the size check above, so the R2 copy is
            # never the last one standing when this runs.
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in sorted(bundle.keys)]},
            )
        return time.perf_counter() - started
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/r2_stem_sync.py",
        description="Resumable R2 -> bifrost2 stem archive sync",
    )
    parser.add_argument("--preset", required=True,
                        help="preset tag whose bundles to move, e.g. htdemucs-ov0.1")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=0,
                        help="cap bundles this run (0 = all outstanding)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the outstanding set and move nothing")
    parser.add_argument(
        "--delete-after", action="store_true",
        help="delete each bundle from R2 once bifrost2 verifies it. Off by "
             "default: keeping both copies costs little and R2 has no egress "
             "fee, so deleting is a choice, not a cleanup step.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    client = r2_client()

    bundles = list_r2_bundles(client, args.bucket, args.preset)
    if not bundles:
        print(
            f"nothing in s3://{args.bucket}/{R2_STEM_PREFIX}/{args.preset}/ "
            "- either the farm has not run with --stems-dest r2, or every "
            "bundle was already moved and deleted."
        )
        return 0

    store = Store()
    store.df()  # fail here if the tailnet is down, before any download
    done = archived_ids(store, args.preset)
    outstanding = [b for b in bundles if b.stable_id not in done]
    batch = outstanding[: args.limit] if args.limit > 0 else outstanding

    total_gb = sum(b.total_bytes for b in batch) / 1e9
    print(
        f"r2={len(bundles)} bundles, bifrost2 already has {len(done)}, "
        f"outstanding={len(outstanding)}, this run={len(batch)} "
        f"({total_gb:.2f} GB)"
    )
    if not batch:
        print("nothing to do: bifrost2 is caught up")
        return 0
    if args.dry_run:
        for bundle in batch[:10]:
            print(f"  would move {bundle.stable_id} "
                  f"({bundle.total_bytes / 1e6:.0f} MB, {len(bundle.keys)} objects)")
        if len(batch) > 10:
            print(f"  ... and {len(batch) - 10} more")
        return 0

    moved = 0
    moved_bytes = 0
    transfer_s = 0.0
    failures: list[tuple[str, str]] = []
    for index, bundle in enumerate(batch, start=1):
        try:
            elapsed = sync_bundle(
                client, store, args.bucket, args.preset, bundle, args.delete_after
            )
        except Exception as exc:
            failures.append((bundle.stable_id, f"{type(exc).__name__}: {exc}"))
            print(f"[{index}/{len(batch)}] FAIL {bundle.stable_id}: {exc}",
                  flush=True)
            continue
        moved += 1
        moved_bytes += bundle.total_bytes
        transfer_s += elapsed
        print(
            f"[{index}/{len(batch)}] ok {bundle.stable_id} "
            f"{bundle.total_bytes / 1e6:.0f}MB in {elapsed:.1f}s "
            f"({bundle.total_bytes / 1e6 / elapsed:.1f} MB/s)",
            flush=True,
        )

    rate = moved_bytes / 1e6 / transfer_s if transfer_s > 0 else 0.0
    print(
        f"\ndone: {moved} moved ({moved_bytes / 1e9:.2f} GB at {rate:.1f} MB/s), "
        f"{len(failures)} failed"
    )
    for stable_id, reason in failures:
        print(f"  {stable_id}: {reason}")
    if failures:
        print(
            "re-run to retry: the outstanding set is recomputed from the two "
            "listings, so only the failures remain.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
