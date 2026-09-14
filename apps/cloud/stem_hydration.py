"""Hydrate R2-indexed stem bundles onto local disk (ADR-0020).

Composes existing primitives rather than duplicating them: content-address
fetch is :func:`apps.cloud.asset_store.fetch_asset`, the mapping is
:mod:`apps.cloud.stem_index`, the budget comes from
:data:`apps.cloud.policy.CFG`, and the strict re-verify after writing is
:func:`apps.webui.server.stem_artifacts.load_stem_bundle` -- ``_load_v1_bundle``
itself is never touched; this module only ever calls the public loader.

Two entry points:

* :func:`hydrate_one` -- one bundle, used both by the on-demand deck-load
  path (``routes/stems.py``, which NEVER skips the reservation guard: a
  deck-load IS the in-app ordering test the reservation exists to allow) and
  by the bulk path below (which DOES skip reserved ids by default).
* :func:`bulk_hydrate` -- many bundles within a byte budget, for the CLI and
  its parity HTTP endpoint.

* [if] a bundle is already valid on local disk [then] hydration is a no-op
  that costs one strict-load, never a network call.
* [if] the R2 index has no entry for a stable_id [then] hydration reports
  ``unavailable`` and touches no network beyond the index lookup already
  done by the caller.
* [if] a bulk request names a reserved id and ``include_reserved`` is not
  explicitly ``True`` [then] that id is skipped and reported with reason
  ``"reserved"``.
* [if] a bundle is open on a deck [then] budget enforcement never evicts it,
  regardless of its recency.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.cloud import asset_store, policy, stem_index
from apps.cloud.asset_store import AssetS3Client
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import BYTES_PER_MB, HydrationError
from apps.webui.server.stem_artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundleNotFoundError,
    load_stem_bundle,
)

RESERVATION_FILENAME: str = "stem-order-reserved-100.json"
STEM_ASSET_KIND: str = "stem_bundle"

HydrationStatus = Literal[
    "already_local", "hydrated", "unavailable", "reserved_skip", "error"
]


# --- reservation guard (D5) ---------------------------------------------------


def reservation_file_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / RESERVATION_FILENAME


def load_reserved_ids(data_dir: Path) -> frozenset[str]:
    """The stable_ids carved out for in-app ordering tests.

    A missing file is an empty reservation, not an error: most machines,
    and every test that does not care about the guard, never write one.
    A present file with the wrong shape IS an error -- a malformed
    reservation file must never be silently read as "nothing reserved".
    """
    path = reservation_file_path(data_dir)
    if not path.is_file():
        return frozenset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HydrationError(f"{path} is not valid JSON: {exc}") from exc
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        raise HydrationError(f"{path} must have a top-level 'tracks' array")
    return frozenset(str(entry["stable_id"]) for entry in tracks)


# --- open-deck protection ------------------------------------------------------


class OpenDeckRegistry:
    """Which stable_ids are currently loaded on a deck, in this process.

    Refcounted so two decks (or a deck plus a preview strip) holding the
    same track double-count correctly. Eviction consults this and a bundle
    it names is never removed regardless of recency. Deliberately in-memory
    and per-process: losing it on restart is safe because nothing is holding
    an open deck yet at that point.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, int] = {}

    def mark_open(self, stable_id: str) -> None:
        with self._lock:
            self._open[stable_id] = self._open.get(stable_id, 0) + 1

    def mark_closed(self, stable_id: str) -> None:
        with self._lock:
            if stable_id not in self._open:
                return
            remaining = self._open[stable_id] - 1
            if remaining <= 0:
                del self._open[stable_id]
            else:
                self._open[stable_id] = remaining

    def is_open(self, stable_id: str) -> bool:
        with self._lock:
            return self._open.get(stable_id, 0) > 0

    def open_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._open)


#: Process-wide registry shared by the deck-load route and eviction.
OPEN_DECKS = OpenDeckRegistry()


# --- outcomes -------------------------------------------------------------------


@dataclass(frozen=True)
class HydrationOutcome:
    stable_id: str
    status: HydrationStatus
    bytes_fetched: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class BulkHydrateReport:
    fetched: tuple[HydrationOutcome, ...]
    skipped: tuple[HydrationOutcome, ...]
    bytes_fetched: int


@dataclass(frozen=True)
class EvictionOutcome:
    evicted_stable_ids: tuple[str, ...]
    bytes_freed: int
    bytes_remaining: int
    budget_bytes: int


# --- single-bundle hydration ----------------------------------------------------


def _is_local(stable_id: str, stems_dir: Path) -> bool:
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir)
    except (StemBundleNotFoundError, StemArtifactError):
        return False
    return True


