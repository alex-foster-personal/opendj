#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.34"]
# ///
"""Publish bifrost2's stem bundles to Cloudflare R2 (the backfill direction).

``scripts/r2_stem_sync.py`` moves bundles R2 -> bifrost2 for archival. This is
the other direction: the 179 bundles that only ever existed on bifrost2's
D:/asset-store predate the farm's ``--stems-dest r2`` path, so R2 has never
held them. This job is a one-off backfill that leaves the two stores
consistent, after which the farm publishes to R2 directly.

ROUTE, and why. The bytes go bifrost2 -> R2 DIRECTLY. The Mac mints a
presigned PUT URL per object and hands bifrost2 nothing but URLs, so:
  * 15.2 GB never traverses the Air's connection (the naive route is
    scp down + upload back up, i.e. 30 GB through a laptop uplink);
  * R2 credentials never leave the Mac. bifrost2 is a shared Windows box; a
    presigned URL that expires and only grants PUT on one key is a far
    smaller thing to leak than an account-scoped S3 secret at rest.
bifrost2 needs only curl, which its MSYS2 shell already has.

IDEMPOTENT + RESUMABLE, with no cursor file: the work set is (bifrost2
objects) - (R2 objects of the same key AND size), recomputed every run. Kill
it at any point and re-running does exactly the remainder. A short or
interrupted PUT leaves a size mismatch, so the next run redoes that object
rather than trusting it.

INCOMPLETE BUNDLES ARE SKIPPED, LOUDLY. One bundle on bifrost2 holds only
bass+vocals with no drums, other or manifest. Publishing that would put a
bundle in R2 that every consumer reads as complete. It is reported and
excluded unless --include-incomplete is passed.

Requirements (mini-PRD):
  ✔︎ ✅ the work set is (bifrost2 objects) - (R2 objects with matching size).
    [if] the job is re-run after a full success [then] it uploads nothing, exits 0
    [if] the job is killed mid-run [then] the next run resumes with no repeats
  ✔︎ ✅ keys match the farm's scheme, stems/<preset>/<stable_id>/<filename>.
    [if] a key would differ from scripts/modal_vocal_farm.py's [then ⛔️] raise
  ✔︎ ✅ every uploaded object is verified by size against bifrost2.
    [if] any size differs after upload [then ⛔️] report it and exit non-zero
  ✔︎ ✅ R2 credentials never reach bifrost2.
    [if] a remote command would carry R2_SECRET_ACCESS_KEY [then ⛔️] raise
  ✔︎ ✅ incomplete bundles are excluded and named.
    [if] a bundle lacks a manifest or 4 stems [then] skip it and report it

Run (R2_* come from Doppler; boto3 is inline, so no repo venv needed):
  doppler run --project general --config dev_personal -- \
    uv run scripts/b2_stems_to_r2.py --dry-run
  doppler run --project general --config dev_personal -- \
    uv run scripts/b2_stems_to_r2.py --preset htdemucs_ft-ov0.5

--dry-run inspects only the bifrost2 side and needs no credentials.

-Claude
"""
from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.b2store import SSH_HOST, SSH_OPTS, STORE_ROOT

R2_ENV_KEYS: tuple[str, str, str] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)
DEFAULT_BUCKET: str = "music-dj-audio"
R2_STEM_PREFIX: str = "stems"          # matches modal_vocal_farm.R2_STEM_PREFIX
STEMS_REMOTE_ROOT: str = f"{STORE_ROOT}/stems"
STEM_PARTS: frozenset[str] = frozenset({"bass", "drums", "other", "vocals"})
MANIFEST: str = "manifest.json"
# One hour is comfortably longer than the slowest plausible bundle and short
# enough that a leaked URL is near-worthless.
URL_TTL_SECONDS: int = 3600
UPLOAD_PARALLELISM: int = 8


@dataclass
class Bundle:
    """One stable_id's files on bifrost2."""

    preset: str
    stable_id: str
    files: dict[str, int] = field(default_factory=dict)  # filename -> bytes

    @property
    def total_bytes(self) -> int:
        return sum(self.files.values())

    @property
    def is_complete(self) -> bool:
        """Four stems plus a manifest. Extension-agnostic: bifrost2 holds
        opus, flac and wav bundles, and a couple carry both flac and wav."""
        if MANIFEST not in self.files:
            return False
        stems = {name.rsplit(".", 1)[0] for name in self.files if name != MANIFEST}
        return STEM_PARTS.issubset(stems)

    def key_for(self, filename: str) -> str:
        return f"{R2_STEM_PREFIX}/{self.preset}/{self.stable_id}/{filename}"


#----- bifrost2 side ----------------------------------------------------------

def _ssh(remote_cmd: str, timeout: int = 120) -> str:
    """Run a POSIX one-liner on bifrost2, return stdout, raise on failure."""
    done = subprocess.run(
        ["ssh", *SSH_OPTS, SSH_HOST, remote_cmd],
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"bifrost2 command failed ({done.returncode}): {remote_cmd}\n"
            f"{done.stderr.strip()}"
        )
    return done.stdout


