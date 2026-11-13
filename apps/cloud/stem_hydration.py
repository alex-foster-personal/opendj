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

* [if] AutoPlay names a next-up head whose stem-set is only in R2 [then]
  :class:`NextUpStemPrefetcher` hydrates it once, one prefetch in flight
  (STEM-11747); [if] the cache is under the STEM-39 floor [then] it is
  skipped with ``below_cache_floor``; [if] the hub refuses [then] one
  WARNING, and the next head is still tried.
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
* [if] a downloaded stem-set breaks the STEM-8231 size rule [then] it is
  re-encoded to the policy MP3 before it enters the store, or refused with a
  named reason and nothing left in the store (#9665).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path

from apps.cloud import asset_store, stem_cache_budget, stem_index
from apps.cloud.eviction import HydrationError
from apps.cloud.stem_bundles import pin_for_hydrate, unpin_hydrate
from apps.cloud.stem_hydrate_policy import StemPolicyRefusedError, conform_fetched_bundle

# Moved to sibling modules (issue #9726) and re-exported here, so every
# ``from apps.cloud.stem_hydration import ...`` keeps working unchanged.
from apps.cloud.stem_hydration_decks import (
    OPEN_DECK_SERVED_TTL_S,
    OPEN_DECKS,
    RESERVATION_FILENAME,
    OpenDeckRegistry,
    load_reserved_ids,
    reservation_file_path,
)
from apps.cloud.stem_hydration_locks import (  # noqa: F401
    _ABANDONED_MARKERS,
    HYDRATE_LOCK_STRIPES,
    HYDRATE_OWNER_TTL_S,
    IN_FLIGHT_MARKER,
    _hydrate_locks_held,
    _hydrate_owner_marked,
    bundle_publish_window,
    hydrate_generation_path,
    hydrate_lock_path,
    hydrate_locks_within,
    hydrate_owner_alive,
    hydrate_owner_attempt_suffixes,
    hydrate_owner_snapshot,
    hydrate_owners_dir,
    read_hydrate_generation,
)
from apps.cloud.stem_hydration_outcomes import (
    _MONTHLY_CAP_CODES,
    BulkHydrateReport,
    HydrationOutcome,
    HydrationStatus,
    _stem_source_error_from_hub_outcome,
)
from apps.cloud.stem_hydration_prefetch import (  # noqa: F401
    PREFETCH_FAILED_CODE,
    PREFETCH_SKIP_BELOW_CACHE_FLOOR,
    PREFETCH_SKIP_NOT_CLOUD,
    NextUpStemPrefetcher,
    PrefetchDeps,
    PrefetchOutcome,
    PrefetchStatus,
    prefetch_one,
)
from apps.cloud.stem_source import (
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
    validate_stable_id,
)
from apps.stems.bundle_lock import (
    StagingCleanupError,
    bundle_lock_path,
    readers_held_off,
    remove_staging,
    replace_bundle_dir,
    restore_orphaned_swap,
)
from apps.stems.recompress import restore_orphaned_recompress_swap

log = logging.getLogger(__name__)

STEM_ASSET_KIND: str = "stem_bundle"


# --- single-bundle hydration ----------------------------------------------------


def _is_local(stable_id: str, stems_dir: Path) -> bool:
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir)
    except (StemBundleNotFoundError, StemArtifactError):
        return False
    return True


def _publish_fetched(stable_id: str, *, data_dir: Path, root: Path, tmp_dir: Path) -> tuple[bool, str | None]:
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

    The size-rule check (#9665) runs on the private verify copy, after the
    strict verify and before the swap, under the hydrate lock only: a set
    that breaks the rule is re-encoded there, and a refusal raises before
    anything enters the store. Returns (published, policy note).
    """
    bundle_dir = root / stable_id
    container = tmp_dir.with_name(f"{tmp_dir.name}.verify")
    container.mkdir()
    staged = container / stable_id
    tmp_dir.rename(staged)
    try:
        load_stem_bundle(stable_id, roots=[container])  # strict verify, never mocked
        policy_note = conform_fetched_bundle(container, stable_id, data_dir=data_dir)
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
                return False, None
            bundle_dir.parent.mkdir(parents=True, exist_ok=True)
            replace_bundle_dir(staged, bundle_dir)
        return True, policy_note
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


def hydrate_one(
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

    An unsafe ``stable_id`` (a separator, ``..``, an absolute path) is an
    ``error`` outcome before any path is built from it. Everything else is
    :func:`_hydrate_valid_one`.
    """
    try:
        validate_stable_id(stable_id)
    except StemArtifactError as exc:
        return HydrationOutcome(stable_id, "error", reason=str(exc))
    # Owned from before the first look at the local bundle, so a state read
    # during that inspection already sees this track as being fetched.
    with contextlib.ExitStack() as stack:
        try:
            attempt_suffix = stack.enter_context(_hydrate_owner_marked(data_dir, stable_id))
        except (HydrationError, OSError) as exc:
            # Nothing was fetched and the bundle was not touched: the named
            # error outcome is the contract, not an exception from here.
            return HydrationOutcome(stable_id, "error", reason=str(exc))
        return _hydrate_valid_one(
            stable_id,
            data_dir=data_dir,
            source=source,
            index=index,
            stems_dir=stems_dir,
            skip_reserved=skip_reserved,
            reserved_ids=reserved_ids,
            hand_off_to_deck=hand_off_to_deck,
            attempt_suffix=attempt_suffix,
        )


def _hydrate_valid_one(  # noqa: PLR0911, PLR0913, C901 - one outcome per branch; extra attempt_suffix param
    stable_id: str,
    *,
    data_dir: Path,
    source: StemHydrationSource,
    index: stem_index.StemAssetIndex,
    stems_dir: Path | None = None,
    skip_reserved: bool = False,
    reserved_ids: frozenset[str] | None = None,
    hand_off_to_deck: bool = False,
    attempt_suffix: str,
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
            tempfile.mkdtemp(
                dir=root,
                prefix=f"{stable_id}{stem_cache_budget.IN_FLIGHT_MARKER}{attempt_suffix}-",
            )
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
            with bundle_publish_window(data_dir, stable_id):
                published, policy_note = _publish_fetched(
                    stable_id, data_dir=data_dir, root=root, tmp_dir=tmp_dir
                )
                if not published:
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
        except StemPolicyRefusedError as exc:
            reason = _discard_failed_copy(tmp_dir, str(exc))
            unpin_hydrate(pin)
            return HydrationOutcome(stable_id, "error", reason=reason, code=exc.code)
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
            index=stem_index.load_remote_confirmed_index(data_dir),
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
    return HydrationOutcome(stable_id, "hydrated", bytes_fetched=total, reason=policy_note)


# --- bulk hydration --------------------------------------------------------------


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
    "PREFETCH_SKIP_BELOW_CACHE_FLOOR",
    "PREFETCH_SKIP_NOT_CLOUD",
    "RESERVATION_FILENAME",
    "STEM_ASSET_KIND",
    "BulkHydrateReport",
    "HydrationOutcome",
    "HydrationStatus",
    "NextUpStemPrefetcher",
    "OpenDeckRegistry",
    "PrefetchDeps",
    "PrefetchOutcome",
    "bulk_hydrate",
    "hydrate_one",
    "load_reserved_ids",
    "prefetch_one",
    "reservation_file_path",
]
