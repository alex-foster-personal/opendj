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

import contextlib
import datetime as dt
import hashlib
import shutil
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from apps.cloud.stem_bundles import (
    IN_FLIGHT_MARKER,
    REASON_CONTENT_DIFFERS,
    REASON_FILES_DIFFER,
    REASON_GONE,
    REASON_NOT_IN_INDEX,
    LocalBundle,
    StemAssetIndex,
    claim_and_remove,
    current_fingerprint,
    file_marks,
    index_gap,
    is_hydrate_pinned,
    scan_bundles,
    sweep_abandoned_claims,
    unconfirmed_reason,
)
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
)
from apps.cloud.stem_upload_queue import (
    UPLOAD_QUEUE_FILENAME,
    exclusive_file_lock,
    load_upload_queue,
    load_verified_fingerprints,
    save_upload_queue,
    upload_queue_path,
)
from apps.stems.bundle_lock import BUNDLE_LOCK_STRIPES, bundle_lock_path, discard_landed_swap_backup

GIB: int = 1024**3
#: The short lock an eviction claim holds from its last registry ask through
#: the rename, and the only one a deck's read waits on. Separate from the
#: bundle lock, which a hydration holds for its whole R2 fetch, so a read
#: never waits on the network (Codex P1 on #6821).
EVICTION_CLAIM_LOCK_DIR_NAME: str = "stem-evict-claim-locks"
#: Bytes one timer pass may hash to find same-name re-renders, so a first pass
#: meeting every bundle unverified spreads that read over several passes.
REVERIFY_HASH_BUDGET_BYTES: int = 2 * GIB

BLOCKED_HYDRATION_NOT_ARMED: str = "hydration_not_armed"
BLOCKED_AUTO_EVICT_OFF: str = "auto_evict_off"
BLOCKED_NOTHING_EVICTABLE: str = "not_enough_evictable_bundles"

CacheState = Literal["healthy", "low_disk"]


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


# ----- upload queue --------------------------------------------------------------


def _queue_reason(
    bundle: LocalBundle,
    index: StemAssetIndex,
    *,
    known_differs: bool,
    was_differing: bool,
    verified_fingerprint: str | None,
    room_to_hash: bool,
) -> tuple[str | None, bool, bool]:
    """One bundle's upload-queue verdict: (reason or None, verified, hashed).

    A bundle queued for differing content is always re-hashed; any other only
    when its fingerprint moved since it last hashed equal, and only with room
    left in this pass's hash budget (else neither queued nor verified)."""
    gap = index_gap(bundle, index)
    if gap is not None:
        return gap, False, False
    if known_differs:
        return REASON_CONTENT_DIFFERS, False, False
    if not was_differing and verified_fingerprint == bundle.fingerprint:
        return None, True, False
    if not was_differing and not room_to_hash:
        return None, False, False
    reason = unconfirmed_reason(bundle, index)
    if reason == REASON_GONE:  # a concurrent pass evicted it: nothing to queue
        return None, False, True
    return reason, reason is None, True


def _refreshed_upload_queue(
    previous: dict[str, dict[str, object]],
    bundles: list[LocalBundle],
    index: StemAssetIndex,
    content_differs: set[str],
    now_iso: str,
    verified: dict[str, str] | None = None,
    hash_budget_bytes: int | None = None,
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """The queue as it should stand after this pass, and the verified map.

    A bundle is queued while it is on disk and the index does not cover it.
    One previously queued for differing content is re-hashed (a handful at
    most) so it leaves the queue as soon as a publish catches up. A same-name
    re-render is hashed within ``hash_budget_bytes`` (see ``_queue_reason``),
    so different bytes reach the queue on a healthy disk too.
    """
    queue: dict[str, dict[str, object]] = {}
    verified_now: dict[str, str] = {}
    known = verified or {}
    hashed_bytes = 0
    for bundle in bundles:
        room = hash_budget_bytes is None or hashed_bytes + bundle.size_bytes <= hash_budget_bytes
        reason, is_verified, hashed = _queue_reason(
            bundle,
            index,
            known_differs=bundle.stable_id in content_differs,
            was_differing=(
                previous.get(bundle.stable_id, {}).get("reason") == REASON_CONTENT_DIFFERS
            ),
            verified_fingerprint=known.get(bundle.stable_id),
            room_to_hash=room,
        )
        if hashed:
            hashed_bytes += bundle.size_bytes
        if is_verified:
            verified_now[bundle.stable_id] = bundle.fingerprint
        if reason is None:
            continue
        queued_at = previous.get(bundle.stable_id, {}).get("queued_at", now_iso)
        queue[bundle.stable_id] = {
            "reason": reason,
            "bytes": bundle.size_bytes,
            "queued_at": queued_at,
        }
    return queue, verified_now


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
    *,
    sweep: bool = False,
) -> _CachePosition:
    """``sweep`` (writing passes only) first finishes removing claims a failed
    ``rmtree`` left behind: scans skip them, so their bytes are otherwise lost."""
    if sweep:
        sweep_abandoned_claims(stems_dir)
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


