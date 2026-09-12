"""CloudSync stems push/hydrate bodies for ``python -m apps.lyrics stems``.

``stems push --missing`` delegates to :mod:`scripts.local_stems_to_r2` (work
set = local minus R2-at-matching-size, verify by re-listing, journal at
``<data_dir>/state/stem-r2-migration.jsonl``). ``stems hydrate`` fetches each
part by ``files_sha256`` via :func:`apps.cloud.asset_store.fetch_asset`` and
verifies with :func:`apps.webui.server.stem_artifacts.load_stem_bundle`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from apps.cloud import asset_store, policy
from apps.cloud.eviction import HydrationError
from apps.lyrics.artifacts import asset_clients_for_mode
from apps.shared.paths import DATA_DIR, PROJECT_ROOT, STATE_DIR
from apps.webui.server.stem_artifacts import ROFORMER_STEMS_DIR, load_stem_bundle

STEM_ASSET_KIND = "stem_bundle"


def _stems_root(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir / "state" / "stems-roformer-spike"
    return ROFORMER_STEMS_DIR


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
    from scripts.local_stems_to_r2 import main as rail_main

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

    bundle_dir.mkdir(parents=True, exist_ok=True)
    for part, rel in files.items():
        dest = bundle_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        fetch_asset = asset_store.fetch_asset
        fetch_asset(cfg, s3, hashes[part], dest)

    shutil.copy2(manifest_path, bundle_dir / "manifest.json")
    load_stem_bundle(stable_id, stems_dir=stems_root)
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


__all__ = ["hydrate", "push_missing", "main_hydrate", "main_push"]
