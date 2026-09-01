#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.34"]
# ///
"""Publish this machine's local stem bundles to Cloudflare R2.

THE THIRD RAIL. ``scripts/r2_stem_sync.py`` moves R2 -> bifrost2 and
``scripts/b2_stems_to_r2.py`` moves bifrost2 -> R2. Neither touches the Mac,
where the RoFormer library spike left 13 GB of bundles that exist in exactly
one place. This job is the missing local -> R2 direction, and it is what makes
R2 the center rather than a third scattered copy.

DISCOVERY IS NOT DUPLICATED. What counts as a bundle, which layout it is, and
which stable_id it belongs to are all decided by ``scripts/stem_inventory.py``
and imported from it. Two definitions of "a bundle" would drift, and the
migration would then move something the inventory never counted.

DIRECT UPLOAD, NOT PRESIGNED. Its sibling hands bifrost2 presigned URLs
because bifrost2 is a shared Windows box that must never hold R2 credentials.
Here the bytes, the credentials and the process are all on the same trusted
machine, so a presigned indirection would add a failure mode and buy nothing.

NOTHING LOCAL IS EVER DELETED, and there is deliberately no flag to. The
sibling job offers ``--delete-after`` because it is moving a copy between two
stores it owns. This one is publishing the ONLY copy of renders that cost GPU
hours, so deletion is not a mode this script should be able to be talked into.
Duplicates found along the way are reported for a human, never resolved here.

RESUMABLE with no cursor file: the work set is (local objects) - (R2 objects
of the same key AND size), recomputed every run. Kill it at any point and the
next run does exactly the remainder.

VERIFIED BY RE-LISTING, NOT BY STATUS CODES. After uploading, the bucket is
listed again and sizes compared, because a truncated PUT can still answer
HTTP 200. Size rather than checksum: boto3 switches to multipart above its
threshold, which makes the ETag a hash-of-hashes rather than the object's
MD5, so an ETag comparison would be sound for small objects and quietly wrong
for exactly the large ones most likely to truncate.

Requirements (mini-PRD):
  ✔︎ ✅ the work set is (local objects) - (R2 objects with matching size).
    [if] re-run after a full success [then] it uploads nothing and exits 0
    [if] killed mid-run [then] the next run resumes with no repeated uploads
    [if] an object exists in R2 at the wrong size [then] it is re-uploaded
  ✔︎ ✅ keys match the farm scheme, stems/<preset>/<stable_id>/<filename>.
    [if] a key would differ from modal_vocal_farm's [then ⛔️] raise
  ✔︎ ✅ no local file is ever deleted or modified.
    [if] any code path would unlink a source [then ⛔️] it does not exist
  ✔︎ ✅ uploads are confirmed against a fresh listing, not against HTTP codes.
    [if] a size differs after upload [then ⛔️] report it and exit non-zero
  ✔︎ ✅ every run appends what it did to a journal.
    [if] a run uploads objects [then] the journal gains one line per run

Run (R2_* come from Doppler; boto3 is inline, so no repo venv needed):
  uv run scripts/local_stems_to_r2.py --dry-run
  doppler run --project general --config dev_personal -- \
    uv run scripts/local_stems_to_r2.py --limit 5
  doppler run --project general --config dev_personal -- \
    uv run scripts/local_stems_to_r2.py

--dry-run inspects only the local side and needs no credentials.

-Claude
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.r2_stems import DEFAULT_BUCKET, list_r2_sizes, r2_client
from scripts.stem_inventory import (
    REPO_ROOT,
    Bundle,
    TrackResolver,
    default_roots,
    scan_roots,
)
from scripts.stem_inventory_report import duplicate_ids

UPLOAD_PARALLELISM: int = 8
JOURNAL: Path = REPO_ROOT / "data/state/stem-r2-migration.jsonl"
CONTENT_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".opus": "audio/opus",
    ".json": "application/json",
}


@dataclass(frozen=True)
class Upload:
    """One object to put. Size is captured up front so the verification pass
    compares against what was read, not against a file that may have been
    rewritten underneath the run."""

    key: str
    source: Path
    size: int

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES.get(self.source.suffix.lower(), "application/octet-stream")


#----- work set ---------------------------------------------------------------

def planned_uploads(bundles: list[Bundle]) -> list[Upload]:
    """Every object the publishable bundles would put, keys included.

    Non-publishable bundles are excluded here rather than filtered later so
    that no caller can accidentally hand an unkeyed bundle to the uploader.
    """
    planned: list[Upload] = []
    for bundle in bundles:
        if not bundle.is_publishable:
            continue
        for filename, source in sorted(bundle.files.items()):
            planned.append(
                Upload(bundle.key_for(filename), source, source.stat().st_size)
            )
    return planned


def outstanding(planned: list[Upload], remote: dict[str, int]) -> list[Upload]:
    """What R2 does not already hold at the right size.

    Size and not mere presence, because a truncated or interrupted PUT leaves
    a key that exists and is wrong. Presence alone would make the next run
    skip precisely the object that needs redoing.
    """
    return [item for item in planned if remote.get(item.key) != item.size]


#----- transfer ---------------------------------------------------------------

def upload_one(client: Any, bucket: str, item: Upload) -> None:
    """Put one object. Raises on failure; the caller records and continues."""
    client.upload_file(
        str(item.source),
        bucket,
        item.key,
        ExtraArgs={"ContentType": item.content_type},
    )


def upload_all(
    client: Any, bucket: str, items: list[Upload], parallelism: int
) -> dict[str, str]:
    """Upload in parallel, returning {key: error} for the ones that failed.

    One bad object must not abandon the other 900: the work set is recomputed
    next run anyway, so the useful behavior is to push everything that can go
    and report the rest.
    """
    failures: dict[str, str] = {}

    def _attempt(item: Upload) -> None:
        try:
            upload_one(client, bucket, item)
        except Exception as exc:
            failures[item.key] = f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        list(pool.map(_attempt, items))
    return failures


def reconcile(
    attempted: list[Upload], remote: dict[str, int]
) -> tuple[list[Upload], list[str]]:
    """Split what landed correctly from what did not, given a fresh listing.

    Kept pure and separate from the listing call so the decision can be tested
    without standing up an S3 double. A test that faked the client would be
    proving the fake behaves, not that this comparison is right.
    """
    confirmed = [item for item in attempted if remote.get(item.key) == item.size]
    bad = [
        f"{item.key}: expected {item.size} bytes, R2 has {remote.get(item.key)}"
        for item in attempted
        if remote.get(item.key) != item.size
    ]
    return confirmed, bad


def verify(
    client: Any, bucket: str, attempted: list[Upload]
) -> tuple[list[Upload], list[str]]:
    """Re-list the bucket and confirm each object landed at the right size.

    Deliberately independent of the upload's own return values: the whole
    point is to catch the case where the transfer reported success and the
    object is nonetheless short.
    """
    return reconcile(attempted, list_r2_sizes(client, bucket))


def append_journal(path: Path, record: dict[str, Any]) -> None:
    """One line per run. Append-only, so a later run can never rewrite the
    history of an earlier one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


