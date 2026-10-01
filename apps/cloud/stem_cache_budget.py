"""Disk-aware stem cache budget: decide and act, without losing a render.

The stem cache used to be capped by one fixed number (102400 MB in
:mod:`apps.cloud.policy`). A fixed cap is wrong in both directions: on a
460 GiB laptop it lets the cache fill the volume, and it knows nothing about
what else is using the disk. Here the budget is DERIVED from the volume: the
cache may be as large as it likes so long as a floor stays free.

    floor  = max(floor_gib, floor_fraction * volume)      (20 GiB or 5%)
    budget = cache + free - floor                         (never below 0)

Why 20 GiB or 5% (changed Thu 1 Oct 2026 from 30 GiB or 10%): the floor is
what the REST of the machine needs free, not a comfort margin. Measured on the
460 GiB preview laptop: swap peaks at 8.2 GiB while the vocals drain runs, one
full set's worth of stems hydrated back is about 7 GiB (200 bundles at 34 MB),
and the state database with its working copies is under 2 GiB. That is 17 GiB,
so 20 GiB absolute. The old 46 GiB floor there read as low disk at 39 GiB free
with nothing in the app short of space. The fraction keeps the floor growing
on large volumes, where other software expects proportionally more headroom.

Two containment rules bound what being wrong can cost, and they are the
point of this module:

* A bundle is evicted ONLY when the R2 index holds it byte for byte: the same
  file names, and a sha256 of each local file equal to the indexed digest. A
  bundle that fails that test is the only copy of a render that cost GPU
  time. It is queued for upload and surfaced, never removed.
* Nothing is evicted on a machine that cannot hydrate (``can_rehydrate``
  false). The bytes would be safe in R2, but the deck would have no way to
  get them back, so low disk is reported instead of acted on.

Pure with respect to the engine: no FastAPI, no globals, and the disk
measurement is injectable. :mod:`apps.cloud.stem_hydration` calls
:func:`enforce` after every hydrate and the engine calls it on a timer.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 STEM-39 the budget is derived from free disk and a floor.
    [if] 5% of the volume exceeds 20 GiB [then] 5% is the floor
    [if] free disk shrinks [then] the budget shrinks with it
    [if] the floor cannot be met even with an empty cache [then] budget is 0
  ✔︎ ✅ 🎯 STEM-40 eviction is gated on the R2 index.
    [if] a bundle is absent from the index [then ⛔️] it is never evicted
    [if] a local file's sha256 differs from the index [then ⛔️] never evicted
    [if] a bundle is local-only [then] it is in the upload queue
  ✔︎ ✅ 🎯 STEM-41 eviction is minimal.
    [if] free disk is at or above the floor [then ⛔️] nothing is evicted
    [if] short by one bundle [then] exactly the LRU bundle goes
    [if] no hydration source is armed [then ⛔️] nothing is evicted
  ✔︎ ✅ 🎯 STEM-42 a loaded or playing bundle is never evicted.
    [if] a bundle is in ``protected`` [then ⛔️] it is skipped, whatever its age
  ✔︎ ✅ 🎯 STEM-43 low disk is visible and the knobs are overridable.
    [if] free disk is under the floor [then] status reports ``low_disk``
    [if] a setting is malformed [then ⛔️] loading raises, no silent default
    [if] status is read [then ⛔️] nothing is written

-Claude
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from apps.cloud.stem_cache_settings import (
    DEFAULT_ENFORCE_INTERVAL_S,
    DEFAULT_FLOOR_FRACTION,
    DEFAULT_FLOOR_GIB,
    SETTINGS_FILENAME,
    StemCacheSettings,
    StemCacheSettingsError,
    load_settings,
    merged_settings,
    save_settings,
    settings_from_mapping,
    settings_path,
    write_json_atomically,
)

GIB: int = 1024**3
UPLOAD_QUEUE_FILENAME: str = "stem-upload-queue.json"
UPLOAD_QUEUE_SCHEMA_VERSION: int = 1
_HASH_CHUNK_BYTES: int = 4 * 1024 * 1024
#: ``hydrate_one`` fetches into ``<stable_id>.tmp-hydrate-<random>`` beside the
#: bundles. That directory is a download in progress, not a bundle: it is
#: neither evicted nor queued for upload.
IN_FLIGHT_MARKER: str = ".tmp-hydrate-"

REASON_NOT_IN_INDEX: str = "not_in_r2_index"
REASON_FILES_DIFFER: str = "file_set_differs_from_r2_index"
REASON_CONTENT_DIFFERS: str = "content_differs_from_r2_index"

BLOCKED_HYDRATION_NOT_ARMED: str = "hydration_not_armed"
BLOCKED_AUTO_EVICT_OFF: str = "auto_evict_off"
BLOCKED_NOTHING_EVICTABLE: str = "not_enough_evictable_bundles"

CacheState = Literal["healthy", "low_disk"]
StemAssetIndex = Mapping[str, Mapping[str, str]]


# ----- disk and budget math ----------------------------------------------------


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    free_bytes: int


def measure_disk(path: Path) -> DiskUsage:
    """Usage of the volume that actually holds ``path``. A stems directory
    that is a symlink reports the volume it points at, which is the one the
    bundles occupy; one that does not exist yet reports the volume it would
    be created on."""
    probe = Path(path).resolve()
    while not probe.exists():
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return DiskUsage(total_bytes=usage.total, free_bytes=usage.free)


def floor_bytes(disk_total_bytes: int, settings: StemCacheSettings) -> int:
    """The space that must stay free: the larger of the absolute floor and
    the fraction of the volume."""
    return max(int(settings.floor_gib * GIB), int(settings.floor_fraction * disk_total_bytes))


def derived_budget_bytes(
    cache_bytes: int, disk: DiskUsage, settings: StemCacheSettings
) -> int:
    """How large the cache may be right now.

    Everything the cache gives up becomes free space, so the largest cache
    that still leaves the floor free is ``cache + free - floor``.
    """
    budget = max(0, cache_bytes + disk.free_bytes - floor_bytes(disk.total_bytes, settings))
    if settings.max_cache_gib is not None:
        budget = min(budget, int(settings.max_cache_gib * GIB))
    return budget


# ----- local bundles and R2 confirmation ---------------------------------------


@dataclass(frozen=True)
class LocalBundle:
    stable_id: str
    path: Path
    size_bytes: int
    newest_atime: float
    filenames: frozenset[str]


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
        if not child.is_dir() or child.is_symlink() or IN_FLIGHT_MARKER in child.name:
            continue
        size, newest_atime = 0, 0.0
        names: set[str] = set()
        for file_path in child.rglob("*"):
            if file_path.is_file():
                stat_result = file_path.stat()
                size += stat_result.st_size
                newest_atime = max(newest_atime, stat_result.st_atime)
                names.add(file_path.relative_to(child).as_posix())
        bundles.append(LocalBundle(child.name, child, size, newest_atime, frozenset(names)))
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
    for filename in sorted(bundle.filenames):
        if _sha256_of(bundle.path / filename) != entry[filename]:
            return REASON_CONTENT_DIFFERS
    return None


# ----- upload queue --------------------------------------------------------------


def upload_queue_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / UPLOAD_QUEUE_FILENAME


def load_upload_queue(data_dir: Path) -> dict[str, dict[str, object]]:
    """``{stable_id: {reason, bytes, queued_at}}`` for bundles awaiting upload."""
    path = upload_queue_path(data_dir)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(payload["bundles"])


def _save_upload_queue(data_dir: Path, queue: dict[str, dict[str, object]]) -> None:
    path = upload_queue_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(
        path, {"schema_version": UPLOAD_QUEUE_SCHEMA_VERSION, "bundles": queue}
    )


def _refreshed_upload_queue(
    previous: dict[str, dict[str, object]],
    bundles: list[LocalBundle],
    index: StemAssetIndex,
    content_differs: set[str],
    now_iso: str,
) -> dict[str, dict[str, object]]:
    """The queue as it should stand after this pass.

    A bundle is queued while it is on disk and the index does not cover it.
    One previously queued for differing content is re-hashed (a handful at
    most) so it leaves the queue as soon as a publish catches up.
    """
    queue: dict[str, dict[str, object]] = {}
    for bundle in bundles:
        reason = index_gap(bundle, index)
        if reason is None and bundle.stable_id in content_differs:
            reason = REASON_CONTENT_DIFFERS
        elif (
            reason is None
            and previous.get(bundle.stable_id, {}).get("reason") == REASON_CONTENT_DIFFERS
        ):
            reason = unconfirmed_reason(bundle, index)
        if reason is None:
            continue
        queued_at = previous.get(bundle.stable_id, {}).get("queued_at", now_iso)
        queue[bundle.stable_id] = {
            "reason": reason,
            "bytes": bundle.size_bytes,
            "queued_at": queued_at,
        }
    return queue


# ----- enforcement -----------------------------------------------------------------


@dataclass(frozen=True)
class EnforceReport:
    """What one enforcement pass measured, decided and did."""

    at_utc: str
    state: CacheState
    dry_run: bool
    disk_total_bytes: int
    disk_free_bytes: int
    floor_bytes: int
    shortfall_bytes: int
    cache_bytes: int
    budget_bytes: int
    evicted_stable_ids: tuple[str, ...]
    bytes_freed: int
    queued_for_upload: tuple[str, ...]
    protected_count: int
    blocked_reason: str | None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["evicted_stable_ids"] = list(self.evicted_stable_ids)
        payload["queued_for_upload"] = list(self.queued_for_upload)
        return payload


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@dataclass(frozen=True)
class _CachePosition:
    """One measurement of the cache against the free-disk floor, shared by
    ``enforce`` and ``status`` so the two cannot disagree on the arithmetic."""

    settings: StemCacheSettings
    usage: DiskUsage
    bundles: list[LocalBundle]
    cache_bytes: int
    floor_bytes: int
    shortfall_bytes: int
    budget_bytes: int
    over_budget_bytes: int

    @property
    def state(self) -> CacheState:
        return "low_disk" if self.shortfall_bytes > 0 else "healthy"


def _measure_cache_position(
    stems_dir: Path,
    data_dir: Path,
    settings: StemCacheSettings | None,
    disk: DiskUsage | None,
) -> _CachePosition:
    resolved = settings if settings is not None else load_settings(data_dir)
    usage = disk if disk is not None else measure_disk(stems_dir)
    bundles = scan_bundles(stems_dir)
    cache = sum(bundle.size_bytes for bundle in bundles)
    floor = floor_bytes(usage.total_bytes, resolved)
    budget = derived_budget_bytes(cache, usage, resolved)
    return _CachePosition(
        settings=resolved,
        usage=usage,
        bundles=bundles,
        cache_bytes=cache,
        floor_bytes=floor,
        shortfall_bytes=max(0, floor - usage.free_bytes),
        budget_bytes=budget,
        over_budget_bytes=max(0, cache - budget),
    )


def _blocked_before_eviction(
    need_bytes: int, settings: StemCacheSettings, can_rehydrate: bool
) -> str | None:
    """Why nothing may be evicted at all, or None when eviction may proceed."""
    if need_bytes <= 0:
        return None
    if not settings.auto_evict:
        return BLOCKED_AUTO_EVICT_OFF
    if not can_rehydrate:
        return BLOCKED_HYDRATION_NOT_ARMED
    return None


def _protected_count(bundles: list[LocalBundle], protected: frozenset[str]) -> int:
    return sum(1 for bundle in bundles if bundle.stable_id in protected)


def _would_evict(evictable: list[LocalBundle], need_bytes: int) -> tuple[int, int]:
    """(count, bytes) reaching the floor would remove: least recently used
    first, the same walk as ``_evict_lru`` minus the content hash (status
    hashes nothing)."""
    count, total_bytes = 0, 0
    for bundle in evictable:
        if total_bytes >= need_bytes:
            break
        count += 1
        total_bytes += bundle.size_bytes
    return count, total_bytes


def _evict_lru(
    bundles: list[LocalBundle],
    *,
    need_bytes: int,
    index: StemAssetIndex,
    protected: frozenset[str],
    dry_run: bool,
    remove: Callable[[Path], None],
) -> tuple[list[str], int, set[str]]:
    """Remove least-recently-used bundles until ``need_bytes`` is freed.

    Returns (evicted ids, bytes freed, ids skipped for differing content).
    A bundle is removed only when it is not protected AND R2 holds it byte
    for byte; anything else is skipped and the walk continues to the next.
    """
    evicted: list[str] = []
    content_differs: set[str] = set()
    freed = 0
    for bundle in bundles:
        if freed >= need_bytes:
            break
        if bundle.stable_id in protected:
            continue
        reason = unconfirmed_reason(bundle, index)
        if reason is not None:
            if reason == REASON_CONTENT_DIFFERS:
                content_differs.add(bundle.stable_id)
            continue
        if not dry_run:
            remove(bundle.path)
        evicted.append(bundle.stable_id)
        freed += bundle.size_bytes
    return evicted, freed, content_differs


def enforce(  # noqa: PLR0913 - each argument is one independent input to the decision
    stems_dir: Path,
    *,
    data_dir: Path,
    index: StemAssetIndex,
    protected: frozenset[str],
    can_rehydrate: bool,
    settings: StemCacheSettings | None = None,
    disk: DiskUsage | None = None,
    dry_run: bool = False,
    max_evict_bytes: int | None = None,
) -> EnforceReport:
    """Measure the volume and evict just enough R2-confirmed bundles to bring
    free space back to the floor. Refreshes the upload queue on the way.

    ``protected`` is the loaded-or-playing set (``OPEN_DECKS.open_ids()``).
    ``can_rehydrate`` is whether this machine has a hydration source armed.
    ``disk`` and ``settings`` are injectable; production omits both.
    ``max_evict_bytes`` bounds one pass. The post-hydrate call passes the
    bytes it just fetched, so a deck load pays to hash about one bundle, not
    the whole backlog; the timer, which nobody is waiting on, passes nothing
    and catches up in full.
    """
    position = _measure_cache_position(stems_dir, data_dir, settings, disk)
    bundles, usage = position.bundles, position.usage
    need = position.over_budget_bytes
    if max_evict_bytes is not None:
        need = min(need, max_evict_bytes)

    evicted: list[str] = []
    freed = 0
    content_differs: set[str] = set()
    blocked = _blocked_before_eviction(need, position.settings, can_rehydrate)
    if need > 0 and blocked is None:
        evicted, freed, content_differs = _evict_lru(
            bundles,
            need_bytes=need,
            index=index,
            protected=protected,
            dry_run=dry_run,
            remove=shutil.rmtree,
        )
        if freed < need:
            blocked = BLOCKED_NOTHING_EVICTABLE

    now_iso = _utc_now_iso()
    evicted_set = set(evicted)
    remaining = [bundle for bundle in bundles if bundle.stable_id not in evicted_set]
    previous = load_upload_queue(data_dir)
    queue = _refreshed_upload_queue(previous, remaining, index, content_differs, now_iso)
    if not dry_run and queue != previous:
        _save_upload_queue(data_dir, queue)

    return EnforceReport(
        at_utc=now_iso,
        state=position.state,
        dry_run=dry_run,
        disk_total_bytes=usage.total_bytes,
        disk_free_bytes=usage.free_bytes,
        floor_bytes=position.floor_bytes,
        shortfall_bytes=position.shortfall_bytes,
        cache_bytes=position.cache_bytes,
        budget_bytes=position.budget_bytes,
        evicted_stable_ids=tuple(evicted),
        bytes_freed=freed,
        queued_for_upload=tuple(sorted(queue)),
        protected_count=_protected_count(bundles, protected),
        blocked_reason=blocked,
    )


# ----- status (read-only) ------------------------------------------------------------


def status(
    stems_dir: Path,
    *,
    data_dir: Path,
    index: StemAssetIndex,
    protected: frozenset[str],
    can_rehydrate: bool,
    settings: StemCacheSettings | None = None,
    disk: DiskUsage | None = None,
) -> dict[str, object]:
    """Where the cache stands against the floor. Writes nothing and hashes
    nothing, so it is safe to poll from a health indicator."""
    position = _measure_cache_position(stems_dir, data_dir, settings, disk)
    bundles, usage = position.bundles, position.usage
    need = position.over_budget_bytes

    local_only = [bundle for bundle in bundles if index_gap(bundle, index) is not None]
    local_only_ids = {bundle.stable_id for bundle in local_only}
    evictable = [
        bundle
        for bundle in bundles
        if bundle.stable_id not in local_only_ids and bundle.stable_id not in protected
    ]
    evictable_bytes = sum(bundle.size_bytes for bundle in evictable)
    would_evict_count, would_evict_bytes = _would_evict(evictable, need)

    blocked = _blocked_before_eviction(need, position.settings, can_rehydrate)
    if need > 0 and blocked is None and evictable_bytes < need:
        blocked = BLOCKED_NOTHING_EVICTABLE

    return {
        "state": position.state,
        "stems_dir": str(stems_dir),
        "disk_total_bytes": usage.total_bytes,
        "disk_free_bytes": usage.free_bytes,
        "floor_bytes": position.floor_bytes,
        "shortfall_bytes": position.shortfall_bytes,
        "cache_bytes": position.cache_bytes,
        "budget_bytes": position.budget_bytes,
        "over_budget_bytes": need,
        "bundle_count": len(bundles),
        "evictable_bundle_count": len(evictable),
        "evictable_bytes": evictable_bytes,
        "would_evict_count": would_evict_count,
        "would_evict_bytes": would_evict_bytes,
        "local_only_count": len(local_only),
        "local_only_bytes": sum(bundle.size_bytes for bundle in local_only),
        "local_only_stable_ids": sorted(local_only_ids),
        "upload_queue_count": len(load_upload_queue(data_dir)),
        "protected_count": _protected_count(bundles, protected),
        "can_rehydrate": can_rehydrate,
        "blocked_reason": blocked,
        "settings": asdict(position.settings),
    }


__all__ = [
    "BLOCKED_AUTO_EVICT_OFF",
    "BLOCKED_HYDRATION_NOT_ARMED",
    "BLOCKED_NOTHING_EVICTABLE",
    "DEFAULT_ENFORCE_INTERVAL_S",
    "DEFAULT_FLOOR_FRACTION",
    "DEFAULT_FLOOR_GIB",
    "GIB",
    "IN_FLIGHT_MARKER",
    "REASON_CONTENT_DIFFERS",
    "REASON_FILES_DIFFER",
    "REASON_NOT_IN_INDEX",
    "SETTINGS_FILENAME",
    "UPLOAD_QUEUE_FILENAME",
    "DiskUsage",
    "EnforceReport",
    "LocalBundle",
    "StemCacheSettings",
    "StemCacheSettingsError",
    "derived_budget_bytes",
    "enforce",
    "floor_bytes",
    "index_gap",
    "load_settings",
    "load_upload_queue",
    "measure_disk",
    "merged_settings",
    "save_settings",
    "scan_bundles",
    "settings_from_mapping",
    "settings_path",
    "status",
    "unconfirmed_reason",
    "upload_queue_path",
]
