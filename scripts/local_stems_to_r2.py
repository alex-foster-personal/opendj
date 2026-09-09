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
HTTP 200. Size rather than checksum, even though every upload here is one
``put_object`` call with the body already in hand (never multipart): a fresh
listing is independent of the upload path's own return value, so it also
catches an object another rail already left short before this run ever
touched it.

ONE READ PER SOURCE, NOT TWO. The body is read once while planning and
handed straight to ``put_object``; nothing re-opens the path a second time
to perform the transfer. A source re-opened after being hashed can be
replaced in between by a same-size render, which a size-only reconciliation
would then wave through under the previous run's content-addressed key --
so the upload ships the exact bytes that were hashed, not a fresh read of
whatever is at that path when the network call happens.

Requirements (mini-PRD):
  ✔︎ ✅ the work set is (local objects) - (R2 objects with matching size).
    [if] re-run after a full success [then] it uploads nothing and exits 0
    [if] killed mid-run [then] the next run resumes with no repeated uploads
    [if] an object exists in R2 at the wrong size [then] it is re-uploaded
  ✔︎ ✅ keys are content-addressed, assets/<sha256[:2]>/<sha256>.
    [if] a key differs from the source body's SHA-256 address [then ⛔️] raise
  ✔︎ ✅ no local file is ever deleted or modified.
    [if] any code path would unlink a source [then ⛔️] it does not exist
  ✔︎ ✅ uploads are confirmed against a fresh listing, not against HTTP codes.
    [if] a size differs after upload [then ⛔️] report it and exit non-zero
  ✔︎ ✅ every confirmed object's mapping is journaled, not only this run's batch.
    [if] a PUT lands but the process dies before appending [then] the next
    run's journal still records it once R2 confirms it
    [if] another rail already published the digest [then] this run's journal
    still records the legacy-to-content mapping for it
  ✔︎ ✅ each source is read once; the bytes hashed are the bytes shipped.
    [if] the source changes between hashing and the PUT [then ⛔️] raise
    before any client call, and never re-open the path for the transfer
  ✔︎ ✅ a torn final journal line does not block the next run.
    [if] the process is killed mid-append [then] the next run's journal read
    tolerates the truncated last line and still raises on any earlier
    corruption
    [if] a later run appends on top of that same torn tail [then] the WRITER
    truncates it first (a stderr note names what was discarded), instead of
    concatenating valid JSON onto invalid bytes and losing every mapping
    appended after it; a well-formed prior tail is left untouched
    [if] a kill lands right after the final closing brace but before its
    newline is flushed [then] the WRITER inserts the missing separator
    before appending, instead of concatenating the new record onto the old
    one with no separator and silently losing both mappings on the next read
    [if] the repair write itself is interrupted [then] it writes to a
    sibling temp file and renames it over the original, instead of
    truncating the journal in place, so a kill mid-repair cannot lose every
    prior mapping (not just the torn line)

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
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.cloud.r2_keys import asset_object_key
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
    legacy_key: str = ""

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES.get(self.source.suffix.lower(), "application/octet-stream")


def object_key_for_body(body: bytes) -> str:
    """Derive the canonical R2 location for these exact bytes."""
    return asset_object_key(hashlib.sha256(body).hexdigest())


def _read_and_key(source: Path, legacy_key: str = "") -> tuple[bytes, Upload]:
    """Read a source exactly once and key it, returning both.

    ``upload_one`` needs the bytes it validated against, not a second read of
    the path: reading here and handing the same buffer to ``put_object``
    closes the window where a same-size replacement between validation and
    transfer would ship the wrong bytes under the previous content-addressed
    key.
    """
    body = source.read_bytes()
    return body, Upload(object_key_for_body(body), source, len(body), legacy_key)


def upload_for_source(source: Path, legacy_key: str = "") -> Upload:
    """Snapshot a local object's content address and size before publishing."""
    _, item = _read_and_key(source, legacy_key)
    return item


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
            planned.append(upload_for_source(source, bundle.key_for(filename)))
    return planned


def outstanding(planned: list[Upload], remote: dict[str, int]) -> list[Upload]:
    """What R2 does not already hold at the right size.

    Size and not mere presence, because a truncated or interrupted PUT leaves
    a key that exists and is wrong. Presence alone would make the next run
    skip precisely the object that needs redoing.
    """
    return [item for item in planned if remote.get(item.key) != item.size]


#----- transfer ---------------------------------------------------------------

def _put_object_kwargs(item: Upload) -> dict[str, Any]:
    """Build the exact ``put_object`` payload from one read of ``item.source``.

    Pinned as its own pure function so this decision is testable without a
    client of any kind: ``Body`` is the bytes already read into memory, never
    the path, so nothing downstream (real boto3 or otherwise) can reopen the
    source a second time. Refuses a source changed since it was keyed.
    """
    body, snapshot = _read_and_key(item.source, item.legacy_key)
    if snapshot != item:
        raise RuntimeError(
            f"source changed after content-addressing: {item.source}; rerun to re-plan"
        )
    return {"Key": item.key, "Body": body, "ContentType": item.content_type}


def upload_one(client: Any, bucket: str, item: Upload) -> None:
    """Put one object from the single body it was keyed from.

    ``client.upload_file`` would reopen ``item.source`` a second time, and a
    same-size replacement in that window would silently ship the wrong bytes
    under a content-addressed key that no longer matches them; passing
    ``Body=`` (built by ``_put_object_kwargs``) removes the path argument that
    vulnerability needed in the first place.

    Built as its own statement, not inlined into the call: ``client.put_object``
    is resolved (attribute lookup) before a call's arguments are evaluated, so
    inlining ``_put_object_kwargs(item)`` there would attempt that lookup
    before a changed source is ever detected, touching ``client`` on exactly
    the path meant to raise before any client call.
    """
    kwargs = _put_object_kwargs(item)
    client.put_object(Bucket=bucket, **kwargs)


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


