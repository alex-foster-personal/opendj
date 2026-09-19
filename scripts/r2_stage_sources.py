#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.34"]
# ///
"""Stage source audio into Cloudflare R2 so Modal pulls it server-to-server.

Local source uploads can put an uplink on every run's critical path.
Staging sources in R2 lets a Modal container fetch them server-to-server.
The farm's ``--stems-dest r2`` option addresses output storage; this script
addresses input storage.

Re-running a preset then costs GPU time and nothing else, which is the whole
point: the expensive part of an experiment should be the experiment.

KEY LAYOUT is `source/<stable_id><ext>`, original extension preserved. The
stable_id is already the farm's unit of identity, so a container that knows
which track it was given can derive the key without a manifest lookup, and the
extension survives so a decoder never has to sniff the container format.

IDEMPOTENCE comes from comparing R2 against the local file, not from a cursor
file. head_object gives the ContentLength R2 actually holds; if that matches
the size on disk the object is already staged and the upload is skipped. Kill
this script at any point and the next run recomputes exactly the remaining
set. There is no state to corrupt.

VERIFICATION is that same ContentLength, read back after the upload. Not a
re-download (wasteful, and it would put the bytes back on the Mac's link) and
not the local size echoed back at us (which proves nothing about R2).

ICLOUD EVICTION IS A ONE-WAY DOOR. An evicted file can report its full size
while its data is held remotely. Reading it materialises the data and
consumes local disk. Every candidate is stat-ed before opening it;
``os.stat().st_blocks == 0`` identifies an evicted file without reading its
contents, and such files are skipped loudly.

Requirements (mini-PRD):
  ✔︎ ✅ the candidate set is tracks with duration_ms>0 and an on-disk file_path.
    [if] file_path is NULL or names a missing file [then] the row is not a candidate
    [if] only some DB rows have existing files [then] only those files are candidates
    [if] --limit N is passed [then] at most N candidates are considered
  ✔︎ ✅ an iCloud-evicted source is never opened, only stat-ed.
    [if] st_blocks == 0 [then ⛔️] skip, count it, name it in the summary
    [if] a file is evicted [then ⛔️] no read() call is ever issued against it
    [if] every candidate is evicted [then] zero bytes are uploaded and exit is 0
  ✔︎ the upload is idempotent against a matching ContentLength in R2.
    [if] the object is absent [then] it is uploaded
    [if] the object is present with a matching size [then] it is skipped, not re-sent
    [if] the object is present with a DIFFERENT size [then] it is re-uploaded
  ✔︎ every upload is verified by the ContentLength R2 reports on head_object.
    [if] the readback size differs from the local size [then ⛔️] raise, name the key
    [if] head_object errors for any reason but 404 [then ⛔️] raise, do not treat as absent
  ✔︎ ✅ --dry-run resolves the full work set without mutating R2.
    [if] --dry-run is passed [then ⛔️] no put_object call is ever issued
  ✔︎ ✅ failures are loud and never silently swallowed.
    [if] any file fails [then ⛔️] it is listed and the exit status is 1

STATUS NOTE: the plain ✔︎ requirements that contact R2 remain UNVERIFIED
end to end. Removing private execution history does not supply public
acceptance evidence for the other markers. Validate these claims on the
intended inputs and an enabled account before promoting any marker.

Run (supply R2_* through the operator's configured credential provider;
select its project/config explicitly. boto3 is inline; no repo venv is needed):
  doppler run --project <operator-project> --config <operator-config> -- \
    uv run scripts/r2_stage_sources.py --dry-run
  doppler run --project <operator-project> --config <operator-config> -- \
    uv run scripts/r2_stage_sources.py --limit 20
  doppler run --project <operator-project> --config <operator-config> -- \
    uv run scripts/r2_stage_sources.py

-Claude
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# CFG
# ---------------------------------------------------------------------------

# Named to match apps/cloud/config.py and scripts/modal_vocal_farm.py so the
# repo has ONE set of R2 env var names rather than a per-script set.
R2_ENV_KEYS: tuple[str, str, str] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)
DEFAULT_R2_BUCKET: str = "music-dj-audio"  # matches DEFAULT_R2_BUCKET in the farm
R2_SOURCE_PREFIX: str = "source"  # -> source/<stable_id><ext>
R2_MAX_ATTEMPTS: int = 3
DEFAULT_DB_PATH: str = "data/state/state.db"
DEFAULT_WORKERS: int = 8
PROGRESS_EVERY: int = 50

CANDIDATE_SQL: str = """
    select stable_id, file_path
      from tracks
     where duration_ms > 0
       and file_path is not null
