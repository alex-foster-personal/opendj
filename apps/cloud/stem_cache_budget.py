"""Disk-aware stem cache budget: decide and act, without losing a render.

The stem cache used to be capped by one fixed number (102400 MB in
:mod:`apps.cloud.policy`). A fixed cap is wrong in both directions: on a
460 GiB laptop it lets the cache fill the volume, and it knows nothing about
what else is using the disk. Here the budget is DERIVED from the volume: the
cache may be as large as it likes so long as a floor stays free.

    floor  = max(floor_gib, floor_fraction * volume)      (30 GiB or 10%)
    budget = cache + free - floor                         (never below 0)

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
    [if] 10% of the volume exceeds 30 GiB [then] 10% is the floor
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

GIB: int = 1024**3
DEFAULT_FLOOR_GIB: float = 30.0
DEFAULT_FLOOR_FRACTION: float = 0.10
DEFAULT_ENFORCE_INTERVAL_S: float = 300.0
SETTINGS_FILENAME: str = "stem-cache-settings.json"
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


class StemCacheSettingsError(ValueError):
    """Raised when the stem cache settings are malformed."""


# ----- settings ----------------------------------------------------------------


@dataclass(frozen=True)
class StemCacheSettings:
    """The overridable knobs. Defaults are the documented policy.

    ``max_cache_gib`` is an optional hard cap for a user who wants the cache
    small regardless of how much disk is free. It only ever LOWERS the budget.
    """

    floor_gib: float = DEFAULT_FLOOR_GIB
    floor_fraction: float = DEFAULT_FLOOR_FRACTION
    max_cache_gib: float | None = None
    enforce_interval_s: float = DEFAULT_ENFORCE_INTERVAL_S
    auto_evict: bool = True


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / SETTINGS_FILENAME


def _number(payload: Mapping[str, object], key: str, rule: str) -> float:
    """``payload[key]`` as a float. A bool is not a number here: ``true``
    would otherwise pass as 1.0."""
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StemCacheSettingsError(f"{key} must be a number {rule}; got {value!r}")
    return float(value)


def settings_from_mapping(payload: Mapping[str, object]) -> StemCacheSettings:
    """Validate a settings mapping. Unknown keys and bad values raise."""
    known = set(StemCacheSettings.__dataclass_fields__)
    unknown = set(payload).difference(known)
    if unknown:
        raise StemCacheSettingsError(
            f"unsupported stem cache setting(s): {sorted(unknown)}; known: {sorted(known)}"
        )
    merged: dict[str, object] = {**asdict(StemCacheSettings()), **payload}
    floor_gib = _number(merged, "floor_gib", ">= 0")
    fraction = _number(merged, "floor_fraction", "in [0, 1)")
    interval = _number(merged, "enforce_interval_s", "> 0")
    cap = (
        None
        if merged["max_cache_gib"] is None
        else _number(merged, "max_cache_gib", ">= 0, or null")
    )
    auto_evict = merged["auto_evict"]
    if floor_gib < 0:
        raise StemCacheSettingsError(f"floor_gib must be >= 0; got {floor_gib!r}")
    if not 0 <= fraction < 1:
        raise StemCacheSettingsError(f"floor_fraction must be in [0, 1); got {fraction!r}")
    if cap is not None and cap < 0:
        raise StemCacheSettingsError(f"max_cache_gib must be null or >= 0; got {cap!r}")
    if interval <= 0:
        raise StemCacheSettingsError(f"enforce_interval_s must be > 0; got {interval!r}")
    if not isinstance(auto_evict, bool):
        raise StemCacheSettingsError(f"auto_evict must be true or false; got {auto_evict!r}")
    return StemCacheSettings(
        floor_gib=floor_gib,
        floor_fraction=fraction,
        max_cache_gib=cap,
        enforce_interval_s=interval,
        auto_evict=auto_evict,
    )


def merged_settings(
    current: StemCacheSettings,
    overrides: Mapping[str, object],
    *,
    clear_max_cache_gib: bool = False,
) -> StemCacheSettings:
    """``current`` with ``overrides`` applied and validated. One merge shared
    by the HTTP route and the CLI verb, so both accept exactly the same
    partial update. ``clear_max_cache_gib`` removes the optional cap."""
    values: dict[str, object] = {**asdict(current), **overrides}
    if clear_max_cache_gib:
        values["max_cache_gib"] = None
    return settings_from_mapping(values)


def load_settings(data_dir: Path) -> StemCacheSettings:
    """Read the settings file. Absent is the documented defaults; present but
    malformed raises, so a typo can never silently restore a default floor."""
    path = settings_path(data_dir)
    if not path.is_file():
        return StemCacheSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StemCacheSettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StemCacheSettingsError(f"{path} must hold a JSON object")
    try:
        return settings_from_mapping(payload)
    except StemCacheSettingsError as exc:
        raise StemCacheSettingsError(f"{path}: {exc}") from exc


def save_settings(data_dir: Path, settings: StemCacheSettings) -> Path:
    validated = settings_from_mapping(asdict(settings))
    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(path, asdict(validated))
    return path


def _write_json_atomically(path: Path, payload: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


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
    _write_json_atomically(
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
    resolved = settings if settings is not None else load_settings(data_dir)
    usage = disk if disk is not None else measure_disk(stems_dir)
    bundles = scan_bundles(stems_dir)
    cache = sum(bundle.size_bytes for bundle in bundles)
    floor = floor_bytes(usage.total_bytes, resolved)
    budget = derived_budget_bytes(cache, usage, resolved)
    shortfall = max(0, floor - usage.free_bytes)
    need = max(0, cache - budget)
    if max_evict_bytes is not None:
        need = min(need, max_evict_bytes)

    blocked: str | None = None
    evicted: list[str] = []
    freed = 0
    content_differs: set[str] = set()
    if need > 0 and not resolved.auto_evict:
        blocked = BLOCKED_AUTO_EVICT_OFF
    elif need > 0 and not can_rehydrate:
        blocked = BLOCKED_HYDRATION_NOT_ARMED
    elif need > 0:
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
        state="low_disk" if shortfall > 0 else "healthy",
        dry_run=dry_run,
        disk_total_bytes=usage.total_bytes,
        disk_free_bytes=usage.free_bytes,
        floor_bytes=floor,
        shortfall_bytes=shortfall,
        cache_bytes=cache,
        budget_bytes=budget,
        evicted_stable_ids=tuple(evicted),
        bytes_freed=freed,
        queued_for_upload=tuple(sorted(queue)),
        protected_count=sum(1 for bundle in bundles if bundle.stable_id in protected),
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
    resolved = settings if settings is not None else load_settings(data_dir)
    usage = disk if disk is not None else measure_disk(stems_dir)
    bundles = scan_bundles(stems_dir)
    cache = sum(bundle.size_bytes for bundle in bundles)
    floor = floor_bytes(usage.total_bytes, resolved)
    shortfall = max(0, floor - usage.free_bytes)
    budget = derived_budget_bytes(cache, usage, resolved)
    need = max(0, cache - budget)

    local_only = [bundle for bundle in bundles if index_gap(bundle, index) is not None]
    local_only_ids = {bundle.stable_id for bundle in local_only}
    evictable = [
        bundle
        for bundle in bundles
        if bundle.stable_id not in local_only_ids and bundle.stable_id not in protected
    ]
    evictable_bytes = sum(bundle.size_bytes for bundle in evictable)

    blocked: str | None = None
    if need > 0 and not resolved.auto_evict:
        blocked = BLOCKED_AUTO_EVICT_OFF
    elif need > 0 and not can_rehydrate:
        blocked = BLOCKED_HYDRATION_NOT_ARMED
    elif need > 0 and evictable_bytes < need:
        blocked = BLOCKED_NOTHING_EVICTABLE

    return {
        "state": "low_disk" if shortfall > 0 else "healthy",
        "stems_dir": str(stems_dir),
        "disk_total_bytes": usage.total_bytes,
        "disk_free_bytes": usage.free_bytes,
        "floor_bytes": floor,
        "shortfall_bytes": shortfall,
        "cache_bytes": cache,
        "budget_bytes": budget,
        "over_budget_bytes": need,
        "bundle_count": len(bundles),
        "evictable_bundle_count": len(evictable),
        "evictable_bytes": evictable_bytes,
        "local_only_count": len(local_only),
        "local_only_bytes": sum(bundle.size_bytes for bundle in local_only),
        "local_only_stable_ids": sorted(local_only_ids),
        "upload_queue_count": len(load_upload_queue(data_dir)),
        "protected_count": sum(1 for bundle in bundles if bundle.stable_id in protected),
        "can_rehydrate": can_rehydrate,
        "blocked_reason": blocked,
        "settings": asdict(resolved),
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
