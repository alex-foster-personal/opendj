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
import sys
import time
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
#: ``hydrate_one`` writes ``<stable_id>.hydrate-pin`` (a file, so scans skip
#: it) BEFORE it publishes a bundle and refreshes it once the bundle is
#: verified. Eviction in ANY process skips a bundle whose pin is fresh, which
#: covers the window between the publishing rename and the deck reading it,
#: when the in-process open-deck registry does not name it yet. A pin older
#: than ``HYDRATE_PIN_TTL_S`` is a crashed hydrate's leftover and is removed.
HYDRATE_PIN_SUFFIX: str = ".hydrate-pin"
HYDRATE_PIN_TTL_S: float = 300.0

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
        bundle = _scan_one(child)
        if bundle is not None:
            bundles.append(bundle)
    bundles.sort(key=lambda bundle: (bundle.newest_atime, bundle.stable_id))
    return bundles


def _scan_one(child: Path) -> LocalBundle | None:
    """One bundle's size, recency and fingerprint, or None when a concurrent
    eviction (or re-render) removed it, or one of its files, mid-scan."""
    size, newest_atime = 0, 0.0
    names: set[str] = set()
    stamps: list[tuple[str, int, int]] = []
    try:
        for file_path in child.rglob("*"):
            if file_path.is_file():
                stat_result = file_path.stat()
                size += stat_result.st_size
                newest_atime = max(newest_atime, stat_result.st_atime)
                name = file_path.relative_to(child).as_posix()
                names.add(name)
                stamps.append((name, stat_result.st_size, stat_result.st_mtime_ns))
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not names and not child.is_dir():
        return None
    fingerprint = hashlib.sha256(json.dumps(sorted(stamps)).encode("utf-8")).hexdigest()
    return LocalBundle(child.name, child, size, newest_atime, frozenset(names), fingerprint)


def current_fingerprint(bundle_dir: Path) -> str | None:
    """The bundle's fingerprint as it stands now, or None when it is gone."""
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        return None
    bundle = _scan_one(bundle_dir)
    return None if bundle is None else bundle.fingerprint


def _remove_tree(path: Path) -> None:
    """``rmtree`` that treats an entry already gone as removed: a sweep and a
    claimant may delete the same abandoned claim at once."""

    def ignore_gone(_func: object, _path: str, error: BaseException) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=ignore_gone)
    else:
        shutil.rmtree(path, onerror=lambda func, p, info: ignore_gone(func, p, info[1]))


def sweep_abandoned_claims(stems_dir: Path) -> list[str]:
    """Finish removing claims a pass renamed but could not delete (an I/O or
    permission error mid-``rmtree``). Scans skip ``.evicting-`` names, so
    without this their bytes would never be counted or freed again. Returns
    the claims still left; a failure here is retried on the next pass."""
    root = Path(stems_dir)
    if not root.is_dir():
        return []
    left: list[str] = []
    for child in sorted(root.iterdir()):
        if EVICTING_MARKER not in child.name or child.is_symlink():
            continue
        try:
            _remove_tree(child)
        except OSError:
            left.append(child.name)
    return left


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


def hydrate_pin_path(bundle_dir: Path) -> Path:
    return bundle_dir.with_name(f"{bundle_dir.name}{HYDRATE_PIN_SUFFIX}")


def pin_for_hydrate(bundle_dir: Path) -> None:
    """Create or refresh the pin that keeps ``bundle_dir`` off every eviction
    pass for ``HYDRATE_PIN_TTL_S``."""
    pin = hydrate_pin_path(bundle_dir)
    pin.touch()
    os.utime(pin)


def unpin_hydrate(bundle_dir: Path) -> None:
    hydrate_pin_path(bundle_dir).unlink(missing_ok=True)


def is_hydrate_pinned(bundle_dir: Path, *, now: float | None = None) -> bool:
    """True while a fresh pin names this bundle. A stale pin is removed."""
    pin = hydrate_pin_path(bundle_dir)
    try:
        age = (time.time() if now is None else now) - pin.stat().st_mtime
    except FileNotFoundError:
        return False
    if age < HYDRATE_PIN_TTL_S:
        return True
    try:
        pin.unlink(missing_ok=True)
    except OSError:
        pass  # best effort; an unreadable leftover is retried next pass
    return False


def claim_and_remove(bundle_dir: Path) -> bool:
    """Remove a bundle directory, or return False when another pass got it first.

    The timer and a post-hydrate pass can pick the same LRU bundle. The rename
    to a unique name is the claim: exactly one ``os.rename`` of the directory
    succeeds and the loser sees FileNotFoundError and reports nothing freed.
    Only ``sweep_abandoned_claims`` may delete the same claim at once, and
    both tolerate entries the other already removed.
    """
    claimed = bundle_dir.with_name(f"{bundle_dir.name}{EVICTING_MARKER}{secrets.token_hex(4)}")
    try:
        os.rename(bundle_dir, claimed)
    except FileNotFoundError:
        return False
    _remove_tree(claimed)  # a failure leaves the claim for sweep_abandoned_claims
    return True


__all__ = [
    "EVICTING_MARKER",
    "HYDRATE_PIN_SUFFIX",
    "HYDRATE_PIN_TTL_S",
    "IN_FLIGHT_MARKER",
    "REASON_CONTENT_DIFFERS",
    "REASON_FILES_DIFFER",
    "REASON_GONE",
    "REASON_NOT_IN_INDEX",
    "LocalBundle",
    "StemAssetIndex",
    "claim_and_remove",
    "current_fingerprint",
    "hydrate_pin_path",
    "index_gap",
    "is_hydrate_pinned",
    "pin_for_hydrate",
    "scan_bundles",
    "sweep_abandoned_claims",
    "unconfirmed_reason",
    "unpin_hydrate",
]
