"""Undo of an applied batch and the crash reconciliation of stalled applies and undos (split out of title_fix_apply)."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from apps.shared.events import publish
from apps.tags.http import (
    BatchRow,
    FenceExitError,
    ProposalFilter,
    ProposalRow,
    StoreBusyError,
    UndoIntentLostError,
)

from .backend import (
    BatchConflictError,
    CommittedReadbackError,
    TrackUpdate,
    applied_state_of,
)
from .backend import Track as LibraryTrack
from .etag import compute_etag
from .title_fix_apply import _stored_identity, _StoredIdentity, _write_in_fence
from .title_fix_core import (
    _ALL_ROWS,
    CFG,
    UNVERIFIED,
    Ctx,
    Outcome,
    TitleFixError,
    _title_text,
    _when_store_free,
    _written_artists,
)


def undo_batch(ctx: Ctx, batch_id: str, *, skip_conflicts: bool = False) -> Outcome:
    _check_undoable(ctx, batch_id)
    rows, _ = ctx.store.list_proposals(ProposalFilter(state="applied", batch_id=batch_id, limit=_ALL_ROWS))
    current = ctx.backend.get_tracks_bulk([r.stable_id for r in rows])
    outcome = Outcome(batch_id=batch_id)
    updates: list[TrackUpdate] = []
    undone: list[str] = []
    applied_state = ctx.store.get_batch(batch_id).applied_state
    for row in rows:
        track = current.get(row.stable_id)
        if track is None or _changed_since_apply(ctx, row, track, applied_state.get(row.proposal_id)):
            outcome.skipped.append({"proposal_id": row.proposal_id, "reason": "track changed since apply"})
            continue
        patch = _undo_patch(row)
        undone.append(row.proposal_id)
        if patch:
            updates.append(_undo_update(row, track, patch, applied_state.get(row.proposal_id)))
    if outcome.skipped and not skip_conflicts:
        raise TitleFixError(
            "title_fix_undo_conflict",
            f"{len(outcome.skipped)} track(s) changed since this batch was applied; nothing was undone. "
            "Retry with skip_conflicts to undo the rest.",
            extra={"conflicts": outcome.skipped},
        )
    token, undone, readback_error = _restore_replanning(ctx, batch_id, rows, updates, undone, outcome, skip_conflicts)
    # A death from here leaves the intent for _reconcile_stalled_undo; both writes wait out contention, or a competing
    # sidecar writer would report a database failure for tracks that were already restored.
    _when_store_free(ctx.store.set_state, undone, "undone")
    _when_store_free(ctx.store.finish_undo, batch_id, complete=not outcome.skipped, token=token)
    outcome.applied = undone
    if readback_error is not None:  # the restore landed and is recorded; only its result could not be re-read
        outcome.warning = f"the restore committed and is recorded, but its result could not be confirmed ({type(readback_error).__name__})"
    return outcome


def _start_undo(ctx: Ctx, batch_id: str, undone: list[str]) -> str:
    token = _when_store_free(ctx.store.mark_undo_started, batch_id, undone)  # intent first: a death after the restore is then recoverable
    if token is None:
        raise TitleFixError("title_fix_undo_in_progress", f"batch {batch_id} is already being undone", 409)
    return token


def _restore_replanning(
    ctx: Ctx, batch_id: str, rows: list[ProposalRow], updates: list[TrackUpdate], undone: list[str], outcome: Outcome,
    skip_conflicts: bool,
) -> tuple[str, list[str], CommittedReadbackError | FenceExitError | None]:
    """The restore. A track that changed between the plan and the compare-and-swap is the documented undo conflict:
    refused whole by default, or (skip_conflicts) skipped while the unaffected rows are restored on a fresh intent."""
    token = _start_undo(ctx, batch_id, undone)
    for _ in range(CFG.CAS_REPLAN_ROUNDS):
        if not updates:
            return token, undone, None
        try:
            readback_error = _restore_tracks_or_release(ctx, batch_id, token, updates)
        except BatchConflictError as exc:  # the intent was released by _restore_tracks_or_release
            updates, undone = _drop_undo_losers(rows, updates, undone, outcome, exc, skip_conflicts)
            token = _start_undo(ctx, batch_id, undone)
        else:
            publish("library.changed", {"kind": "tracks", "ids": [u.stable_id for u in updates]})
            return token, undone, readback_error
    if not updates:  # the last round dropped every remaining row: report them skipped instead of a bare 409
        return token, undone, None
    _when_store_free(ctx.store.finish_undo, batch_id, complete=False, token=token)  # no writer behind the last intent
    raise TitleFixError("title_fix_undo_conflict", "tracks kept changing while the undo was written; nothing was undone", 409)


def _drop_undo_losers(
    rows: list[ProposalRow], updates: list[TrackUpdate], undone: list[str], outcome: Outcome, exc: BatchConflictError,
    skip_conflicts: bool,
) -> tuple[list[TrackUpdate], list[str]]:
    """The undo conflict contract for a CAS loser: refuse whole, or skip it and keep the unaffected rows."""
    lost = {c["stable_id"] for c in exc.conflicts}
    lost_ids = {r.proposal_id for r in rows if r.stable_id in lost}
    skipped = [{"proposal_id": pid, "reason": "track changed since apply"} for pid in sorted(lost_ids)]
    if not skip_conflicts:
        raise TitleFixError(
            "title_fix_undo_conflict",
            f"{len(skipped)} track(s) changed while the undo was written; nothing was undone. "
            "Retry with skip_conflicts to undo the rest.",
            extra={"conflicts": skipped},
        ) from exc
    outcome.skipped += skipped
    return [u for u in updates if u.stable_id not in lost], [pid for pid in undone if pid not in lost_ids]


def _restore_tracks_or_release(
    ctx: Ctx, batch_id: str, token: str, updates: list[TrackUpdate],
) -> CommittedReadbackError | FenceExitError | None:
    """The restore, with the undo intent held as this writer's through the track commit; a failure releases it.
    Returns the error of a restore that COMMITTED but whose result could not be re-read: the undo is recorded."""
    try:
        _write_in_fence(lambda: ctx.store.undo_fence(batch_id, token), lambda: ctx.backend.update_tracks(
            updates, source=CFG.WRITE_SOURCE, mutation_guard=ctx.write_gate))
    except (CommittedReadbackError, FenceExitError) as exc:  # the write committed: keep the record of it
        return exc
    except UndoIntentLostError as exc:  # a reconcile cleared it and another undo took over: not ours to clear
        raise TitleFixError("title_fix_undo_in_progress", str(exc), 409) from exc
    except Exception:
        _when_store_free(ctx.store.finish_undo, batch_id, complete=False, token=token)  # nothing restored: release it
        raise
    return None


def _originals_of(ctx: Ctx, batch_id: str) -> list[dict[str, Any]]:
    """What a person needs to revert a refused batch by hand: each track with the title and artist it had before."""
    rows, _ = ctx.store.list_proposals(ProposalFilter(state="applied", batch_id=batch_id, limit=_ALL_ROWS))
    return [{"stable_id": r.stable_id, "original_title": r.original_title, "original_artist": r.original_artist,
             "original_artists": r.original_artists} for r in rows]


def _check_undoable(ctx: Ctx, batch_id: str) -> None:
    try:
        batch = ctx.store.get_batch(batch_id)
    except KeyError as exc:
        raise TitleFixError("title_fix_batch_not_found", str(exc), 404) from exc
    if batch.undone_at is not None:
        raise TitleFixError("title_fix_batch_already_undone", f"batch {batch_id} was undone at {batch.undone_at}")
    if batch.committed_at is None:
        batch = _reconcile_stalled_batch(ctx, batch)
    if batch is None:
        raise TitleFixError("title_fix_batch_not_found", f"batch {batch_id} never landed and was released", 404)
    if batch.committed_at is None:
        raise TitleFixError("title_fix_batch_not_committed", f"batch {batch_id} is still being applied", 409)
    if any(state.get(UNVERIFIED) for state in batch.applied_state.values()):
        raise TitleFixError(
            "title_fix_undo_unverifiable",
            f"batch {batch_id} was recovered after a crash, so edits made before the recovery cannot be told from its "
            "own writes; revert those tracks by hand to the originals listed in this error",
            409,
            extra={"originals": _originals_of(ctx, batch_id)},
        )
    if batch.undo_started_at is not None:
        _reconcile_stalled_undo(ctx, batch)


@contextlib.contextmanager
def _exclusive(ctx: Ctx, busy_code: str, busy_message: str) -> Iterator[sqlite3.Connection]:
    """The store's write lock for a reconcile's whole decision and action; a live writer still holding it is not stalled."""
    try:
        with ctx.store.exclusive() as conn:
            yield conn
    except StoreBusyError as exc:
        raise TitleFixError(busy_code, busy_message, 409) from exc


def _reconcile_stalled_batch(ctx: Ctx, batch: BatchRow) -> BatchRow | None:
    """Finish or release a batch left between its track write and its commit stamp (the two live in separate databases).

    Within the grace window it may still be mid-apply and is left alone. Once stalled, the verdict is taken UNDER the
    store's write lock, which a live writer holds through its track commit: the batch is re-read after acquiring it,
    and the tracks are observed only then. The write is atomic, so affirmative evidence decides: a track holding the
    values the batch wrote means it landed (stamped committed); a track still holding its originals means it never
    landed (claims return to pending, batch discarded, None). If no track shows either, because each was edited
    or deleted since, the batch is left stuck with a 409 rather than guessed.
    """
    age_s = (datetime.now(UTC) - datetime.fromisoformat(batch.created_at)).total_seconds()
    if age_s < CFG.BATCH_GRACE_S:
        return batch
    with _exclusive(ctx, "title_fix_batch_not_committed", f"batch {batch.batch_id} is still being applied") as conn:
        try:
            fresh = ctx.store.get_batch(batch.batch_id)  # revalidate: a live writer may have finished while we waited
        except KeyError:
            return None
        if fresh.committed_at is not None or fresh.undone_at is not None:
            return fresh
        rows, _ = ctx.store.list_proposals(ProposalFilter(state="applied", batch_id=batch.batch_id, limit=_ALL_ROWS))
        current = ctx.backend.get_tracks_bulk([r.stable_id for r in rows])
        tracks = [current.get(r.stable_id) for r in rows]
        if _batch_landed(ctx, batch.batch_id, rows, tracks):
            # The in-transaction baseline died with the process. The state NOW would bless any edit made between the
            # write and this recovery, so the batch is marked unverifiable and Undo refuses it (_check_undoable).
            recovered = {r.proposal_id: {"etag": "", "tags": None, UNVERIFIED: True} for r in rows}
            ctx.store.mark_batch_committed(batch.batch_id, applied_state=recovered, conn=conn)
        else:
            ctx.store.release_batch(batch.batch_id, conn=conn)
            ctx.store.discard_batch(batch.batch_id, conn=conn)
            return None
    return ctx.store.get_batch(batch.batch_id)


def _reconcile_stalled_undo(ctx: Ctx, batch: BatchRow) -> None:
    """Finish or clear an undo whose process died between restoring the tracks and recording it (separate databases).

    Within the grace window it may still be running. Once stalled, the verdict is taken under the store's write lock,
    which a live undo holds through its track commit, and the intent is re-read after acquiring it. Each proposal
    is judged on its own: one whose track already shows its originals was restored, so it is marked undone; the
    rest stay applied (real conflicts of a skip_conflicts undo). All restored finishes the batch; none restored
    clears the intent to run again.
    """
    assert batch.undo_started_at is not None
    in_progress = f"batch {batch.batch_id} is already being undone"
    age_s = (datetime.now(UTC) - datetime.fromisoformat(batch.undo_started_at.split("#")[0])).total_seconds()
    if age_s < CFG.BATCH_GRACE_S:
        raise TitleFixError("title_fix_undo_in_progress", in_progress, 409)
    with _exclusive(ctx, "title_fix_undo_in_progress", in_progress) as conn:
        fresh = ctx.store.get_batch(batch.batch_id)
        if fresh.undone_at is not None:
            raise TitleFixError("title_fix_batch_already_undone", f"batch {batch.batch_id} was undone at {fresh.undone_at}")
        if fresh.undo_started_at != batch.undo_started_at:  # finished, cleared or taken over while we waited
            if fresh.undo_started_at is not None:
                raise TitleFixError("title_fix_undo_in_progress", in_progress, 409)
            return
        if not _record_restored_rows(ctx, batch.batch_id, fresh.undo_targets, conn):
            return
    raise TitleFixError("title_fix_batch_already_undone", f"batch {batch.batch_id} was undone (recovered)")


def _record_restored_rows(ctx: Ctx, batch_id: str, targets: list[str] | None, conn: sqlite3.Connection) -> bool:
    """Judge each row the stalled undo TARGETED by its track (read under the held lock) and record what landed.
    A row a skip_conflicts undo left out may already show its originals because someone restored it by hand; it was
    never this undo's to record. ``targets`` None is an intent that predates the record: every applied row counts.
    True when every applied row was restored, so the batch is finished; False when the undo never landed or only
    partly did."""
    rows, _ = ctx.store.list_proposals(ProposalFilter(state="applied", batch_id=batch_id, limit=_ALL_ROWS))
    current = ctx.backend.get_tracks_bulk([r.stable_id for r in rows])
    targeted = [r for r in rows if targets is None or r.proposal_id in targets]
    restored = [r for r in targeted if _landing_evidence(ctx, r, current.get(r.stable_id)) == "original"]
    if restored:  # each proposal on its own: real conflicts of a skip_conflicts undo stay applied
        ctx.store.set_state([r.proposal_id for r in restored], "undone", conn=conn)
    complete = bool(restored) and len(restored) == len(rows)
    ctx.store.finish_undo(batch_id, complete=complete, conn=conn)  # nothing restored: just release the intent
    return complete


def _batch_landed(ctx: Ctx, batch_id: str, rows: list[ProposalRow], tracks: list[LibraryTrack | None]) -> bool:
    """True when a track holds the batch's written values, False when EVERY track still holds its originals (the
    write is atomic, so a landed batch leaves none) or when the batch claimed no row at all (it could not have written
    a track). Raises 409 when originals are mixed with edited or removed rows, when the evidence is absent (every track
    edited or removed since), or when it is contradictory (one track shows the written values and another its
    originals, which no atomic write can produce)."""
    verdicts = {_landing_evidence(ctx, r, t) for r, t in zip(rows, tracks, strict=True)}
    if not rows:
        return False
    if {"applied", "original"} <= verdicts:
        raise TitleFixError(
            "title_fix_batch_unverifiable",
            f"batch {batch_id} cannot be told landed from never-landed: one track shows the proposed values and "
            "another its originals, which an atomic write cannot leave. It is left as it is rather than guessed.", 409)
    if "applied" in verdicts:
        return True
    if verdicts == {"original"}:
        return False
    if "original" in verdicts:  # the rest are "unknown": an atomic apply may have landed on rows edited or removed since
        raise TitleFixError(
            "title_fix_batch_unverifiable",
            f"batch {batch_id} cannot be told landed from never-landed: some tracks still show their originals and the "
            "rest were edited or removed since, so the write may have landed on them. It is left as it is rather than guessed.", 409)
    raise TitleFixError(
        "title_fix_batch_unverifiable",
        f"batch {batch_id} cannot be told landed from never-landed: no track shows the proposed or the original "
        "values (edited or missing since). It is left as it is rather than guessed.", 409)


def _landing_evidence(ctx: Ctx, row: ProposalRow, track: LibraryTrack | None) -> str:
    """What one claimed row's track says about whether its batch landed: 'applied' (it holds every value the row
    wrote), 'original' (it still holds what the row would have replaced) or 'unknown' (edited since, removed or
    missing). Artists are judged as the exact stored list, not the flat text that joins ["A", "B"] and ["A, B"]."""
    stored = _stored_identity(ctx, row.stable_id)
    if track is None or stored is None or stored.removed:
        return "unknown"
    if _holds_proposed(row, track, stored):
        return "applied"
    if _shows_originals(row, track, stored):
        return "original"
    return "unknown"


def _holds_proposed(row: ProposalRow, track: LibraryTrack, stored: _StoredIdentity) -> bool:
    """The track holds every value this proposal wrote (and it wrote at least one)."""
    if not (row.changes_title or row.changes_artist or row.tags_added):
        return False
    if row.changes_title and track.title != row.proposed_title:
        return False
    if row.changes_artist and stored.artists != _written_artists(row):
        return False
    return all(t in (track.tags or []) for t in row.tags_added)


def _shows_originals(row: ProposalRow, track: LibraryTrack, stored: _StoredIdentity) -> bool:
    """The track still holds the fields this proposal would have replaced (only those it changes), and none of the
    tags it would have added."""
    if row.changes_title and _title_text(track.title) != row.original_title:
        return False
    if row.changes_artist and stored.artists != row.original_artists:
        return False
    return not any(t in (track.tags or []) for t in row.tags_added)


def _moved_since(track: LibraryTrack, applied: dict[str, Any]) -> bool:
    """The track's ETag, or its complete tag set when the baseline recorded one (a migrated legacy baseline has none)."""
    now = applied_state_of(track)
    return now.etag != applied["etag"] or (applied["tags"] is not None and now.tags != tuple(applied["tags"]))