"""


@dataclass(frozen=True)
class Candidate:
    """One track whose source file exists on disk."""

    stable_id: str
    path: Path
    size: int

    @property
    def key(self) -> str:
        return f"{R2_SOURCE_PREFIX}/{self.stable_id}{self.path.suffix.lower()}"


@dataclass(frozen=True)
class Outcome:
    """What happened to one candidate. ``kind`` drives the summary counters."""

    kind: str  # uploaded | present | evicted | failed
    candidate: Candidate
    detail: str = ""
    sent_bytes: int = 0


# ---------------------------------------------------------------------------
# _helpers -- discovery
# ---------------------------------------------------------------------------


def _require_r2_env() -> None:
    """Fail before any work if the run cannot possibly succeed.

    Loud and specific by design: the alternative is discovering the gap after
    walking the database and stat-ing candidate files.
    """
    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"error: staging to R2 needs {missing} in the environment.\n"
            "  Run under: doppler run --project <operator-project> --config <operator-config> -- \\\n"
            "    uv run scripts/r2_stage_sources.py ...\n"
            "  If those secrets do not exist yet, R2 has to be enabled on the "
            "Cloudflare account and an R2 API token created first; then store "
            "the pair as R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (and "
            "R2_ACCOUNT_ID) in the operator's configured credential provider. See issue #373."
        )


def _r2_client() -> Any:
    """S3 client pointed at the account's R2 endpoint. Mirrors the farm."""
    import boto3
    from botocore.config import Config

    _require_r2_env()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": R2_MAX_ATTEMPTS, "mode": "standard"}),
    )