def _remove_one(
    bundle: LocalBundle, *, dry_run: bool, remove: Callable[[LocalBundle], bool | None]
) -> tuple[int, bool]:
    """(bytes this bundle covers, whether this pass removed it).

    A lost claim still covers its bytes (another pass freed them); a bundle
    rewritten since the scan (``remove`` returns None) is kept and covers
    nothing, so the pass walks on to the next one.
    """
    removed = True if dry_run else remove(bundle)
    if removed is None:
        return 0, False
    return bundle.size_bytes, removed


def _held_now(bundle: LocalBundle, live_protected: Callable[[], frozenset[str]] | None) -> bool:
    """Whether a deck or a hydrate holds ``bundle`` at this moment, so it must stay."""
    # ``protected`` predates the scan and the hash (seconds on a big bundle),
    # so a deck may have opened this one since: ask the live registry again.
    if live_protected is not None and bundle.stable_id in live_protected():
        return True
    # A hydrate in any process pins a bundle before publishing it, so a
    # bundle no deck registry names yet is kept until it is handed over.
    return is_hydrate_pinned(bundle.path)


def _evict_lru(
    bundles: list[LocalBundle],
    *,
    need_bytes: int,
    index: StemAssetIndex,
    protected: frozenset[str],
    dry_run: bool,
    remove: Callable[[LocalBundle], bool | None],
    live_protected: Callable[[], frozenset[str]] | None = None,
) -> tuple[list[str], int, set[str], int]:
    """Remove LRU bundles until ``need_bytes`` is freed: (evicted ids, bytes
    freed, ids skipped for differing content, bytes covered). A bundle is
    removed only when it is not protected AND R2 holds it byte for byte.
    Covered adds the bundles a concurrent pass removed first: those bytes are
    free too, so this pass stops short instead of evicting the next one."""
    evicted: list[str] = []
    content_differs: set[str] = set()
    freed = covered = 0
    for bundle in bundles:
        if covered >= need_bytes:
            break
        if bundle.stable_id in protected:
            continue
        reason = unconfirmed_reason(bundle, index)  # REASON_GONE when raced away
        if reason == REASON_GONE and dry_run:  # removed under us: those bytes are free too
            covered += bundle.size_bytes
            continue
        if reason is not None and reason != REASON_GONE:
            if reason == REASON_CONTENT_DIFFERS:
                content_differs.add(bundle.stable_id)
            continue
        # A missing file may also mean a writer swapped in a bundle with other
        # file names (FLAC parts re-rendered as MP3), which frees nothing. The
        # claim below tells the two apart under the bundle lock and credits
        # only a bundle that is really gone (Codex on #6820).
        if _held_now(bundle, live_protected):
            continue
        credited, removed = _remove_one(bundle, dry_run=dry_run, remove=remove)
        covered += credited
        if not removed:
            continue  # claimed by another pass first, or rewritten since the scan
        evicted.append(bundle.stable_id)
        freed += bundle.size_bytes
    return evicted, freed, content_differs, covered