def _changed_since_apply(
    ctx: Ctx, row: ProposalRow, track: LibraryTrack, applied: dict[str, Any] | None = None,
) -> bool:
    """The track was removed, its ETag or complete tag set moved since the apply wrote it (any row or tag edit,
    whatever field), or a field this proposal wrote no longer holds the proposed value. The artists are compared as
    the exact list the apply wrote, not the flat text, so ["A, B"] edited to ["A", "B"] counts. applied is None for a
    batch that recorded none (a crash before its commit marker, an older batch): the field checks alone then decide."""
    stored = _stored_identity(ctx, row.stable_id)
    if stored is None or stored.removed:
        return True  # a removed track is a conflict whatever fields the proposal wrote: undo must not touch its row
    if applied is not None and _moved_since(track, applied):
        return True
    if any(tag not in (track.tags or []) for tag in row.tags_added):
        return True  # a tag this batch added was taken off since
    if row.changes_title and track.title != row.proposed_title:
        return True
    return row.changes_artist and stored.artists != _written_artists(row)




def _undo_update(
    row: ProposalRow, track: LibraryTrack, patch: dict[str, object], applied: dict[str, Any] | None = None,
) -> TrackUpdate:
    """The CAS update that restores one row; only an undo may write back a title that was blank when scanned.
    With a recorded baseline the write also pins the COMPLETE applied tag set, so any tag edit that leaves the ETag
    alone still conflicts inside the write transaction."""
    etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    return TrackUpdate(
        row.stable_id, patch, etag,
        allow_blank_title=row.changes_title and not row.original_title.strip(), tags_present=tuple(row.tags_added),
        allow_blank_artist_members=row.changes_artist and "" in row.original_artists,
        tags_exact=None if applied is None or applied["tags"] is None else tuple(applied["tags"]),
    )


def _undo_patch(row: ProposalRow) -> dict[str, object]:
    """Restores the original title and artists list and removes exactly the tags the batch added."""
    patch: dict[str, object] = {}
    if row.changes_title:
        patch["title"] = row.original_title
    if row.changes_artist:
        patch["artists"] = row.original_artists
    if row.tags_added:
        patch["tags_remove"] = row.tags_added
    return patch