#----- cli --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/local_stems_to_r2.py",
        description="Publish local stem bundles to Cloudflare R2",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=REPO_ROOT / "data",
        help="data directory holding state/; worktrees point this at the "
             "primary checkout",
    )
    parser.add_argument("--root", action="append", type=Path, default=None,
                        help="stem store to publish; repeatable")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=0,
                        help="cap objects this run (0 = all outstanding)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the outstanding set; needs no credentials")
    parser.add_argument("--parallelism", type=int, default=UPLOAD_PARALLELISM)
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = tuple(args.root) if args.root else default_roots(args.data_dir)
    db_path = args.data_dir / "state/state.db"
    resolver = TrackResolver(db_path) if db_path.is_file() else None

    bundles, _scanned, missing = scan_roots(roots, resolver)
    for root in missing:
        print(f"note: {root} does not exist, skipping", file=sys.stderr)

    publishable = [b for b in bundles if b.is_publishable]
    planned = planned_uploads(bundles)
    held_back = len(bundles) - len(publishable)
    print(
        f"local: {len(bundles)} bundles, {len(publishable)} publishable "
        f"({len(planned)} objects, "
        f"{sum(i.size for i in planned) / 1e9:.2f} GB); {held_back} held back "
        "as incomplete, unidentified, presetless or manifestless"
    )

    duplicates = duplicate_ids(bundles)
    if duplicates:
        print(
            f"note: {len(duplicates)} stable_ids appear in more than one store. "
            "Nothing is deleted here; run stem_inventory.py --json to see them.",
            file=sys.stderr,
        )

    if args.dry_run:
        for item in planned[:10]:
            print(f"  would put {item.key} ({item.size / 1e6:.1f} MB)")
        if len(planned) > 10:
            print(f"  ... and {len(planned) - 10} more objects")
        print("\ndry run: no credentials used, nothing uploaded")
        return 0

    client = r2_client("scripts/local_stems_to_r2.py")
    remote = list_r2_sizes(client, args.bucket)
    todo = outstanding(planned, remote)
    batch = todo[: args.limit] if args.limit > 0 else todo
    print(
        f"r2 holds {len(remote)} stem objects; outstanding={len(todo)}, "
        f"this run={len(batch)} ({sum(i.size for i in batch) / 1e9:.2f} GB)"
    )
    if not batch:
        print("nothing to do: R2 is caught up with local")
        return 0

    failures = upload_all(client, args.bucket, batch, args.parallelism)
    confirmed, mismatched = verify(client, args.bucket, batch)

    record = {
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "bucket": args.bucket,
        "attempted": len(batch),
        "confirmed": len(confirmed),
        "bytes_confirmed": sum(i.size for i in confirmed),
        "upload_errors": failures,
        "size_mismatches": mismatched,
    }
    append_journal(args.journal, record)

    print(
        f"\ndone: {len(confirmed)}/{len(batch)} objects confirmed in R2 "
        f"({sum(i.size for i in confirmed) / 1e9:.2f} GB). "
        f"journal: {args.journal}"
    )
    for key, reason in list(failures.items())[:10]:
        print(f"  upload failed {key}: {reason}", file=sys.stderr)
    for line in mismatched[:10]:
        print(f"  size mismatch {line}", file=sys.stderr)
    if failures or mismatched:
        print(
            "re-run to retry: the work set is recomputed from the two "
            "listings, so only the unfinished objects remain.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