def claim_if_unchanged(
    bundle: LocalBundle,
    data_dir: Path,
    index: StemAssetIndex,
    *,
    live_protected: Callable[[], frozenset[str]] | None = None,
) -> bool | None:
    """Remove ``bundle`` under its bundle lock if it is still what this pass confirmed.

    Every writer of a bundle directory holds :mod:`apps.stems.bundle_lock`
    (the same striped files hydration holds) while it creates, replaces or
    moves it. Eviction confirmed this bundle against R2 without that lock,
    so a STEM-58 re-compress swap or a republish may have put a new,
    not-yet-uploaded bundle at this path since (Codex P1 on #6820). Inside
    the lock the fingerprint (every file's name, size and mtime) is taken
    again and compared with the scan's; then, OUTSIDE the lock, every
    file's content is hashed against ``index`` again: a coarse filesystem
    clock (1-2 s on HFS+ or FAT) can give a same-size rewrite the same
    mtime, so metadata alone cannot prove these are still the bytes R2
    holds (Codex P1 on #6821). Under the lock once more, every file's
    inode and ctime must be as they were before the hash
    (``file_marks``), so a writer that swapped or rewrote the bundle while
    it was hashed keeps it (Codex on #6821). That wait and hash can take
    seconds, so last of all, immediately before the claim, the live deck registry
    (``live_protected``) and the hydrate pins are asked again: a deck that
    opened this bundle meanwhile keeps it (STEM-42; Codex P1 on #6820 and
    #6821). That last ask and the claim run under the eviction-claim lock
    as well, the one a deck's read waits on. Only a bundle that passes all three is claimed. True: removed.
    False: already gone, another pass freed it. None: rewritten since the
    scan, or opened or pinned since, so it is kept and frees nothing.
    """
    lock_path = bundle_lock_path(data_dir, bundle.stable_id)
    with exclusive_file_lock(lock_path):
        now = current_fingerprint(bundle.path)
        if now is None:
            return False
        if now != bundle.fingerprint:
            return None
        marks = file_marks(bundle.path)
    # The hash reads every part, seconds on a big bundle or a slow disk, so
    # it runs outside the striped writer lock: a publish or hydration of any
    # id on this stripe would otherwise wait it out (Codex on #6821). What
    # was hashed is then proven still in place under the lock, by every
    # file's inode and ctime, which any write moves (file_marks).
    reason = unconfirmed_reason(bundle, index)
    if reason == REASON_GONE:
        # A file the hash missed may be a swap that put a bundle with other
        # file names here, not a pass that removed it: only a path still
        # absent under the lock is another pass's freed bytes (Codex on #6820).
        with exclusive_file_lock(lock_path):
            return False if current_fingerprint(bundle.path) is None else None
    if reason is not None:
        return None
    return _claim_if_still_marked(bundle, data_dir, lock_path, marks, live_protected)


def _claim_if_still_marked(
    bundle: LocalBundle,
    data_dir: Path,
    lock_path: Path,
    marks: tuple[tuple[str, int, int, int], ...] | None,
    live_protected: Callable[[], frozenset[str]] | None,
) -> bool | None:
    """The second half of :func:`claim_if_unchanged`, under the bundle lock again."""
    with exclusive_file_lock(lock_path):
        marks_now = file_marks(bundle.path)
        if marks_now is None:
            return False
        if marks_now != marks:
            return None
        with exclusive_file_lock(eviction_claim_lock_path(data_dir, bundle.stable_id)):
            if live_protected is not None and bundle.stable_id in live_protected():
                return None
            if is_hydrate_pinned(bundle.path):
                return None
            # A backup a killed swap left beside this live bundle is an older
            # render; it goes first, or the next read would restore it once
            # the bundle is gone (Codex on #6821). One that cannot be moved
            # aside keeps the bundle, which a later pass retries (Codex on
            # #6820).
            if not discard_landed_swap_backup(bundle.path):
                return None
            # Likewise a recompression's landed journal, or a read would put
            # its old backup back once the bundle is gone (Codex on #6820).
            # Imported here: recompress pulls in the pydantic bundle schema,
            # and the root conftest imports this module in environments
            # (the quality job's) that do not install it.
            from apps.stems.recompress import clear_landed_recompress_journal

            clear_landed_recompress_journal(bundle.path)
            return claim_and_remove(bundle.path)


def eviction_claim_lock_path(data_dir: Path, stable_id: str) -> Path:
    """The OS lock an eviction claim of ``stable_id`` holds, striped like the bundle lock."""
    stripe = int(hashlib.sha256(stable_id.encode("utf-8")).hexdigest(), 16) % BUNDLE_LOCK_STRIPES
    return Path(data_dir) / "state" / EVICTION_CLAIM_LOCK_DIR_NAME / f"{stripe:03d}.lock"


def hold_against_eviction(mark: Callable[[], None], data_dir: Path, stable_id: str) -> None:
    """Record a deck's hold on ``stable_id``, then wait out any claim already under way.

    :func:`claim_if_unchanged` asks the deck registry and claims the bundle
    inside one hold of the eviction-claim lock. A deck that only marked the
    registry could mark just after that ask and then read a bundle the claim
    is about to rename away (Codex P1 on #6820). So the deck marks FIRST,
    then takes and drops that lock before it reads: a claim that asked
    before the mark has finished by then (the read finds nothing and
    hydrates), and every claim that asks after it sees the mark and keeps
    the bundle. It waits on the claim lock, not the bundle lock, because a
    hydration holds the bundle lock for its whole R2 fetch and a manifest
    poll must answer at once (Codex P1 on #6821). The deck never holds the
    lock while it reads, so serving parts never blocks a writer.
    """
    mark()
    with exclusive_file_lock(eviction_claim_lock_path(data_dir, stable_id)):
        pass


