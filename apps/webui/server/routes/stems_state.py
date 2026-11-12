"""Stem hydration progress and index-backed state helpers.

Split from ``stems.py`` for issue #9676.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from fastapi import Request

from apps.cloud import stem_hydration, stem_index
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.stems.artifacts import StemArtifactError, StemBundleNotFoundError, load_stem_bundle

from .stems_bundle import _PATH_UNREADABLE, _stem_roots
from .stems_hydration import _INFLIGHT, _INFLIGHT_FILE_TOTALS, _INFLIGHT_LOCK
from .stems_models import StemHydrationProgressOut, StemTrackState


def _tmp_dir_matches_hydrate_attempt(tmp_name: str, prefix: str, suffixes: frozenset[str]) -> bool:
    """Whether ``tmp_name`` is a temp dir for one of the listed owner suffixes."""
    return any(tmp_name.startswith(f"{prefix}{sfx}") for sfx in suffixes)


def _hydration_progress(
    stable_id: str,
    stems_dir: Path,
    *,
    data_dir: Path | None,
    attempt_suffixes: frozenset[str] | None = None,
) -> StemHydrationProgressOut:
    """Files and bytes the in-flight fetch has written so far.

    Read from the fetch's own temp directory (``<stable_id>.tmp-hydrate-*``),
    so it costs a directory listing and never touches the network or the
    fetch itself. A file still being written counts as done with the bytes it
    has, which is why this is progress and not a completion check.

    Directories that share the in-flight prefix but not a live attempt's
    suffix (leftover kills, offload restore staging) are ignored so
    ``files_done`` stays aligned with ``files_total``.
    """
    files_done = 0
    bytes_done = 0
    with _INFLIGHT_LOCK:
        route_future = _INFLIGHT.get(stable_id)
        # A total is this route's fetch's size only while that fetch runs; a
        # finished fetch's total would size a later fetch of a changed bundle.
        route_total = (
            _INFLIGHT_FILE_TOTALS.get(stable_id, 0) if route_future is not None and not route_future.done() else 0
        )
    if data_dir is None:
        return StemHydrationProgressOut(files_total=route_total, files_done=0, bytes_done=0)
    suffixes = attempt_suffixes
    if suffixes is None:
        suffixes = stem_hydration.hydrate_owner_attempt_suffixes(data_dir, stable_id)
    prefix = f"{stable_id}{stem_hydration.IN_FLIGHT_MARKER}"
    if stems_dir.is_dir():
        for tmp_dir in stems_dir.iterdir():
            if not tmp_dir.is_dir():
                continue
            if not _tmp_dir_matches_hydrate_attempt(tmp_dir.name, prefix, suffixes):
                continue
            dir_files, dir_bytes = _written_so_far(tmp_dir)
            files_done += dir_files
            bytes_done += dir_bytes
    return StemHydrationProgressOut(files_total=route_total, files_done=files_done, bytes_done=bytes_done)


def _state_progress(
    stable_id: str,
    stems_dir: Path,
    data_dir: Path | str | None,
    *,
    attempt_suffixes: frozenset[str] | None = None,
) -> StemHydrationProgressOut | None:
    """Progress for a ``fetching`` state, whichever process is fetching.

    This route records its own fetch's file total at enqueue. A fetch by bulk
    hydrate or a job runner has no entry there, so its total comes from the
    same cached index that fetch reads; with no readable total the progress is
    omitted rather than shown as ``n/0``.
    """
    shared = Path(data_dir) if data_dir is not None else None
    progress = _hydration_progress(
        stable_id,
        stems_dir,
        data_dir=shared,
        attempt_suffixes=attempt_suffixes,
    )
    if progress.files_total:
        return progress
    total = _indexed_file_total(stable_id, data_dir)
    if total is None:
        return None
    return StemHydrationProgressOut(files_total=total, files_done=progress.files_done, bytes_done=progress.bytes_done)


def _indexed_file_total(stable_id: str, data_dir: Path | str | None) -> int | None:
    if data_dir is None:
        return None
    entry = stem_index.load_cached_index(Path(data_dir)).get(stable_id)
    return len(entry) if entry else None


def _written_so_far(tmp_dir: Path) -> tuple[int, int]:
    """Regular files and bytes in one fetch's temp directory.

    The fetch publishes (renames) or removes the directory when it finishes,
    which can land between the caller's ``is_dir()`` and this listing, or
    between the listing and a file's ``stat()``. Either way the directory's
    files are no longer in flight: they count as nothing here, never as an
    error, so a progress read racing a finished fetch cannot fail the poll.
    """
    files_done = 0
    bytes_done = 0
    try:
        children = list(tmp_dir.iterdir())
    except _PATH_UNREADABLE:
        return 0, 0
    for child in children:
        try:
            child_stat = child.stat()
        except _PATH_UNREADABLE:
            continue
        if stat.S_ISREG(child_stat.st_mode):
            files_done += 1
            bytes_done += child_stat.st_size
    return files_done, bytes_done


def _hydration_in_flight(stable_id: str) -> bool:
    with _INFLIGHT_LOCK:
        future = _INFLIGHT.get(stable_id)
        return future is not None and not future.done()


def _hydration_arming(state: Any) -> tuple[str | None, Any, bool]:
    """Read, never change, whether cloud stem hydration is armed on ``app.state``.

    Returns the recorded reason it is not armed (or None), its data directory,
    and whether a fetch could run now: no unarmed reason, a source, and a data
    directory.
    """
    unarmed_reason = getattr(state, "stem_hydration_unarmed_reason", None)
    data_dir = getattr(state, "stem_hydration_data_dir", None)
    has_source = getattr(state, "stem_hydration_source", None) is not None or (
        getattr(state, "stem_hydration_cfg", None) is not None and getattr(state, "stem_hydration_s3", None) is not None
    )
    return unarmed_reason, data_dir, unarmed_reason is None and has_source and data_dir is not None


def _bundle_landed(stable_id: str, stems_dir: Path, request: Request) -> bool:
    """Whether a valid bundle for ``stable_id`` is on this machine now."""
    try:
        load_stem_bundle(stable_id, stems_dir=stems_dir, roots=_stem_roots(request))
    except (StemBundleNotFoundError, StemArtifactError):
        return False
    return True


def _index_proves_absent(stable_id: str, data_dir: Path | str | None) -> bool:
    """Whether a cached cloud index exists, reads cleanly and lacks ``stable_id``.

    A missing cache proves nothing (it was never fetched), and an unreadable
    one is not evidence either, so both answer ``False``.
    """
    if data_dir is None:
        return False
    bundled = stem_index.bundled_stable_ids(Path(data_dir))
    return bundled is not None and stable_id not in bundled


def _shared_hydration_dir(stable_id: str, request: Request) -> Path | None:
    """The data directory every hydrator of this machine shares, or ``None``
    when not armed (nothing to coordinate with) or for an id that names no
    bundle path (the caller's read is then the loader's own error)."""
    data_dir = getattr(request.app.state, "stem_hydration_data_dir", None)
    if data_dir is None or not is_safe_stable_id_segment(stable_id):
        return None
    return Path(data_dir)


def _this_track_fetching(stable_id: str, shared_dir: Path | None) -> bool:
    """This route's fetch is in flight, or a live process owns a fetch of it."""
    if _hydration_in_flight(stable_id):
        return True
    return shared_dir is not None and stem_hydration.hydrate_owner_alive(shared_dir, stable_id)