def _iter_rows(db_path: Path) -> Iterator[tuple[str, str]]:
    if not db_path.exists():
        raise SystemExit(f"error: state db not found at {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        yield from db.execute(CANDIDATE_SQL)
    finally:
        db.close()


def _classify(stable_id: str, file_path: str) -> tuple[str, Candidate | None]:
    """stat one row. Returns (state, candidate) without ever reading the file.

    ``st_blocks == 0`` with a non-zero ``st_size`` is the iCloud dataless-file
    signature: the metadata is local, the bytes are not. Opening it would
    trigger a materialising download, so it is classified here and never
    handed to the uploader.
    """
    path = Path(file_path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing", None
    except OSError as exc:
        raise SystemExit(f"error: cannot stat {path}: {exc}") from exc

    if st.st_size == 0:
        return "empty", None
    if st.st_blocks == 0:
        return "evicted", Candidate(stable_id, path, st.st_size)
    return "ok", Candidate(stable_id, path, st.st_size)


def _collect(db_path: Path, limit: int | None) -> tuple[list[Candidate], list[Candidate], dict[str, int]]:
    """Walk the DB once. Returns (stageable, evicted, counters)."""
    stageable: list[Candidate] = []
    evicted: list[Candidate] = []
    counts: dict[str, int] = {"rows": 0, "missing": 0, "empty": 0}

    for stable_id, file_path in _iter_rows(db_path):
        counts["rows"] += 1
        state, cand = _classify(stable_id, file_path)
        if state == "ok":
            assert cand is not None
            stageable.append(cand)
        elif state == "evicted":
            assert cand is not None
            evicted.append(cand)
        elif state == "missing":
            counts["missing"] += 1
        elif state == "empty":
            counts["empty"] += 1
        else:
            raise AssertionError(f"unhandled state {state!r}")
        if limit is not None and len(stageable) >= limit:
            break

    return stageable, evicted, counts


# ---------------------------------------------------------------------------
# _helpers -- transfer
# ---------------------------------------------------------------------------


def _remote_size(client: Any, bucket: str, key: str) -> int | None:
    """ContentLength R2 holds for ``key``, or None if the object is absent.

    Anything other than a genuine 404 is re-raised: a throttle or a permission
    fault must never be mistaken for "not there yet" and silently re-upload.
    """
    from botocore.exceptions import ClientError

    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise
    return int(head["ContentLength"])


def _stage_one(client: Any, bucket: str, cand: Candidate, dry_run: bool) -> Outcome:
    """head, upload if needed, then verify by the size R2 reports back."""
    existing = _remote_size(client, bucket, cand.key)
    if existing == cand.size:
        return Outcome("present", cand, "size match")

    if dry_run:
        why = "absent" if existing is None else f"size {existing} != {cand.size}"
        return Outcome("uploaded", cand, f"DRY-RUN would upload ({why})")

    with cand.path.open("rb") as fh:
        client.upload_fileobj(fh, bucket, cand.key)

    readback = _remote_size(client, bucket, cand.key)
    if readback != cand.size:
        raise RuntimeError(
            f"verify failed for {cand.key}: local {cand.size} bytes, "
            f"R2 reports {readback}"
        )
    return Outcome("uploaded", cand, "verified", sent_bytes=cand.size)


def _ensure_bucket(client: Any, bucket: str, dry_run: bool) -> str:
    """Return the bucket's state, creating it when absent."""
    from botocore.exceptions import ClientError

    try:
        client.head_bucket(Bucket=bucket)
        return "exists"
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status != 404:
            raise
    if dry_run:
        return "absent (DRY-RUN, not created)"
    client.create_bucket(Bucket=bucket)
    return "created"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage source audio into R2 as source/<stable_id><ext>.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"state db (default {DEFAULT_DB_PATH})")
    parser.add_argument("--bucket", default=DEFAULT_R2_BUCKET, help=f"R2 bucket (default {DEFAULT_R2_BUCKET})")
    parser.add_argument("--limit", type=int, default=None, help="stage at most N files")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"parallel uploads (default {DEFAULT_WORKERS})")
    parser.add_argument("--dry-run", action="store_true", help="resolve the work set, mutate nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    started = time.monotonic()

    db_path = Path(args.db)
    stageable, evicted, counts = _collect(db_path, args.limit)

    print(f"db rows scanned      : {counts['rows']}")
    print(f"file missing on disk : {counts['missing']}")
    print(f"zero-byte files      : {counts['empty']}")
    print(f"iCloud-EVICTED       : {len(evicted)}  ({_gb(sum(c.size for c in evicted))} GB) -- SKIPPED, never opened")
    print(f"stageable            : {len(stageable)}  ({_gb(sum(c.size for c in stageable))} GB)")
    if args.dry_run:
        print("mode                 : DRY-RUN (no object will be written)")
    print()

    if evicted:
        print(f"NOTE: {len(evicted)} evicted file(s) skipped. Reading one would")
        print("      permanently materialise it from iCloud. Showing first 5:")
        for cand in evicted[:5]:
            print(f"      - {cand.stable_id}  {cand.path.name}")
        print()

    if not stageable:
        print("nothing stageable; exiting 0")
        return 0

    client = _r2_client()
    print(f"bucket {args.bucket!r}: {_ensure_bucket(client, args.bucket, args.dry_run)}")

    done = 0
    lock = threading.Lock()
    outcomes: list[Outcome] = []

    def _run(cand: Candidate) -> Outcome:
        nonlocal done
        try:
            outcome = _stage_one(client, args.bucket, cand, args.dry_run)
        except Exception as exc:
            outcome = Outcome("failed", cand, f"{type(exc).__name__}: {exc}")
        with lock:
            done += 1
            if done % PROGRESS_EVERY == 0 or done == len(stageable):
                sent = sum(o.sent_bytes for o in outcomes) + outcome.sent_bytes
                rate = sent / max(time.monotonic() - started, 1e-6) / 1e6
                print(
                    f"  [{done}/{len(stageable)}] {_gb(sent)} GB sent, "
                    f"{rate:.1f} MB/s avg",
                    flush=True,
                )
        return outcome

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_run, cand) for cand in stageable]
        for fut in as_completed(futures):
            outcomes.append(fut.result())

    uploaded = [o for o in outcomes if o.kind == "uploaded"]
    present = [o for o in outcomes if o.kind == "present"]
    failed = [o for o in outcomes if o.kind == "failed"]
    sent = sum(o.sent_bytes for o in uploaded)
    elapsed = time.monotonic() - started

    print()
    print("-" * 62)
    print(f"uploaded        : {len(uploaded)}  ({_gb(sent)} GB)")
    print(f"already present : {len(present)}")
    print(f"evicted-skipped : {len(evicted)}")
    print(f"failed          : {len(failed)}")
    print(f"wall time       : {elapsed:.1f}s")
    print("-" * 62)

    if failed:
        print("\nFAILURES:")
        for o in failed:
            print(f"  - {o.candidate.key}: {o.detail}")
        return 1
    return 0


def _gb(nbytes: int) -> str:
    return f"{nbytes / 1e9:.2f}"


if __name__ == "__main__":
    sys.exit(main())
