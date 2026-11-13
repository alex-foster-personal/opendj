"""Believed-state playback selection, local copies and CloudSync refusals.

Config is dereferenced at call time through the shared adapter config module.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from apps.cloud import hydration
from apps.cloud import policy as cloud_policy
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.eviction import HydrationError
from apps.shared import audio_quality
from apps.shared.job_store import JobStoreLike
from apps.shared.state import locations as track_locations
from apps.shared.state import sync_stamp

from . import config
from .content_paths import audio_file, is_streaming_path, resolve_asset_path, resolve_content
from .errors import _open_ro, not_found, unavailable


def _picked_from_path(path: Path, *, source: str) -> track_locations.PickedAudio:
    media_type = config.AUDIO_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise not_found(
            "AUDIO_FILE_MISSING",
            f"unsupported audio extension for {path}",
        )
    quality = audio_quality.classify(str(path), None)
    return track_locations.PickedAudio(
        path=path,
        media_type=media_type,
        kind="local",
        venue_key=quality.venue.key if quality.venue else None,
        venue_rank=quality.venue.rank if quality.venue else None,
        source=source,
    )


def _rekordbox_copy(stable_id: str) -> Path | HTTPException | None:
    """rekordbox's own file for ``stable_id``: its path, why it won't play, or None.

    None covers every way there is no such file here: no vendor mapping, no
    FolderPath, a streaming row, a path this platform cannot map or that is not
    on disk, or no rekordbox install at all (``MASTER_DB_UNAVAILABLE``, which
    the analysis route reads the same way). A file that IS on disk but won't
    play (a dataless iCloud stub, an unsupported extension) comes back as its
    error, so the deck can say why once no other copy plays. Any other server
    error propagates: it means this machine could not look, not that the file
    is absent.
    """
    try:
        content = resolve_content(stable_id)
    except HTTPException as exc:
        if exc.status_code == 404 or _error_code(exc) == "MASTER_DB_UNAVAILABLE":
            return None
        raise
    try:
        path, _media_type = audio_file(content)
    except HTTPException as exc:
        return exc if _on_disk(content.folder_path) else None
    return path


def _on_disk(folder_path: str | None) -> bool:
    if not folder_path or is_streaming_path(folder_path):
        return False
    resolved = resolve_asset_path(folder_path).resolved
    return resolved is not None and resolved.exists()


def _error_code(exc: HTTPException) -> str | None:
    return exc.detail.get("code") if isinstance(exc.detail, dict) else None


@dataclasses.dataclass(frozen=True)
class _LocalCopy:
    """What this machine's own disk offers once believed state found nothing."""

    pick: track_locations.PickedAudio | None
    rekordbox_refusal: HTTPException | None


def _local_copy_pick(
    state: sqlite3.Connection,
    stable_id: str,
    share_policy: track_locations.PickPolicy | None,
) -> _LocalCopy:
    """Issue #3934: the deck plays any copy the listing counts as available.

    Believed state reads only the first local location row and
    ``tracks.file_path``, so rekordbox's own FolderPath (the path the tree's
    ``available_count`` and the row wire count first) or a working alternate
    can still be on disk. The candidates are
    :func:`track_locations.playable_candidate_paths`, the SAME list the
    listing probes, ranked by the share-cap picker. A rekordbox file that is
    here but unplayable comes back as its refusal so a local-only deck can
    say why (CLOUDSYNC-33).
    """
    rekordbox_copy = _rekordbox_copy(stable_id)
    primary = str(rekordbox_copy) if isinstance(rekordbox_copy, Path) else None
    candidates = track_locations.playable_candidate_paths(state, {stable_id: primary})
    pick = track_locations.pick_playable_from_candidates(state, stable_id, candidates[stable_id], policy=share_policy)
    if pick is not None and pick.source == "primary":
        pick = dataclasses.replace(pick, source="rekordbox-folder-path")
    return _LocalCopy(
        pick=pick,
        rekordbox_refusal=rekordbox_copy if isinstance(rekordbox_copy, HTTPException) else None,
    )


def _unavailable_refusal(
    stable_id: str,
    source: hydration.PlaybackSource,
    local_copy: _LocalCopy | None,
) -> HTTPException:
    """Why a track with no playable copy here will not load.

    A local-only machine (no CloudSync policy) says why rekordbox's file will
    not play, else plainly that it is not here (CLOUDSYNC-33); a configured
    machine names the cloud asset as unavailable.
    """
    if source.policy_source == "unconfigured":
        if local_copy is not None and local_copy.rekordbox_refusal is not None:
            return local_copy.rekordbox_refusal
        return not_found("AUDIO_NOT_ON_THIS_MACHINE", source.reason or hydration.NOT_ON_THIS_MACHINE)
    return not_found(
        "CLOUD_ASSET_UNAVAILABLE",
        source.reason or f"audio for track {stable_id} is unavailable on this machine",
    )


