"""Stem bundle path, barrier, and load helpers for stems routes.

Split from ``stems.py`` for issue #9676.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from fastapi import HTTPException, Request

from apps.cloud import stem_cache_budget, stem_hydration
from apps.stems.artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundle,
    StemBundleNotFoundError,
    load_stem_bundle,
    stem_roots,
)
from apps.stems.bundle_lock import (
    held_for_read,
    is_shared_store_bundle,
    readers_held_off,
    restore_orphaned_swap,
    swap_backup_path,
)
from apps.stems.recompress import restore_orphaned_recompress_swap, swap_journal_path

from .stems_models import StemUnavailableOut

#: A path that is gone, replaced by a file, or not ours to read right now:
#: during a publish or a fetch's cleanup all three are transient states of the
#: same directory (Windows reports a directory mid-rename as PermissionError).
_PATH_UNREADABLE = (FileNotFoundError, NotADirectoryError, PermissionError)
def _stems_dir(request: Request) -> Path:
    """Use an injected directory in isolated apps, else the canonical state dir."""
    configured = getattr(request.app.state, "stems_dir", DEFAULT_STEMS_DIR)
    return Path(configured)


def _stem_roots(request: Request) -> tuple[Path, ...] | None:
    """Return an explicit mode boundary when the production app configured one."""

    configured = getattr(request.app.state, "stem_roots", None)
    if configured is None:
        return None
    return tuple(Path(root) for root in configured)


def _hold_for_deck(stable_id: str, request: Request, *, deck_open: bool = False) -> Callable[[], None]:
    """Protect ``stable_id`` from eviction BEFORE this route reads its bundle.

    Marks the deck registry, then waits out any eviction claim already
    under way (:func:`stem_cache_budget.hold_against_eviction`; never a
    hydration's R2 fetch),
    so a claim can never remove a bundle this request goes on to read
    (Codex P1 on #6820). Only an app built on a data dir runs eviction
    (``stem_cache_data_dir``); a bare router app has none to wait for.

    Returns the undo for a read that then fails: a request that served no
    bytes must not keep a corrupt bundle from eviction for the served TTL
    (Codex on #6820). A deck-open is the caller's own explicit hold and has
    nothing to undo.
    """
    registry = stem_hydration.OPEN_DECKS
    withdraw: list[Callable[[], None]] = []

    def mark() -> None:
        if deck_open:
            registry.mark_open(stable_id)
        else:
            withdraw.append(registry.mark_served_for_read(stable_id))

    data_dir = getattr(request.app.state, "stem_cache_data_dir", None)
    if data_dir is None:
        mark()
    else:
        stem_cache_budget.hold_against_eviction(mark, Path(data_dir), stable_id)
    return withdraw[0] if withdraw else (lambda: None)


def _read_barrier(stable_id: str, request: Request) -> AbstractContextManager[None]:
    """Hold off swaps and eviction claims while this request resolves and opens its bundle.

    ``_hold_for_deck`` only waits out a claim already under way, then lets
    go; a swap that starts after it and before the route opens its files
    would empty the bundle path under the read (Codex on #6820). See
    :func:`stem_cache_budget.read_barrier`. Never held across an R2 wait.
    """
    data_dir = getattr(request.app.state, "stem_cache_data_dir", None)
    if data_dir is None:
        return stem_cache_budget.read_barrier(None, stable_id)
    return _restoring_read_barrier(Path(data_dir), stable_id, request)


@contextmanager
def _restoring_read_barrier(data_dir: Path, stable_id: str, request: Request) -> Iterator[None]:
    """The read barrier, after putting back a bundle an interrupted swap left aside.

    A publisher killed between a swap's two renames leaves the only good
    copy at its swap backup, and the track read as missing until another
    publish (Codex on #6820). The root stays lexical: a stem root symlinked
    to another volume resolves outside ``<data>/state``, where the swap's
    backup is not looked for (Codex on #6820). The backup helper resolves
    only where it must.
    """
    roots = _stem_roots(request)
    for root in roots if roots is not None else stem_roots(_stems_dir(request)):
        _restore_interrupted_swap(Path(root) / stable_id, data_dir)
    with stem_cache_budget.read_barrier(data_dir, stable_id):
        yield


def _restore_interrupted_swap(dest: Path, data_dir: Path) -> None:
    """Put back ``dest`` from a dead swap's backup, as a writer: bundle lock, then the barrier.

    The restore is a rename into the live path, so it takes the bundle lock
    every writer holds, then the claim lock, in the order a swap takes
    them. Under the barrier alone it could land while a publisher holding
    the bundle lock had already seen no bundle there, and that publish then
    replaced what was restored without carrying its files over (Sol on
    #6820). With both held no live swap is mid-rename, so a backup with no
    bundle in place is a dead writer's.
    """
    if dest.exists():
        return  # the common case: one stat, no lock
    if not is_shared_store_bundle(dest):
        return  # outside <data>/state no swap holds the store's locks, and none leaves a backup to restore
    if not swap_backup_path(dest).is_dir() and not swap_journal_path(dest).is_file():
        # Nothing a dead swap left. A missing bundle is usually one a
        # hydration is fetching under the bundle lock, and the manifest
        # route must answer that poll at once (Codex P1 on #6821).
        return
    with held_for_read(data_dir, dest.name), readers_held_off(dest):
        restore_orphaned_swap(dest, data_dir)
        # A recompression keeps its backup under its own journal, not the
        # swap backup above (Codex on #6820).
        restore_orphaned_recompress_swap(dest, data_dir)


def _load_or_http_error(stable_id: str, request: Request) -> StemBundle:
    try:
        return load_stem_bundle(
            stable_id,
            stems_dir=_stems_dir(request),
            roots=_stem_roots(request),
        )
    except StemBundleNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_BUNDLE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "STEM_ARTIFACT_INVALID", "message": str(exc)},
        ) from exc


def _unavailable_out(stable_id: str, exc: StemBundleNotFoundError) -> StemUnavailableOut:
    return StemUnavailableOut(stable_id=stable_id, message=str(exc))