def _fetch_bundle_files(
    cfg: CloudConfig,
    s3: AssetS3Client,
    *,
    stable_id: str,
    file_hashes: dict[str, str],
    bundle_dir: Path,
) -> int:
    """Fetch every file the index names, into a tmp dir, then publish it as
    ``bundle_dir`` with one atomic rename. A failure anywhere leaves no
    partial bundle behind -- a half-written directory would strict-load-fail
    forever until someone notices and deletes it by hand."""
    tmp_dir = bundle_dir.parent / f"{stable_id}.tmp-hydrate-{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    total = 0
    try:
        for filename, digest in file_hashes.items():
            dest = tmp_dir / filename
            asset_store.fetch_asset(cfg, s3, digest, dest)
            total += dest.stat().st_size
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.rename(bundle_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return total


def hydrate_one(
    stable_id: str,
    *,
    data_dir: Path,
    cfg: CloudConfig,
    s3: AssetS3Client,
    index: stem_index.StemAssetIndex,
    stems_dir: Path | None = None,
    skip_reserved: bool = False,
    reserved_ids: frozenset[str] | None = None,
) -> HydrationOutcome:
    """Hydrate one bundle from R2, or explain precisely why it did not.

    ``skip_reserved`` is the BULK-path-only guard: the on-demand deck-load
    caller must always pass ``False`` (its default) because opening a deck
    on a reserved track is exactly the in-app ordering test the reservation
    protects, not something to skip.
    """
    root = stems_dir or DEFAULT_STEMS_DIR
    if _is_local(stable_id, root):
        return HydrationOutcome(stable_id, "already_local")

    if skip_reserved:
        reserved = (
            reserved_ids if reserved_ids is not None else load_reserved_ids(data_dir)
        )
        if stable_id in reserved:
            return HydrationOutcome(stable_id, "reserved_skip", reason="reserved")

    file_hashes = index.get(stable_id)
    if not file_hashes:
        return HydrationOutcome(stable_id, "unavailable", reason="not in R2 stem index")
    if stem_index.MANIFEST_FILENAME not in file_hashes:
        return HydrationOutcome(
            stable_id,
            "unavailable",
            reason="index has no manifest.json hash for this bundle",
        )

    bundle_dir = root / stable_id
    try:
        total = _fetch_bundle_files(
            cfg, s3, stable_id=stable_id, file_hashes=file_hashes, bundle_dir=bundle_dir
        )
        load_stem_bundle(stable_id, stems_dir=root)  # strict re-verify, never mocked
    except (
        asset_store.AssetStoreError,
        StemArtifactError,
        StemBundleNotFoundError,
        OSError,
    ) as exc:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        return HydrationOutcome(stable_id, "error", reason=str(exc))

    enforce_budget(root, protected=OPEN_DECKS.open_ids())
    return HydrationOutcome(stable_id, "hydrated", bytes_fetched=total)


# --- bulk hydration --------------------------------------------------------------


def bulk_hydrate(
    stable_ids: Sequence[str],
    *,
    data_dir: Path,
    cfg: CloudConfig,
    s3: AssetS3Client,
    index: stem_index.StemAssetIndex,
    byte_budget: int,
    include_reserved: bool = False,
    stems_dir: Path | None = None,
) -> BulkHydrateReport:
    """Hydrate as many of ``stable_ids`` as fit ``byte_budget``, in order.

    Order is preserved from the caller (a playlist's own track order, or an
    explicit id list), so the front of a playlist wins the budget first.
    Reserved ids are skipped and reported unless ``include_reserved=True`` is
    passed explicitly -- there is no other way to include them, which is the
    "refuse without an explicit flag" contract.
    """
    reserved = load_reserved_ids(data_dir)
    fetched: list[HydrationOutcome] = []
    skipped: list[HydrationOutcome] = []
    bytes_used = 0
    for stable_id in stable_ids:
        if bytes_used >= byte_budget:
            skipped.append(
                HydrationOutcome(stable_id, "unavailable", reason="byte_budget exhausted")
            )
            continue
        outcome = hydrate_one(
            stable_id,
            data_dir=data_dir,
            cfg=cfg,
            s3=s3,
            index=index,
            stems_dir=stems_dir,
            skip_reserved=not include_reserved,
            reserved_ids=reserved,
        )
        if outcome.status in ("hydrated", "already_local"):
            bytes_used += outcome.bytes_fetched
            fetched.append(outcome)
        else:
            skipped.append(outcome)
    return BulkHydrateReport(
        fetched=tuple(fetched), skipped=tuple(skipped), bytes_fetched=bytes_used
    )


# --- budget-bounded LRU eviction, bundle-granular --------------------------------


def enforce_budget(
    stems_dir: Path,
    *,
    protected: frozenset[str] = frozenset(),
    budget_mb: int | None = None,
) -> EvictionOutcome:
    """LRU-evict whole bundle directories over the policy budget.

    Bundle-granular, unlike :func:`apps.cloud.eviction.evict_cache`'s
    file-granular LRU: a stem bundle is 4-5 files that must survive or die
    together, so evicting half of one would leave an unloadable directory
    that the strict loader reports as CORRUPT, not simply absent. A
    bundle's recency is its newest file's atime, so soloing one stem still
    counts the whole bundle as recently used. ``protected`` (the open-deck
    set) is never evicted regardless of recency.
    """
    budget = (
        budget_mb
        if budget_mb is not None
        else policy.CFG.artifacts[STEM_ASSET_KIND].cache_budget_mb
    )
    budget_bytes = budget * BYTES_PER_MB
    root = Path(stems_dir)
    if not root.is_dir():
        return EvictionOutcome((), 0, 0, budget_bytes)

    bundles: list[tuple[float, str, Path, int]] = []
    total = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        newest_atime = 0.0
        size = 0
        for file_path in child.rglob("*"):
            if file_path.is_file():
                stat_result = file_path.stat()
                size += stat_result.st_size
                newest_atime = max(newest_atime, stat_result.st_atime)
        total += size
        bundles.append((newest_atime, child.name, child, size))

    bundles.sort(key=lambda item: (item[0], item[1]))
    evicted: list[str] = []
    freed = 0
    for _atime, stable_id, path, size in bundles:
        if total - freed <= budget_bytes:
            break
        if stable_id in protected:
            continue
        shutil.rmtree(path)
        evicted.append(stable_id)
        freed += size
    return EvictionOutcome(tuple(evicted), freed, total - freed, budget_bytes)


__all__ = [
    "OPEN_DECKS",
    "RESERVATION_FILENAME",
    "STEM_ASSET_KIND",
    "BulkHydrateReport",
    "EvictionOutcome",
    "HydrationOutcome",
    "HydrationStatus",
    "OpenDeckRegistry",
    "bulk_hydrate",
    "enforce_budget",
    "hydrate_one",
    "load_reserved_ids",
    "reservation_file_path",
]