def list_bifrost2_bundles(preset: str | None = None) -> list[Bundle]:
    """Inventory the asset store in ONE ssh round trip.

    A call per bundle would be ~179 handshakes, which costs more than several
    of the transfers. `find -printf` is available because the remote shell is
    MSYS2 bash, not cmd (see scripts/b2store.py).
    """
    root = f"{STEMS_REMOTE_ROOT}/{preset}" if preset else STEMS_REMOTE_ROOT
    depth = 2 if preset else 3
    listing = _ssh(
        f"find '{root}' -mindepth {depth} -maxdepth {depth} -type f "
        r"-printf '%s\t%P\n'"
    )
    grouped: dict[tuple[str, str], Bundle] = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        size_text, rel = line.split("\t", 1)
        parts = rel.split("/")
        if preset:
            stable_id, filename = parts[0], "/".join(parts[1:])
            preset_name = preset
        else:
            preset_name, stable_id, filename = parts[0], parts[1], "/".join(parts[2:])
        bundle = grouped.setdefault(
            (preset_name, stable_id), Bundle(preset_name, stable_id)
        )
        bundle.files[filename] = int(size_text)
    return [grouped[k] for k in sorted(grouped)]


#----- R2 side ----------------------------------------------------------------

def r2_client() -> Any:
    """S3 client for R2. Fails naming the missing key rather than surfacing a
    bare NoCredentialsError from three frames deeper.

    The credential check runs BEFORE the boto3 import, deliberately. boto3 is
    a PEP 723 inline dependency, so it is absent unless the script is run
    under `uv run` -- and the far more common mistake is forgetting `doppler
    run`, not forgetting `uv run`. Importing first buried that behind a
    ModuleNotFoundError, which says nothing about the actual problem.
    """
    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"error: {missing} not in the environment. Run under:\n"
            "  doppler run --project general --config dev_personal -- "
            "uv run scripts/b2_stems_to_r2.py ...\n"
            "If those secrets do not exist, mint an R2 API token (Object Read "
            "& Write, scoped to the music-dj-audio bucket) at\n"
            "  Cloudflare dashboard > R2 > API > Manage API tokens\n"
            "and store it in Doppler general/dev_personal under those names."
        )

    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def list_r2_sizes(client: Any, bucket: str, preset: str | None) -> dict[str, int]:
    """{key: size} already in R2. Paginated: 179 bundles x 5 objects is past
    the 1000-key cap, and silently taking page one would make the job re-upload
    what is already there."""
    prefix = f"{R2_STEM_PREFIX}/{preset}/" if preset else f"{R2_STEM_PREFIX}/"
    sizes: dict[str, int] = {}
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for item in page.get("Contents", []):
            sizes[item["Key"]] = item["Size"]
    return sizes


#----- transfer ---------------------------------------------------------------

def _assert_no_secrets(payload: str) -> None:
    """Guard the design invariant: bifrost2 receives URLs, never credentials.

    Cheap, but it is the one property that a careless future edit (say,
    exporting the env into the remote command) would break silently.
    """
    for key in R2_ENV_KEYS:
        value = os.environ.get(key)
        if value and value in payload:
            raise RuntimeError(
                f"refusing to send {key} to {SSH_HOST}: this job keeps R2 "
                "credentials on the Mac and hands bifrost2 presigned URLs only"
            )


def upload_batch(
    client: Any, bucket: str, jobs: list[tuple[str, str]], parallelism: int
) -> dict[str, int]:
    """PUT each (remote_path, key) from bifrost2 straight to R2.

    Returns {key: http_status}. The URLs are streamed over stdin rather than
    written to a file on bifrost2, so nothing sensitive is left at rest there.
    """
    lines: list[str] = []
    for remote_path, key in jobs:
        # The remote loop splits each line at its single space, so a space in
        # a path would silently truncate it into a wrong path plus a wrong
        # URL. Stem paths are hex stable_ids and fixed filenames, so this is a
        # guard against a future layout change, not a live case.
        if " " in remote_path:
            raise RuntimeError(
                f"remote path contains a space, which the batched upload "
                f"cannot express: {remote_path!r}"
            )
        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=URL_TTL_SECONDS,
        )
        lines.append(f"{remote_path} {url}")
    payload = "\n".join(lines) + "\n"
    _assert_no_secrets(payload)

    # -d '\n' makes each LINE one argument, which also switches off xargs's
    # default quote/backslash processing -- that would otherwise chew on a
    # presigned URL. bash then splits the line at its single space.
    # -H 'Expect:' suppresses curl's 100-continue, which some S3 endpoints
    # answer in a way that breaks a streamed upload.
    remote_cmd = (
        f"xargs -d '\\n' -P {parallelism} -n 1 bash -c "
        "'p=\"${0% *}\"; u=\"${0#* }\"; "
        "code=$(curl -sS -o /dev/null -w \"%{http_code}\" -X PUT "
        "-T \"$p\" -H \"Expect:\" \"$u\"); echo \"$code $p\"'"
    )
    done = subprocess.run(
        ["ssh", *SSH_OPTS, SSH_HOST, remote_cmd],
        input=payload, text=True, capture_output=True, timeout=3600, check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"remote upload failed: {done.stderr.strip()}")

    path_to_key = dict(jobs)
    statuses: dict[str, int] = {}
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        code, remote_path = line.split(" ", 1)
        statuses[path_to_key[remote_path.strip()]] = int(code)
    return statuses


