"""CloudSync stems push/hydrate bodies for ``python -m apps.lyrics stems``.

``stems push --missing`` delegates to :mod:`apps.stems.r2_migration` (work
set = local minus R2-at-matching-size, verify by re-listing, journal at
``<data_dir>/state/stem-r2-migration.jsonl``). ``stems hydrate`` fetches each
part by ``files_sha256`` via :func:`apps.cloud.asset_store.fetch_asset`` and
verifies with :func:`apps.stems.artifacts.load_stem_bundle`.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from apps.cloud import asset_store, policy
from apps.cloud.eviction import HydrationError
from apps.cloud.stem_bundles import file_marks
from apps.lyrics.artifacts import asset_clients_for_mode
from apps.shared.paths import DATA_DIR
from apps.stems.artifacts import ROFORMER_STEMS_DIR, load_stem_bundle
from apps.stems.bundle_lock import beside_stem_root, remove_staging, replace_bundle_dir, store_writer_lock
from apps.stems.r2_migration import main as rail_main
from apps.stems.recompress import bundle_fingerprint

STEM_ASSET_KIND = "stem_bundle"
#: Beside the stem root, never inside it, so no scan or eviction pass sees a
#: half-fetched bundle: ``<root parent>/.stem-hydrate/<unique>/<stable_id>``.
HYDRATE_STAGING_DIR_NAME = ".stem-hydrate"


def _stems_root(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir / "state" / "stems-roformer-spike"
    return ROFORMER_STEMS_DIR


#: What :func:`live_bundle_digest` returns for a bundle another writer changed
#: while it was hashed: it equals no digest, so the swap keeps that writer's bundle.
CHANGING = "changing"


def _hash_outside_the_lock(bundle_dir: Path) -> tuple[str, tuple[tuple[str, int, int, int], ...] | None]:
    """The bundle's content digest and the marks it was taken against.

    The marks (:func:`file_marks`: every file's device, inode and ctime) are
    read under the bundle lock, the bytes are hashed with it released, and the
    caller compares the marks again under the lock before relying on the
    digest. The lock is striped across ids, so a full hash under it would stall
    other tracks' hydrates and publishes for the length of a disk read (Codex
    on #6821). Every writer swaps a whole directory in (new inodes) and a write
    in place moves ctime, so a bundle changed mid-hash fails that comparison.
    ``CHANGING`` when the bundle vanished or was swapped out under the hash.
    """
    with store_writer_lock(bundle_dir):
        marks = file_marks(bundle_dir)
    try:
        digest = bundle_fingerprint(bundle_dir)
    except (FileNotFoundError, NotADirectoryError):
        return CHANGING, marks
    return digest, marks


def live_bundle_digest(bundle_dir: Path) -> str:
    """The live bundle's content digest (:func:`bundle_fingerprint`, ``"absent"``
    when there is none), or :data:`CHANGING` when another writer changed it
    while it was hashed outside the bundle lock.

    Content, not names, sizes and mtimes: on a coarse-clock filesystem (FAT,
    HFS+) a same-codec republish of fixed-duration CBR parts can keep all
    three, and a metadata stamp would then let a stale cloud copy overwrite
    the newer render (Codex P1 on #6821).
    """
    digest, marks = _hash_outside_the_lock(bundle_dir)
    with store_writer_lock(bundle_dir):
        if file_marks(bundle_dir) != marks:
            return CHANGING
    return digest


def swap_in_hydrated(staging: Path, bundle_dir: Path, before: str) -> bool:
    """Swap a fetched ``staging`` bundle in as ``bundle_dir``, unless it changed.

    Under the bundle lock every writer of this directory holds (the STEM-58
    re-compress swap and eviction rewrite this root too), and for the mark
    check and swap alone; the content hash runs before it with the lock free.
    ``before`` is :func:`live_bundle_digest` taken before the fetch: if
    another writer published, rewrote or removed the bundle since, its bundle
    is kept and False is returned (Codex on #6820 and #6821).
    """
    if before == CHANGING:
        return False
    digest, marks = _hash_outside_the_lock(bundle_dir)
    if digest != before:
        return False
    with store_writer_lock(bundle_dir):
        if file_marks(bundle_dir) != marks:
            return False
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        replace_bundle_dir(staging, bundle_dir)
        return True


def push_missing(
    *,
    data_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Backfill local stem bundles to R2 via the hardened migration rail."""
    root = data_dir or DATA_DIR
    journal = root / "state" / "stem-r2-migration.jsonl"
    argv = [
        "--data-dir",
        str(root),
        "--journal",
        str(journal),
    ]
    if dry_run:
        argv.append("--dry-run")
    return rail_main(argv)


def hydrate(
    stable_id: str,
    *,
    manifest_path: Path,
    data_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Fetch every manifest part by sha256 and strict-read the bundle."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 3:
        raise ValueError(
            f"stems hydrate requires schema_version 3; got {manifest.get('schema_version')}"
        )
    files = manifest.get("files") or {}
    hashes = manifest.get("files_sha256") or {}
    missing_parts = [part for part in files if part not in hashes]
    if missing_parts:
        raise ValueError(
            f"manifest missing files_sha256 for part(s): {missing_parts}; "
            "run register-stems on a v3 bundle first"
        )

    stems_root = _stems_root(data_dir)
    bundle_dir = stems_root / stable_id
    if dry_run:
        print(f"[DRY-RUN] hydrate {stable_id} -> {bundle_dir}")
        for part, rel in files.items():
            print(f"  fetch {hashes[part][:16]}... -> {rel}")
        return 0

    if policy.CFG.mode != "cloud":
        raise HydrationError(
            "stems hydrate requires cloudsync mode 'cloud'; local mode has no fetch path"
        )
    s3, cfg = asset_clients_for_mode(writing=True)
    if s3 is None or cfg is None:
        raise HydrationError("cloud mode but no S3 client / CloudConfig supplied")
    # Fetch into a staging copy and swap it in whole (Codex on #6820): a
    # bundle STEM-58 already re-compressed holds other files, which layering
    # the manifest's parts over it would leave behind, outside the new
    # manifest's R2 index and so never evictable; and a failed fetch must
    # leave the old bundle as it was.
    # One staging parent per call, not per process: the webui serves hydrates
    # for different tracks at once (Codex on #6820).
    # Beside the RESOLVED root, so the publishing rename stays on one
    # filesystem when the stem root is a symlink to another disk.
    staging_root = beside_stem_root(stems_root) / HYDRATE_STAGING_DIR_NAME
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=f"{stable_id}-", dir=str(staging_root)))
    staging = staging_parent / stable_id
    # What the live bundle was before the unlocked fetch, so a bundle another
    # writer publishes meanwhile is kept, not overwritten (Codex on #6820).
    before = live_bundle_digest(bundle_dir)
    try:
        # The staging copy is this call's alone, so the network fetch and the
        # strict load run with no lock held: the striped bundle lock is shared
        # by every id on its stripe, and a deck hydrate of another track must
        # not wait out this transfer (Codex on #6821).
        staging.mkdir()
        for part, rel in files.items():
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            fetch_asset = asset_store.fetch_asset
            fetch_asset(cfg, s3, hashes[part], dest)
        shutil.copy2(manifest_path, staging / "manifest.json")
        load_stem_bundle(stable_id, stems_dir=staging_parent)
        swapped = swap_in_hydrated(staging, bundle_dir, before)
    finally:
        remove_staging(staging_parent, "stems hydrate staging directory")
    if not swapped:
        print(f"[KEEP] {stable_id}: another writer published {bundle_dir} during the fetch; kept its bundle")
        return 0
    print(f"[OK] hydrated {stable_id} at {bundle_dir}")
    return 0


def main_push(argv: list[str]) -> int:
    """Entry for ``stems push --missing`` from the CLI parser."""
    data_dir: Path | None = None
    dry_run = False
    i = 0
    while i < len(argv):
        if argv[i] == "--data-dir" and i + 1 < len(argv):
            data_dir = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            raise SystemExit(f"[ERROR] unknown stems push flag: {argv[i]!r}")
    return push_missing(data_dir=data_dir, dry_run=dry_run)


def main_hydrate(stable_id: str, argv: list[str]) -> int:
    """Entry for ``stems hydrate <stable_id>`` from the CLI parser."""
    manifest_path: Path | None = None
    data_dir: Path | None = None
    dry_run = False
    i = 0
    while i < len(argv):
        if argv[i] == "--manifest" and i + 1 < len(argv):
            manifest_path = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--data-dir" and i + 1 < len(argv):
            data_dir = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            raise SystemExit(f"[ERROR] unknown stems hydrate flag: {argv[i]!r}")
    if manifest_path is None:
        raise SystemExit("[ERROR] stems hydrate requires --manifest PATH")
    try:
        return hydrate(
            stable_id,
            manifest_path=manifest_path,
            data_dir=data_dir,
            dry_run=dry_run,
        )
    except (HydrationError, ValueError, asset_store.AssetStoreError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


__all__ = ["hydrate", "main_hydrate", "main_push", "push_missing"]