def _fetching_progress_suffixes(stable_id: str, request: Request) -> frozenset[str]:
    """Suffixes (``pid-token``) binding temp-dir progress to live hydrate attempts.

    One owner-marker scan lists every marker not provably dead, including this
    route's own in-flight fetch (the marker is written before the lock wait).
    Uses the same data-directory guard as :func:`_shared_hydration_dir`.
    """
    shared_dir = _shared_hydration_dir(stable_id, request)
    if shared_dir is None:
        return frozenset()
    _owner_alive, not_dead = stem_hydration.hydrate_owner_snapshot(shared_dir, stable_id)
    del _owner_alive
    return not_dead


def _armed_index_state(
    stable_id: str, data_dir: Path, stems_dir: Path, request: Request
) -> tuple[StemTrackState, str, str | None]:
    """Name an armed engine's state for a track that is neither local nor
    being fetched, from the cached cloud index: cloud, local, none or error."""
    if not stem_index.local_index_cache_path(data_dir).is_file():
        # load_cached_index reads a missing cache as empty; that is "not
        # fetched yet", never proof the track has no cloud bundle.
        return "error", "the cloud stem index has not been fetched yet", "STEM_INDEX_NOT_FETCHED"
    try:
        index = stem_index.load_cached_index(data_dir)
    except stem_index.StemIndexError as exc:
        return "error", str(exc), "STEM_INDEX_CORRUPT"
    if stable_id not in index:
        return "none", "no stem bundle on this machine or in the cloud", None
    # A fetch that finished between the local read and the in-flight check has
    # already landed the bundle; read again before calling it cloud-only, so a
    # just-landed retry never reads as "cloud".
    if _bundle_landed(stable_id, stems_dir, request):
        return "local", "stem bundle is on this machine", None
    return "cloud", "stem bundle is in the cloud and is fetched on demand", None