@contextlib.contextmanager
def read_barrier(data_dir: Path | None, stable_id: str) -> Iterator[None]:
    """Hold the eviction-claim lock while a read resolves and opens a bundle.

    A swap (``apps.stems.bundle_lock.replace_bundle_dir``, the STEM-58
    ``_swap_in``) and an eviction claim hold this lock across the moves
    that empty the bundle path, so a stems route that finds its bundle and
    opens the file it serves inside this hold sees either the old bundle or
    the new one, never the gap between them and never a lower-precedence
    root (Codex on #6820). Only that resolve-and-open is held; streaming
    an opened file and decoding a waveform run outside it, so a writer
    waits milliseconds at most. ``data_dir`` is None for an app with no
    data dir, which runs no swap or eviction, and then nothing is held.
    """
    if data_dir is None:
        yield
        return
    with exclusive_file_lock(eviction_claim_lock_path(Path(data_dir), stable_id)):
        yield


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
    live_protected: Callable[[], frozenset[str]] | None = None,
) -> EnforceReport:
    """Measure the volume and evict just enough R2-confirmed bundles to bring
    free space back to the floor. Refreshes the upload queue on the way.

    ``protected`` is the loaded-or-playing set (``OPEN_DECKS.open_ids()``).
    ``can_rehydrate`` is whether this machine has a hydration source armed.
    ``disk`` and ``settings`` are injectable; production omits both.
    ``max_evict_bytes`` bounds one pass. The post-hydrate call passes the
    bytes it just fetched, so a deck load pays to hash about one bundle, not
    the whole backlog; the timer, which nobody is waiting on, passes nothing
    and catches up in full. Looking for same-name re-renders is the timer's
    job alone (``REVERIFY_HASH_BUDGET_BYTES`` a pass): the post-hydrate call
    hashes nothing for it, so a deck load pays no extra read.
    ``live_protected`` (``OPEN_DECKS.open_ids``) is asked again just before
    each removal, so a bundle a deck opened during the pass is kept.
    """
    position = _measure_cache_position(stems_dir, data_dir, settings, disk, sweep=not dry_run)
    bundles, usage = position.bundles, position.usage
    need = position.over_budget_bytes
    if max_evict_bytes is not None:
        need = min(need, max_evict_bytes)

    evicted: list[str] = []
    freed = 0
    content_differs: set[str] = set()
    blocked = _blocked_before_eviction(need, position.settings, can_rehydrate)
    if need > 0 and blocked is None:
        evicted, freed, content_differs, covered = _evict_lru(
            bundles,
            need_bytes=need,
            index=index,
            protected=protected,
            dry_run=dry_run,
            remove=lambda bundle: claim_if_unchanged(bundle, data_dir, index, live_protected=live_protected),
            live_protected=live_protected,
        )
        if covered < need:
            blocked = BLOCKED_NOTHING_EVICTABLE

    now_iso = _utc_now_iso()
    evicted_set = set(evicted)
    remaining = [bundle for bundle in bundles if bundle.stable_id not in evicted_set]
    previous = load_upload_queue(data_dir)
    previous_verified = load_verified_fingerprints(data_dir)
    queue, verified = _refreshed_upload_queue(
        previous,
        remaining,
        index,
        content_differs,
        now_iso,
        verified=previous_verified,
        hash_budget_bytes=0 if max_evict_bytes is not None else REVERIFY_HASH_BUDGET_BYTES,
    )
    if not dry_run and (queue != previous or verified != previous_verified):
        queue = save_upload_queue(
            data_dir, queue, verified, stems_dir=stems_dir,
            seen={bundle.stable_id: bundle.fingerprint for bundle in bundles},
        )

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

    queued = load_upload_queue(data_dir)
    # A bundle the last pass hashed different from R2 is local-only too, even
    # though its names alone match the index.
    local_only = [
        bundle
        for bundle in bundles
        if index_gap(bundle, index) is not None
        or queued.get(bundle.stable_id, {}).get("reason") == REASON_CONTENT_DIFFERS
    ]
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
        "upload_queue_count": len(queued),
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
    "EVICTION_CLAIM_LOCK_DIR_NAME",
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
    "claim_if_unchanged",
    "derived_budget_bytes",
    "enforce",
    "eviction_claim_lock_path",
    "floor_bytes",
    "hold_against_eviction",
    "index_gap",
    "load_settings",
    "load_upload_queue",
    "measure_disk",
    "merged_settings",
    "read_barrier",
    "save_settings",
    "scan_bundles",
    "settings_from_mapping",
    "settings_path",
    "status",
    "unconfirmed_reason",
    "upload_queue_path",
]