def _refuse_if_offloaded(stable_id: str) -> None:
    """OFFLOAD: an offloaded track is in the cloud on purpose, never "missing".

    409 ``TRACK_OFFLOADED`` tells the deck (and an agent) to restore it first,
    with ``POST /api/v1/offload/restore {mode: temporary}`` (spec e8da14b8).
    """
    from apps.cloud import offload

    row = offload.offload_states(config.DATA_DIR, [stable_id]).get(stable_id)
    if row is not None and row.state in ("offloaded", "restoring"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TRACK_OFFLOADED",
                "message": f"track {stable_id} is offloaded to the cloud; restore it before loading",
                "offload_state": row.state,
            },
        )


def resolve_playable_audio(
    stable_id: str,
    *,
    share: bool = False,
    jobs_store: JobStoreLike | None = None,
) -> track_locations.PickedAudio:
    """Pick the single file the frontend may play via believed-state resolution.

    Believed state answers WHETHER this machine may serve audio at all (its own
    copy, a hydrated cache entry, or not yet). For a share host it does not
    answer WHICH copy: a remote audience is capped at the share venue ceiling,
    so when several local copies exist the cap picks among them.
    """
    share_policy = track_locations.policy_from_env(share=True) if share else None
    if not share:
        from apps.shared.library_mode import library_mode

        library_mode()
    state = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        exists = state.execute(
            "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        if exists is None:
            raise not_found("TRACK_NOT_FOUND", f"unknown stable_id {stable_id}")
        machine_id = sync_stamp.local_machine_id(state)
        cache_dir = cloud_policy.artifact_cache_root(config.DATA_DIR, "audio")
        try:
            cfg = CloudConfig.from_env()
        except MissingEnvError:
            cfg = None
        try:
            source = hydration.resolve_playback_source(
                state,
                stable_id,
                machine_id,
                asset_kind="audio",
                cache_dir=cache_dir,
                cfg=cfg,
            )
        except HydrationError as exc:
            message = str(exc)
            code = "CLOUD_POLICY_UNCONFIGURED" if "no sync_policies row" in message else "CLOUD_HYDRATION_FAILED"
            raise unavailable(code, message) from exc
        # A cache entry is the one hydrated object, so there is nothing for the
        # cap to choose between; only a machine serving its own copies can hold
        # a master and a lossy alternate of the same track.
        share_pick = (
            track_locations.pick_playable(state, stable_id, policy=share_policy)
            if share_policy is not None and source.origin == "local"
            else None
        )
        # Issue #3934: believed state never tries rekordbox's FolderPath,
        # which the tree and rows count first, so a copy that is on disk here
        # wins over "unavailable" and over hydrating a remote object. A
        # configured ``excluded`` policy is the one refusal local bytes never
        # bypass (issue #13229): absence of a policy is not a refusal.
        explicitly_excluded = source.mode == "excluded" and source.policy_source != "unconfigured"
        local_copy = (
            _local_copy_pick(state, stable_id, share_policy)
            if source.origin in ("unavailable", "presigned") and not explicitly_excluded
            else None
        )
    finally:
        state.close()

    if source.origin in ("local", "cache"):
        if source.path is None:
            raise not_found(
                "AUDIO_FILE_MISSING",
                f"believed-state origin {source.origin!r} has no path for {stable_id}",
            )
        if share_pick is not None:
            # The capped pick reads the same two layers believed state does
            # (track_locations, then tracks.file_path), so None here is not a
            # missing file: it is the picker's stricter open probe rejecting a
            # path the believed-state residency gate admits, a FIFO the audio
            # route probes itself (#766, #2749). That path then stands as-is.
            return share_pick
        return _picked_from_path(
            source.path,
            source="believed-state-local" if source.origin == "local" else "believed-state-cache",
        )

    if local_copy is not None and local_copy.pick is not None:
        return local_copy.pick

    _refuse_if_offloaded(stable_id)

    if source.origin == "unavailable":
        raise _unavailable_refusal(stable_id, source, local_copy)

    if source.origin == "presigned":
        try:
            hydration.enqueue_hydrate_asset(
                jobs_store,
                stable_id=stable_id,
                asset_kind="audio",
                machine_id=machine_id,
                data_dir=config.DATA_DIR,
            )
        except HydrationError as exc:
            raise unavailable("CLOUD_HYDRATING", str(exc)) from exc
        raise unavailable(
            "CLOUD_HYDRATING",
            f"audio for track {stable_id} is hydrating from remote storage",
        )

    raise not_found(
        "AUDIO_FILE_MISSING",
        f"no working audio location for track {stable_id}",
    )