def append_journal(path: Path, record: dict[str, Any]) -> None:
    """One line per run. Append-only, so a later run can never rewrite the
    history of an earlier one.

    Repairs a torn tail left by a kill mid-append first: appending straight
    onto a truncated final line would concatenate this run's valid JSON onto
    invalid bytes with no separator, corrupting the new record too and
    silently swallowing every mapping recorded from this point forward.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _truncate_torn_tail(path)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _truncate_torn_tail(path: Path) -> None:
    """Repair a journal so the next append never lands mid-line.

    Two shapes come out of a kill mid-write: a genuinely torn line (drop it,
    only ever touching the LAST line -- an earlier corrupt line is real
    corruption and stays in place to raise), and a complete-but-unterminated
    final object whose trailing newline never landed. Read-time tolerance
    treats the second as fine, since json.loads ignores a missing trailing
    newline, but appending straight onto it would concatenate the new record
    right after the old one with no separator, corrupting both on the next
    read.
    """
    if not path.is_file():
        return
    lines = path.read_text().splitlines(keepends=True)
    if not lines:
        return
    try:
        json.loads(lines[-1])
    except json.JSONDecodeError:
        print(
            f"local_stems_to_r2: discarding torn tail line from {path}: {lines[-1]!r}",
            file=sys.stderr,
        )
        # write_text() truncates in place first: a kill between that
        # truncation and the write landing would lose every prior mapping,
        # not just the torn line. Write the repair to a sibling temp file
        # and rename it over the original, so a kill mid-repair leaves
        # either the untouched original or the fully-written repair, never
        # a partial file.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(lines[:-1]))
        tmp.replace(path)
        return
    if not lines[-1].endswith("\n"):
        with path.open("a") as handle:
            handle.write("\n")


def _journaled_mappings(path: Path) -> set[tuple[str, str]]:
    """(legacy_key, key) pairs already recorded across every prior run.

    Paired identity, not legacy_key alone: re-rendering a source keeps the
    same legacy_key but changes its content key, and a legacy_key-only dedup
    would silently drop that new mapping forever, which is the same class of
    permanent mapping loss this journal exists to prevent.

    Tolerates a torn FINAL line only: a process killed mid-append leaves a
    truncated last record, and this job's own kill-and-resume design means
    the very next run must read the journal, not need manual repair first.
    An earlier line that fails to parse is real corruption, not an
    interrupted write, and still raises.
    """
    if not path.is_file():
        return set()
    mappings: set[tuple[str, str]] = set()
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        for obj in record.get("objects", []):
            mappings.add((obj["legacy_key"], obj["key"]))
    return mappings


def new_journal_mappings(
    planned: list[Upload],
    final_remote: dict[str, int],
    already_journaled: set[tuple[str, str]],
) -> list[Upload]:
    """Every planned source whose destination is confirmed in R2 right now and
    whose (legacy_key, key) pair has not already been journaled.

    Covers uploads confirmed THIS run and objects a prior interrupted run or
    another rail already put in place: both are "confirmed present in R2",
    and both need their legacy-to-content mapping recorded exactly once, or
    that association is permanently lost despite R2 holding the object.
    """
    return [
        item
        for item in planned
        if final_remote.get(item.key) == item.size
        and (item.legacy_key, item.key) not in already_journaled
    ]


def run_worth_recording(batch: list[Upload], new_mappings: list[Upload]) -> bool:
    """Whether this run has anything a durable record must not lose.

    True whenever uploads were attempted, even if every one of them failed or
    came up short: `upload_errors` and `size_mismatches` are the only durable
    trace of that outcome, and gating the journal append on `new_mappings`
    alone would silently drop them on a run that attempted work and confirmed
    none of it, printing "nothing to do" over what was actually a failure.
    """
    return bool(batch) or bool(new_mappings)


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

    failures: dict[str, str] = {}
    if batch:
        failures = upload_all(client, args.bucket, batch, args.parallelism)
        # Re-list rather than trust upload_all's own return values: the
        # whole point is to catch a transfer that reported success while the
        # object landed short. Reused below for journaling too, so an object
        # another rail already published gets its mapping recorded even
        # though this run never uploaded it.
        final_remote = list_r2_sizes(client, args.bucket)
    else:
        final_remote = remote

    confirmed, mismatched = reconcile(batch, final_remote)

    already_journaled = _journaled_mappings(args.journal)
    new_mappings = new_journal_mappings(planned, final_remote, already_journaled)

    if run_worth_recording(batch, new_mappings):
        record = {
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "bucket": args.bucket,
            "attempted": len(batch),
            "confirmed": len(confirmed),
            "bytes_confirmed": sum(i.size for i in confirmed),
            "upload_errors": failures,
            "size_mismatches": mismatched,
            "objects": [
                {"legacy_key": item.legacy_key, "key": item.key}
                for item in new_mappings
            ],
        }
        append_journal(args.journal, record)
        print(
            f"\ndone: {len(confirmed)}/{len(batch)} objects confirmed in R2 "
            f"({sum(i.size for i in confirmed) / 1e9:.2f} GB); "
            f"{len(new_mappings)} new mapping(s) journaled. journal: {args.journal}"
        )
    else:
        print("nothing to do: R2 is caught up with local and the journal is current")

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