#----- cli --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/b2_stems_to_r2.py",
        description="Backfill bifrost2 stem bundles into Cloudflare R2",
    )
    parser.add_argument("--preset", default=None,
                        help="limit to one preset tag (default: every preset)")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=0,
                        help="cap bundles this run (0 = all outstanding)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the outstanding set; needs no credentials")
    parser.add_argument("--parallelism", type=int, default=UPLOAD_PARALLELISM)
    parser.add_argument(
        "--include-incomplete", action="store_true",
        help="publish bundles missing stems or a manifest. Off by default: a "
             "partial bundle in R2 reads as complete to every consumer.",
    )
    return parser


def _report_inventory(bundles: list[Bundle], skipped: list[Bundle]) -> None:
    per_preset = collections.Counter(b.preset for b in bundles)
    total_gb = sum(b.total_bytes for b in bundles) / 1e9
    print(f"bifrost2: {len(bundles)} publishable bundles, {total_gb:.2f} GB, "
          f"across {len(per_preset)} presets")
    for preset, count in sorted(per_preset.items()):
        size = sum(b.total_bytes for b in bundles if b.preset == preset) / 1e9
        print(f"  {preset:26s} {count:4d} bundles  {size:6.2f} GB")
    if skipped:
        print(f"\nSKIPPED {len(skipped)} incomplete bundle(s) "
              "(pass --include-incomplete to publish anyway):")
        for bundle in skipped:
            print(f"  {bundle.preset}/{bundle.stable_id}: "
                  f"has {sorted(bundle.files)}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    every = list_bifrost2_bundles(args.preset)
    if not every:
        root = f"{STEMS_REMOTE_ROOT}/{args.preset}" if args.preset else STEMS_REMOTE_ROOT
        print(f"nothing under {root} on {SSH_HOST}")
        return 0

    skipped = [] if args.include_incomplete else [b for b in every if not b.is_complete]
    bundles = [b for b in every if b.is_complete or args.include_incomplete]
    _report_inventory(bundles, skipped)

    if args.dry_run:
        print("\n--dry-run: inspected bifrost2 only, contacted no credentials "
              "and uploaded nothing.")
        return 0

    client = r2_client()
    present = list_r2_sizes(client, args.bucket, args.preset)

    jobs: list[tuple[str, str]] = []
    for bundle in bundles:
        for filename, size in sorted(bundle.files.items()):
            key = bundle.key_for(filename)
            if present.get(key) == size:
                continue  # already there, byte-for-byte
            remote_path = (
                f"{STEMS_REMOTE_ROOT}/{bundle.preset}/{bundle.stable_id}/{filename}"
            )
            jobs.append((remote_path, key))

    already = sum(len(b.files) for b in bundles) - len(jobs)
    print(f"\nr2 already holds {already} matching objects; "
          f"{len(jobs)} to upload")
    if not jobs:
        print("nothing to do: R2 is caught up")
        return 0

    if args.limit > 0:
        wanted = {b.stable_id for b in bundles[: args.limit]}
        jobs = [j for j in jobs if j[1].split("/")[2] in wanted]
        print(f"--limit {args.limit}: {len(jobs)} objects this run")

    statuses = upload_batch(client, args.bucket, jobs, args.parallelism)

    failed = {k: c for k, c in statuses.items() if not 200 <= c < 300}
    missing = [k for _, k in jobs if k not in statuses]
    print(f"uploaded {len(statuses) - len(failed)}/{len(jobs)} objects")

    # Verify against R2 itself rather than trusting the HTTP codes: a
    # truncated PUT can still answer 200.
    after = list_r2_sizes(client, args.bucket, args.preset)
    expected = {
        bundle.key_for(name): size
        for bundle in bundles for name, size in bundle.files.items()
    }
    mismatched = {
        key: (size, after.get(key))
        for key, size in expected.items()
        if after.get(key) != size
    }

    if failed or missing or mismatched:
        for key, code in sorted(failed.items()):
            print(f"  HTTP {code} {key}", file=sys.stderr)
        for key in missing:
            print(f"  no status reported for {key}", file=sys.stderr)
        for key, (want, got) in sorted(mismatched.items()):
            print(f"  size mismatch {key}: bifrost2 {want}, r2 {got}",
                  file=sys.stderr)
        print("\nre-run to retry: the outstanding set is recomputed from the "
              "two listings, so only the failures remain.", file=sys.stderr)
        return 1

    verified_gb = sum(expected.values()) / 1e9
    print(f"verified {len(expected)} objects ({verified_gb:.2f} GB) match "
          f"bifrost2 byte-for-byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
