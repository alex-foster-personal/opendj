"""Hydrate R2-indexed stem bundles onto local disk (ADR-0024).

Composes existing primitives rather than duplicating them: content-address
fetch is :func:`apps.cloud.asset_store.fetch_asset`, the mapping is
:mod:`apps.cloud.stem_index`, the budget is derived from free disk by
:mod:`apps.cloud.stem_cache_budget`, and the strict re-verify after writing is
:func:`apps.stems.artifacts.load_stem_bundle` -- ``_load_v1_bundle``
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
* [if] a hydrate is about to publish a bundle [then] it pins it first, so no
  eviction pass in any process removes it between the publishing rename, the
  verify and the post-hydrate pass; a deck-load hydrate marks it served before
  the pin is dropped (STEM-42).
* [if] a hydrate lands [then] the disk-aware budget is enforced at once, and
  only bundles the R2 index holds byte for byte are ever removed (STEM-39,
  STEM-40). Supersedes: the fixed-budget ``enforce_budget`` this module used
  to carry, which read 102400 MB from the policy and evicted any directory
  under the stems root, local-only renders included.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.cloud import asset_store, stem_cache_budget, stem_index
from apps.cloud.eviction import HydrationError
from apps.cloud.stem_bundles import pin_for_hydrate, unpin_hydrate
from apps.cloud.stem_source import (
    STEM_HUB_INDEX_FAILED,
    STEM_HUB_UNREACHABLE,
    STEM_MONTHLY_CAP_REACHED,
    STEM_MONTHLY_CAP_UNSET,
    StemHydrationSource,
    StemSourceError,
    hub_transport_failure_kind,
)
from apps.cloud.stem_upload_queue import exclusive_file_lock
from apps.stems.artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundleNotFoundError,
    load_stem_bundle,
)
from apps.stems.bundle_lock import (
    BUNDLE_LOCK_STRIPES,
    StagingCleanupError,
    bundle_lock_path,
    readers_held_off,
    remove_staging,
    replace_bundle_dir,
    restore_orphaned_swap,
)
from apps.stems.recompress import restore_orphaned_recompress_swap

log = logging.getLogger(__name__)

RESERVATION_FILENAME: str = "stem-order-reserved-100.json"
STEM_ASSET_KIND: str = "stem_bundle"

HydrationStatus = Literal[
    "already_local", "hydrated", "unavailable", "reserved_skip", "error", "hub_error"
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


#: How long a bundle stays eviction-protected after the manifest or part
#: route last served it, with no explicit deck-open call required. Chosen
#: as a generous multiple of a normal DJ set length so a long-running
#: session's decks are never evicted between two real loads of the same
#: track; see (private path)
#: Addendum 3 for the full rationale.
OPEN_DECK_SERVED_TTL_S: float = 6 * 3600.0  # 6 hours


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
        self._served_at: dict[str, float] = {}
        #: Each read's own hold, by token, so overlapping reads of one
        #: bundle never undo each other (Codex on #6821). A read that
        #: succeeds keeps its hold until the TTL; one that fails drops it.
        self._read_holds: dict[str, dict[int, float]] = {}
        self._next_read_token: int = 0
        #: True only in the process that serves decks (the engine, set when it
        #: arms hydration). Any other process (a hydrate worker, a CLI) sees an
        #: empty registry, so it must never evict: its post-hydrate pass only
        #: refreshes the upload queue and leaves eviction to the engine.
        self.holds_decks: bool = False

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

    def mark_served(self, stable_id: str, *, now: float | None = None) -> None:
        """Record that a manifest/part route just served this bundle's
        bytes, protecting it from eviction for OPEN_DECK_SERVED_TTL_S even
        with no explicit deck-open/close call -- there is currently no
        production caller of those endpoints (see ADR-0024 Addendum 3).
        ``now`` is injectable for tests; production omits it."""
        with self._lock:
            self._served_at[stable_id] = now if now is not None else time.monotonic()

    def mark_served_for_read(self, stable_id: str, *, now: float | None = None) -> Callable[[], None]:
        """Hold ``stable_id`` against eviction for a read that may fail; returns its undo.

        Each read gets its own hold, kept beside :meth:`mark_served`'s stamp
        rather than in it, so the undo removes exactly this read's hold and
        nothing else. A read that serves nothing leaves no protection behind,
        however its undo interleaves with other reads of the same bundle,
        while every read that did serve keeps its hold for the TTL (Codex on
        #6820 and #6821: a single shared stamp restored by a failed read could
        resurrect another failed read's mark). ``now`` is injectable for
        tests; production omits it.
        """
        moment = now if now is not None else time.monotonic()
        with self._lock:
            holds = self._read_holds.setdefault(stable_id, {})
            for token in [t for t, at in holds.items() if moment - at >= OPEN_DECK_SERVED_TTL_S]:
                del holds[token]
            token = self._next_read_token
            self._next_read_token += 1
            holds[token] = moment

        def withdraw() -> None:
            with self._lock:
                mine = self._read_holds.get(stable_id)
                if mine is None:
                    return
                mine.pop(token, None)
                if not mine:
                    del self._read_holds[stable_id]

        return withdraw

    def open_ids(self, *, now: float | None = None) -> frozenset[str]:
        """Explicitly-open ids, UNION any id served within the TTL. This is
        the single set every eviction caller already protects via
        ``protected=OPEN_DECKS.open_ids()`` -- widening its meaning here
        needs no changes at any of those call sites."""
        moment = now if now is not None else time.monotonic()
        with self._lock:
            recently_served = {
                sid for sid, at in self._served_at.items()
                if moment - at < OPEN_DECK_SERVED_TTL_S
            }
            # Expired holds go here too, not only on the next read of the
            # same id, so a long-lived engine does not keep one token per
            # read it ever served (Codex on #6820 and #6821).
            for sid in list(self._read_holds):
                holds = self._read_holds[sid]
                for token in [t for t, at in holds.items() if moment - at >= OPEN_DECK_SERVED_TTL_S]:
                    del holds[token]
                if holds:
                    recently_served.add(sid)
                else:
                    del self._read_holds[sid]
            return frozenset(self._open) | recently_served


#: Process-wide registry shared by the deck-load route and eviction.
OPEN_DECKS = OpenDeckRegistry()

#: Infix of a fetch's temp directory (``<stable_id>.tmp-hydrate-*``) while it
#: is in flight; a progress read lists that directory by this name.
IN_FLIGHT_MARKER: str = ".tmp-hydrate-"

_hydrate_locks_guard = threading.Lock()
_hydrate_locks: dict[str, threading.Lock] = {}


def _hydrate_lock(stable_id: str) -> threading.Lock:
    """One lock per stable_id, created lazily, shared process-wide, so
    concurrent in-process hydration calls for the SAME bundle serialize
    instead of racing on the same destination directory."""
    with _hydrate_locks_guard:
        return _hydrate_locks.setdefault(stable_id, threading.Lock())


#: Stripes of the cross-process hydrate lock, the bundle lock's own stripes.
#: Bounded so the lock files never grow with the library; two ids sharing a
#: stripe only wait on each other.
HYDRATE_LOCK_STRIPES: int = BUNDLE_LOCK_STRIPES


def hydrate_lock_path(data_dir: Path, stable_id: str) -> Path:
    """The OS lock file a hydrate of ``stable_id`` holds across its whole fetch, in every process.

    Only hydrations take it, so two hydrations of one id never fetch at once.
    It is NOT the bundle writer lock (:func:`apps.stems.bundle_lock.bundle_lock_path`):
    a hydrate takes that one only around its publish, because a stems job's
    publish or a recompress swap of an unrelated id on the same stripe would
    otherwise wait out a whole R2 transfer (Codex on #6821).
    """
    writer_lock = bundle_lock_path(data_dir, stable_id)
    return writer_lock.with_name(f"fetch-{writer_lock.name}")


@contextlib.contextmanager
def _hydrate_locks_held(data_dir: Path, stable_id: str) -> Iterator[None]:
    """The in-process lock, then the cross-process one. The engine and a
    ``hydrate_runner`` job can hydrate the same id at once; with only the
    in-process lock both could judge an old bundle invalid and each remove
    the other's freshly published copy (Codex P1 on #4974)."""
    with _hydrate_lock(stable_id), exclusive_file_lock(hydrate_lock_path(data_dir, stable_id)):
        yield


# --- outcomes -------------------------------------------------------------------


@dataclass(frozen=True)
class HydrationOutcome:
    stable_id: str
    status: HydrationStatus
    bytes_fetched: int = 0
    reason: str | None = None
    #: The ``StemSourceError.code`` that produced this outcome, when there was
    #: one. Without it a monthly-cap refusal became a code-less "error" and
    #: every caller re-labelled it STEM_BUNDLE_HYDRATION_FAILED, so the stems
    #: route answered 502 and bulk hydration treated a spent month as an
    #: ordinary skip and kept asking for more tracks (Codex round 5).
    code: str | None = None


#: Source codes that mean "no more bundles this month", not "this bundle
#: failed": hydration outcomes carrying one stop a bulk run.
_MONTHLY_CAP_CODES = frozenset({STEM_MONTHLY_CAP_REACHED, STEM_MONTHLY_CAP_UNSET})


@dataclass(frozen=True)
class BulkHydrateReport:
    fetched: tuple[HydrationOutcome, ...]
    skipped: tuple[HydrationOutcome, ...]
    bytes_fetched: int


# --- single-bundle hydration ----------------------------------------------------


def _is_local(stable_id: str, stems_dir: Path) -> bool:
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir)
    except (StemBundleNotFoundError, StemArtifactError):
        return False
    return True


def _publish_fetched(stable_id: str, *, data_dir: Path, root: Path, tmp_dir: Path) -> bool:
    """Verify a fetched bundle, then swap it into place under the bundle writer lock.

    The strict reader runs on the fetched copy before anything can read it:
    the copy is moved, under its own name, into a ``.verify`` container
    beside it that every scan skips, read there, and only a bundle that
    passes is swapped in. Verified after the swap instead, a deck read in
    between served a bundle the verify then took away (Sol on #6820). Only
    the swap holds the lock every writer of this bundle directory shares, so
    it never interleaves with a stems job's publish or a recompress swap,
    while the fetch and the verify hold only the hydrate lock (Codex on
    #6821); an invalid bundle in place is replaced through the barrier-held
    swap, never removed then renamed in the open (Codex on #6821). Returns
    False when another writer already published a valid bundle. A copy that
    is not published is moved back to ``tmp_dir`` for the caller to discard.
    """
    bundle_dir = root / stable_id
    container = tmp_dir.with_name(f"{tmp_dir.name}.verify")
    container.mkdir()
    staged = container / stable_id
    tmp_dir.rename(staged)
    try:
        load_stem_bundle(stable_id, roots=[container])  # strict verify, never mocked
        with exclusive_file_lock(bundle_lock_path(data_dir, stable_id)):
            # A writer killed between a swap's two renames left the only good
            # copy aside, possibly a render never uploaded. Put it back before
            # the keep check, or the swap below restores it and then deletes
            # it as the old bundle (Codex P1 on #6821).
            with readers_held_off(bundle_dir):
                restore_orphaned_swap(bundle_dir, data_dir)
                restore_orphaned_recompress_swap(bundle_dir, data_dir)
            # Cross-process race: something else already published a valid
            # bundle while we were fetching. Keep the winner, discard our own
            # copy rather than clobbering it.
            if bundle_dir.exists() and _is_local(stable_id, root):
                return False
            bundle_dir.parent.mkdir(parents=True, exist_ok=True)
            replace_bundle_dir(staged, bundle_dir)
        return True
    finally:
        try:
            if staged.exists():
                staged.rename(tmp_dir)
            container.rmdir()
        except OSError:
            # The copy cannot be handed back: removed here with its
            # container, or named by StagingCleanupError, never left where
            # no scan and no caller looks (Codex on #6821).
            remove_staging(container, "hydration verify container")


def _discard_losing_copy(tmp_dir: Path, outcome: HydrationOutcome) -> HydrationOutcome:
    """Remove the copy a hydrate fetched after another publisher won; report it if it stays.

    The winner's bundle is local and the deck can play it, so the outcome
    stays ``already_local``. A copy that cannot be removed is logged and
    named in the outcome's reason, never left on disk unseen (Sol on #6820).
    """
    try:
        remove_staging(tmp_dir, "losing hydration copy")
    except StagingCleanupError as exc:
        log.error("%s", exc)
        return dataclasses.replace(outcome, reason=str(exc))
    return outcome


def _discard_failed_copy(path: Path, reason: str) -> str:
    """Remove what a failed hydrate wrote; name it in ``reason`` if it stays.

    A ``.tmp-hydrate-*`` directory is outside every scan, so one left behind
    would never be counted or evicted, and retries could fill the volume
    unseen. A removal that fails is logged and reported with the hydrate's
    own error (Codex on #6821).
    """
    try:
        remove_staging(path, "failed hydration copy")
    except StagingCleanupError as exc:
        log.error("%s", exc)
        return f"{reason}; {exc}"
    return reason


def hydrate_one(  # noqa: PLR0911 - one outcome per named HydrationStatus branch, all real states
    stable_id: str,
    *,
    data_dir: Path,
    source: StemHydrationSource,
    index: stem_index.StemAssetIndex,
    stems_dir: Path | None = None,
    skip_reserved: bool = False,
    reserved_ids: frozenset[str] | None = None,
    hand_off_to_deck: bool = False,
) -> HydrationOutcome:
    """Hydrate one bundle from R2, or explain precisely why it did not.

    ``hand_off_to_deck`` is the deck-load path's flag: the bundle is marked
    served (``OPEN_DECKS``) before its hydrate pin is dropped, so the deck's
    next read finds it. Bulk and drain callers leave it off, so the bundle is
    evictable as soon as this call returns.

    ``skip_reserved`` is the BULK-path-only guard: the on-demand deck-load
    caller must always pass ``False`` (its default) because opening a deck
    on a reserved track is exactly the in-app ordering test the reservation
    protects, not something to skip.
    """
    root = stems_dir or DEFAULT_STEMS_DIR

    def already_local() -> HydrationOutcome:
        # Another hydrator (maybe another process) published it: the deck that
        # asked still needs the served lease before that hydrator's pin drops.
        if hand_off_to_deck:
            OPEN_DECKS.mark_served(stable_id)
        return HydrationOutcome(stable_id, "already_local")

    if _is_local(stable_id, root):
        return already_local()

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
        # Indexed but unproducible: an error, never "unavailable", so callers
        # keep the loud 502 surface instead of reading it as "no bundle".
        return HydrationOutcome(
            stable_id,
            "error",
            reason="index has no manifest.json hash for this bundle",
        )
    bad_filenames = sorted(
        filename
        for filename in file_hashes
        if not stem_index.is_allowed_stem_filename(filename)
    )
    if bad_filenames:
        return HydrationOutcome(
            stable_id,
            "error",
            reason=(
                "index has disallowed filename(s): "
                + ", ".join(bad_filenames)
            ),
        )

    bundle_dir = root / stable_id
    with _hydrate_locks_held(data_dir, stable_id):
        # Double-checked: another call, in this process or another, may have
        # just published this bundle while we were waiting for the lock.
        if _is_local(stable_id, root):
            return already_local()
        root.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(
            tempfile.mkdtemp(dir=root, prefix=f"{stable_id}{stem_cache_budget.IN_FLIGHT_MARKER}")
        )
        pin: Path | None = None
        try:
            total = source.fetch_bundle_files(
                stable_id=stable_id,
                file_hashes=file_hashes,
                tmp_dir=tmp_dir,
            )
            # Pin BEFORE publishing: an eviction pass in any process may scan
            # the bundle the moment the rename lands, before the verify and
            # before any deck registry names it. The pin is this call's own
            # file, so another process's hydrate of this id cannot drop it.
            pin = pin_for_hydrate(bundle_dir)
            if not _publish_fetched(stable_id, data_dir=data_dir, root=root, tmp_dir=tmp_dir):
                outcome = already_local()
                unpin_hydrate(pin)
                return _discard_losing_copy(tmp_dir, outcome)
        except StemSourceError as exc:
            # A failed publish already removed what it renamed into place,
            # under the writer lock; only OUR OWN temp dir can be left.
            reason = _discard_failed_copy(tmp_dir, exc.message)
            unpin_hydrate(pin)
            status = (
                "hub_error"
                if hub_transport_failure_kind(exc) is not None
                else "error"
            )
            return HydrationOutcome(
                stable_id, status, reason=reason, code=exc.code
            )
        except (
            HydrationError,
            asset_store.AssetStoreError,
            StemArtifactError,
            StemBundleNotFoundError,
            OSError,
        ) as exc:
            # Remove only OUR OWN temp dir, never bundle_dir: a publish whose
            # verify failed removed its own bundle under the writer lock.
            reason = _discard_failed_copy(tmp_dir, str(exc))
            unpin_hydrate(pin)
            return HydrationOutcome(stable_id, "error", reason=reason)

    # A hydrate just succeeded, so this machine can demonstrably get an
    # evicted bundle back: that is what ``can_rehydrate`` asserts.
    try:
        stem_cache_budget.enforce(
            root,
            data_dir=data_dir,
            index=index,
            protected=OPEN_DECKS.open_ids() | {stable_id},
            can_rehydrate=True,
            # Give back only what this hydrate took. A deck is waiting on this
            # call, and confirming a bundle against R2 means hashing it; the
            # engine timer clears any larger backlog off the request path.
            # Outside the engine nothing is evicted: this process cannot see
            # which bundles the engine's decks hold.
            max_evict_bytes=total if OPEN_DECKS.holds_decks else 0,
            live_protected=lambda: OPEN_DECKS.open_ids() | {stable_id},
        )
        if hand_off_to_deck:
            OPEN_DECKS.mark_served(stable_id)
    finally:
        unpin_hydrate(pin)
    return HydrationOutcome(stable_id, "hydrated", bytes_fetched=total)


# --- bulk hydration --------------------------------------------------------------


def _stem_source_error_from_hub_outcome(outcome: HydrationOutcome) -> StemSourceError:
    reason = outcome.reason or "hub transport failure"
    code = (
        STEM_HUB_INDEX_FAILED
        if "HTTP 5" in reason
        else STEM_HUB_UNREACHABLE
    )
    return StemSourceError(code, reason)


def _append_hub_error_remainder(
    stable_id: str,
    remaining: Sequence[str],
    reason: str,
    *,
    skipped: list[HydrationOutcome],
) -> None:
    skipped.append(HydrationOutcome(stable_id, "hub_error", reason=reason))
    for sid in remaining:
        skipped.append(HydrationOutcome(sid, "hub_error", reason=reason))


def bulk_hydrate(
    stable_ids: Sequence[str],
    *,
    data_dir: Path,
    source: StemHydrationSource,
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
    root = stems_dir or DEFAULT_STEMS_DIR
    for idx, stable_id in enumerate(stable_ids):
        if bytes_used >= byte_budget:
            skipped.append(
                HydrationOutcome(stable_id, "unavailable", reason="byte_budget exhausted")
            )
            continue
        if not _is_local(stable_id, root):
            file_hashes = index.get(stable_id)
            if file_hashes and stem_index.MANIFEST_FILENAME in file_hashes:
                try:
                    bundle_size = source.bundle_remote_size(
                        file_hashes, stable_id=stable_id
                    )
                except StemSourceError as exc:
                    if hub_transport_failure_kind(exc) is None:
                        raise
                    if bytes_used == 0 and not fetched:
                        raise
                    _append_hub_error_remainder(
                        stable_id,
                        stable_ids[idx + 1 :],
                        exc.message,
                        skipped=skipped,
                    )
                    break
                if bundle_size is not None and bytes_used + bundle_size > byte_budget:
                    remaining = byte_budget - bytes_used
                    skipped.append(
                        HydrationOutcome(
                            stable_id,
                            "unavailable",
                            reason=(
                                f"byte_budget exhausted (bundle needs {bundle_size} bytes, "
                                f"{remaining} remaining)"
                            ),
                        )
                    )
                    continue
        outcome = hydrate_one(
            stable_id,
            data_dir=data_dir,
            source=source,
            index=index,
            stems_dir=stems_dir,
            skip_reserved=not include_reserved,
            reserved_ids=reserved,
        )
        if outcome.status == "hub_error":
            if bytes_used == 0 and not fetched:
                raise _stem_source_error_from_hub_outcome(outcome)
            _append_hub_error_remainder(
                stable_id,
                stable_ids[idx + 1 :],
                outcome.reason or "hub transport failure",
                skipped=skipped,
            )
            break
        if outcome.code in _MONTHLY_CAP_CODES:
            # A spent (or unconfigured) month is not this track's problem: no
            # further bundle can be served, so stop asking the hub instead of
            # reporting a capped track as an ordinary skip (Codex round 5).
            raise StemSourceError(outcome.code, outcome.reason or outcome.code)
        if outcome.status in ("hydrated", "already_local"):
            bytes_used += outcome.bytes_fetched
            fetched.append(outcome)
        else:
            skipped.append(outcome)
    return BulkHydrateReport(
        fetched=tuple(fetched), skipped=tuple(skipped), bytes_fetched=bytes_used
    )


__all__ = [
    "IN_FLIGHT_MARKER",
    "OPEN_DECKS",
    "OPEN_DECK_SERVED_TTL_S",
    "RESERVATION_FILENAME",
    "STEM_ASSET_KIND",
    "BulkHydrateReport",
    "HydrationOutcome",
    "HydrationStatus",
    "OpenDeckRegistry",
    "bulk_hydrate",
    "hydrate_one",
    "load_reserved_ids",
    "reservation_file_path",
]
