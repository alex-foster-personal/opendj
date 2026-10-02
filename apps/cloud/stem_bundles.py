"""Local stem bundles on disk and whether R2 holds them byte for byte.

Split out of :mod:`apps.cloud.stem_cache_budget`, which re-exports every name
here; the budget, eviction and upload-queue logic that use these stay there.
The requirements (STEM-40, STEM-42) are listed in that module's docstring.

-Claude
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_HASH_CHUNK_BYTES: int = 4 * 1024 * 1024

#: ``hydrate_one`` fetches into ``<stable_id>.tmp-hydrate-<random>`` beside the
#: bundles. That directory is a download in progress, not a bundle: it is
#: neither evicted nor queued for upload.
IN_FLIGHT_MARKER: str = ".tmp-hydrate-"
#: An eviction renames a bundle to ``<stable_id>.evicting-<random>`` before
#: deleting it; scans skip it like a download in progress.
EVICTING_MARKER: str = ".evicting-"

REASON_NOT_IN_INDEX: str = "not_in_r2_index"
REASON_FILES_DIFFER: str = "file_set_differs_from_r2_index"
REASON_CONTENT_DIFFERS: str = "content_differs_from_r2_index"
#: The bundle vanished mid-hash: a concurrent pass evicted it. Never queued.
REASON_GONE: str = "bundle_removed_concurrently"

StemAssetIndex = Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class LocalBundle:
    stable_id: str
    path: Path
    size_bytes: int
    newest_atime: float
    filenames: frozenset[str]
    #: Every file's name, size and mtime_ns, digested. It changes when a bundle
    #: is re-rendered in place under the same file names, which ``index_gap``
    #: (names only) cannot see; it is what decides a bundle needs re-hashing.
    fingerprint: str = ""


def scan_bundles(stems_dir: Path) -> list[LocalBundle]:
    """Every bundle directory under ``stems_dir``, least recently used first.

    A bundle's recency is its newest file's atime, so soloing one stem counts
    the whole bundle as recently used. Symlinked children are skipped: this
    module only ever removes directories it can see are really here. So is a
    hydration still in flight.
    """
    root = Path(stems_dir)
    if not root.is_dir():
        return []
    bundles: list[LocalBundle] = []
    for child in sorted(root.iterdir()):
        if (
            not child.is_dir()
            or child.is_symlink()
            or IN_FLIGHT_MARKER in child.name
            or EVICTING_MARKER in child.name
        ):
            continue
        size, newest_atime = 0, 0.0
        names: set[str] = set()
        stamps: list[tuple[str, int, int]] = []
        for file_path in child.rglob("*"):
            if file_path.is_file():
                stat_result = file_path.stat()
                size += stat_result.st_size
                newest_atime = max(newest_atime, stat_result.st_atime)
                name = file_path.relative_to(child).as_posix()
                names.add(name)
                stamps.append((name, stat_result.st_size, stat_result.st_mtime_ns))
        fingerprint = hashlib.sha256(json.dumps(sorted(stamps)).encode("utf-8")).hexdigest()
        bundles.append(
            LocalBundle(child.name, child, size, newest_atime, frozenset(names), fingerprint)
        )
    bundles.sort(key=lambda bundle: (bundle.newest_atime, bundle.stable_id))
    return bundles


def index_gap(bundle: LocalBundle, index: StemAssetIndex) -> str | None:
    """Why the index does NOT cover this bundle, by name alone (no hashing),
    or ``None`` when every local file has an indexed digest. Cheap enough to
    run over the whole cache on every status read."""
    entry = index.get(bundle.stable_id)
    if not entry:
        return REASON_NOT_IN_INDEX
    if not bundle.filenames or not bundle.filenames.issubset(entry):
        return REASON_FILES_DIFFER
    return None


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def unconfirmed_reason(bundle: LocalBundle, index: StemAssetIndex) -> str | None:
    """Why R2 does NOT hold this bundle byte for byte, or ``None`` when it
    does. The expensive half of the gate: keys are content-addressed, so a
    local sha256 equal to the indexed digest proves the indexed object is
    these exact bytes. Run only on a bundle about to be evicted."""
    gap = index_gap(bundle, index)
    if gap is not None:
        return gap
    entry = index[bundle.stable_id]
    try:
        for filename in sorted(bundle.filenames):
            if _sha256_of(bundle.path / filename) != entry[filename]:
                return REASON_CONTENT_DIFFERS
    except FileNotFoundError:
        return REASON_GONE
    return None


def claim_and_remove(bundle_dir: Path) -> bool:
    """Remove a bundle directory, or return False when another pass got it first.

    The timer and a post-hydrate pass can pick the same LRU bundle. The rename
    to a unique name is the claim: exactly one ``os.rename`` of the directory
    succeeds, the loser sees FileNotFoundError and reports nothing freed, and
    no ``rmtree`` ever runs on a path another pass is deleting.
    """
    claimed = bundle_dir.with_name(f"{bundle_dir.name}{EVICTING_MARKER}{secrets.token_hex(4)}")
    try:
        os.rename(bundle_dir, claimed)
    except FileNotFoundError:
        return False
    shutil.rmtree(claimed)
    return True


__all__ = [
    "EVICTING_MARKER",
    "IN_FLIGHT_MARKER",
    "REASON_CONTENT_DIFFERS",
    "REASON_FILES_DIFFER",
    "REASON_GONE",
    "REASON_NOT_IN_INDEX",
    "LocalBundle",
    "StemAssetIndex",
    "claim_and_remove",
    "index_gap",
    "scan_bundles",
    "unconfirmed_reason",
]
