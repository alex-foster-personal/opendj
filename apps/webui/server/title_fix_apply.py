"""Apply, reject and undo (with crash reconciliation) for the title-fix service (split out of title_fix_service)."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.tags.http import (
    BatchReleasedError,
    BatchRow,
    FenceExitError,
    ProposalRow,
    StoreBusyError,
    load_config,
    tag_family,
)

from .backend import (
    AppliedState,
    BatchConflictError,
    CommittedReadbackError,
    TrackUpdate,
)
from .backend import Track as LibraryTrack
from .etag import compute_etag
from .title_fix_core import (
    CFG,
    Ctx,
    Outcome,
    TitleFixError,
    _artist_flat,
    _artists_or_refuse,
    _get_or_404,
    _title_text,
    _when_store_free,
    _written_artists,
)

_CLAIMED = "claimed by a concurrent apply"




def apply_proposals(
    ctx: Ctx, proposal_ids: list[str], *, kind: str = "manual", policy: str = "manual", scan_id: str | None = None,
    tag_families: tuple[str, ...] | None = None,
) -> Outcome:
    """``tag_families`` is the snapshot an auto-applying scan started under; a manual apply reads the current setting."""
    _check_apply_selection(proposal_ids, kind)
    families = set(load_config(ctx.paths).tag_families if tag_families is None else tag_families)
    rows = _get_or_404(ctx, list(dict.fromkeys(proposal_ids)))
    outcome = Outcome()
    current = ctx.backend.get_tracks_bulk([r.stable_id for r in rows])
    updates: list[TrackUpdate] = []
    chosen: list[ProposalRow] = []
    tags_added: dict[str, list[str]] = {}
    seen: set[str] = set()
    for row in rows:
        update = _plan_row_apply(ctx, row, current.get(row.stable_id), seen, families, outcome)
        if update is None:
            continue
        seen.add(row.stable_id)
        tags_added[row.proposal_id] = update[1]
        chosen.append(row)
        updates.append(update[0])
    if not updates:
        return outcome
    try:
        batch_id, outcome.applied, chosen = _write_replanning(
            ctx, chosen, updates, tags_added, outcome, kind=kind, policy=policy, scan_id=scan_id)
    except (CommittedReadbackError, FenceExitError) as exc:
        return _committed_outcome(outcome, exc)
    outcome.batch_id = batch_id or None
    outcome.skipped += [{"proposal_id": r.proposal_id, "reason": _CLAIMED}
                        for r in chosen if r.proposal_id not in outcome.applied]
    return outcome


def _write_replanning(
    ctx: Ctx, chosen: list[ProposalRow], updates: list[TrackUpdate], tags_added: dict[str, list[str]], outcome: Outcome, *,
    kind: str, policy: str, scan_id: str | None,
) -> tuple[str, list[str], list[ProposalRow]]:
    """_write_apply_batch, but a track that changed between the plan and the compare-and-swap is marked stale (the same
    verdict the plan gives a row it sees changed) and the batch is re-planned without it, instead of failing whole."""
    for _ in range(CFG.CAS_REPLAN_ROUNDS):
        if not updates:
            return "", [], chosen
        try:
            batch_id, applied = _write_apply_batch(ctx, chosen, updates, tags_added, kind=kind, policy=policy, scan_id=scan_id)
        except BatchConflictError as exc:
            chosen, updates = _drop_cas_losers(ctx, chosen, updates, exc, outcome)
            if not updates:
                return "", [], chosen  # every remaining row lost: report them stale instead of a bare 409
        else:
            return batch_id, applied, chosen
    raise TitleFixError("title_fix_track_conflict", "tracks kept changing while the batch was written; nothing applied", 409)


def _drop_cas_losers(
    ctx: Ctx, chosen: list[ProposalRow], updates: list[TrackUpdate], exc: BatchConflictError, outcome: Outcome,
) -> tuple[list[ProposalRow], list[TrackUpdate]]:
    """Mark the rows whose track lost the compare-and-swap stale and return the plan without them."""
    lost = {c["stable_id"] for c in exc.conflicts}
    losers = [r for r in chosen if r.stable_id in lost]
    _when_store_free(ctx.store.resolve_pending, [r.proposal_id for r in losers], "stale")
    outcome.skipped += [{"proposal_id": r.proposal_id, "reason": "stale"} for r in losers]
    kept = [(r, u) for r, u in zip(chosen, updates, strict=True) if r.stable_id not in lost]
    return [r for r, _ in kept], [u for _, u in kept]


def _plan_row_apply(
    ctx: Ctx, row: ProposalRow, track: LibraryTrack | None, seen: set[str], families: set[str], outcome: Outcome,
) -> tuple[TrackUpdate, list[str]] | None:
    """The CAS update (and the tags it adds) for one proposal, or None after recording why it is skipped."""
    reason = _apply_refusal(row, track, seen) or _identity_refusal(ctx, row)
    if reason == "stale" and not _when_store_free(ctx.store.resolve_pending, [row.proposal_id], "stale"):
        reason = _CLAIMED  # an apply claimed it since this request read it: not ours to overwrite
    if reason:
        outcome.skipped.append({"proposal_id": row.proposal_id, "reason": reason})
        return None
    assert track is not None  # _apply_refusal refuses a missing track
    added = [t for t in row.tags if tag_family(t) in families and t not in (track.tags or [])]
    patch = _apply_patch(row, added)
    if not patch:
        won = _when_store_free(ctx.store.resolve_pending, [row.proposal_id], "noop")
        outcome.skipped.append({"proposal_id": row.proposal_id, "reason": "nothing to change" if won else _CLAIMED})
        return None
    etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    return TrackUpdate(row.stable_id, patch, etag, tags_absent=tuple(added)), added


def _check_apply_selection(proposal_ids: list[str], kind: str) -> None:
    """The cap bounds a selection a person or client sends; an auto-apply is the scan's own full eligible set."""
    if not proposal_ids:
        raise TitleFixError("title_fix_empty_selection", "select at least one proposal", 422)
    if kind != "auto" and len(proposal_ids) > CFG.MAX_SELECTION:
        raise TitleFixError("title_fix_selection_too_large", f"at most {CFG.MAX_SELECTION} proposals per apply", 422)


def _apply_patch(row: ProposalRow, tags_add: list[str]) -> dict[str, object]:
    """The update_tracks patch for one proposal; empty when it would change nothing."""
    patch: dict[str, object] = {}
    if row.changes_title:
        patch["title"] = row.proposed_title
    if row.changes_artist:
        patch["artists"] = _written_artists(row)
    if tags_add:
        patch["tags_add"] = tags_add
    return patch


def _write_apply_batch(
    ctx: Ctx, chosen: list[ProposalRow], updates: list[TrackUpdate], tags_added: dict[str, list[str]], *,
    kind: str, policy: str, scan_id: str | None = None,
) -> tuple[str, list[str]]:
    """Records the batch intent first, then writes; a failed write rolls the proposals back to pending."""
    batch, claimed = _open_batch(ctx, chosen, len(updates), tags_added, kind=kind, policy=policy, scan_id=scan_id)
    won = [(r, u) for r, u in zip(chosen, updates, strict=True) if r.proposal_id in claimed]
    if not won:  # a concurrent apply claimed every row first
        _when_store_free(ctx.store.discard_batch, batch.batch_id)
        return "", []
    if len(won) != len(updates):
        _record_won_count(ctx, batch.batch_id, len(won))
    written: dict[str, AppliedState] = {}  # filled inside the write transaction, before its COMMIT
    readback_error = _write_tracks_or_release(ctx, batch.batch_id, [u for _, u in won], scan_id, written)
    # only now may Undo see it; waited out under contention, or a competing sidecar writer hides a landed change
    _when_store_free(ctx.store.mark_batch_committed, batch.batch_id, applied_state=_by_proposal(won, written))
    publish("library.changed", {"kind": "tracks", "ids": [u.stable_id for _, u in won]})
    _raise_committed_readback(readback_error, batch.batch_id, [r.proposal_id for r, _ in won])
    return batch.batch_id, [r.proposal_id for r, _ in won]


def _committed_outcome(outcome: Outcome, exc: CommittedReadbackError | FenceExitError) -> Outcome:
    """A write that COMMITTED and is recorded (and undoable) is a success with a warning, never a failed request."""
    outcome.batch_id = exc.batch_id
    outcome.applied = list(exc.applied)
    outcome.warning = f"the write committed and is undoable, but its result could not be confirmed ({type(exc).__name__})"
    return outcome


def _raise_committed_readback(
    readback_error: CommittedReadbackError | FenceExitError | None, batch_id: str, applied: list[str] | None = None,
) -> None:
    """The write landed and is undoable; only its result could not be re-read. The batch id rides on the error so the
    scan's auto-apply links the undoable batch to its row."""
    if readback_error is None:
        return
    readback_error.batch_id = batch_id
    readback_error.applied = tuple(applied or ())
    raise readback_error


def _open_batch(
    ctx: Ctx, chosen: list[ProposalRow], count: int, tags_added: dict[str, list[str]], *,
    kind: str, policy: str, scan_id: str | None,
) -> tuple[BatchRow, set[str]]:
    """The batch intent and its claim on the proposals, both waited out under contention. If the claim cannot be made
    (the scan lost its lease, or the store stayed busy) nothing was written: the batch is discarded, never left empty."""
    batch = _when_store_free(ctx.store.create_batch, "auto" if kind == "auto" else "manual", policy, count)
    try:
        claimed = _when_store_free(
            ctx.store.claim_pending, [r.proposal_id for r in chosen], batch.batch_id, tags_added, fence_scan_id=scan_id)
    except Exception:  # incl. ScanLeaseLostError: an auto-apply whose scan was taken over claims and writes nothing
        _when_store_free(ctx.store.release_batch, batch.batch_id)
        _when_store_free(ctx.store.discard_batch, batch.batch_id)
        raise
    return batch, set(claimed)


def _record_won_count(ctx: Ctx, batch_id: str, count: int) -> None:
    """The batch's row count after a partial claim. Waited out under contention; if it still fails nothing was written,
    so the claimed rows go back to pending rather than staying attached to a batch that will never commit."""
    try:
        _when_store_free(ctx.store.set_batch_count, batch_id, count)
    except Exception:
        _when_store_free(ctx.store.release_batch, batch_id)
        _when_store_free(ctx.store.discard_batch, batch_id)
        raise


def _by_proposal(won: list[tuple[ProposalRow, TrackUpdate]], written: dict[str, AppliedState]) -> dict[str, dict[str, Any]]:
    """The write's in-transaction track state, keyed by the proposal that wrote each track."""
    return {row.proposal_id: asdict(written[row.stable_id]) for row, _ in won if row.stable_id in written}


def _write_in_fence(open_fence: Callable[[], contextlib.AbstractContextManager[Any]], write: Callable[[], Any]) -> None:
    """Run ``write`` inside a store fence. A fence that cannot be ENTERED while another writer holds the store is waited
    out for the contention budget (nothing was written yet, so a retry is safe); a busy store met after the body has
    started is never retried, and neither is any other error."""
    give_up_at = time.monotonic() + CFG.STORE_BUSY_WAIT_S
    while True:
        entered = False
        try:
            with open_fence():
                entered = True
                write()
        except StoreBusyError:
            if entered or time.monotonic() >= give_up_at:
                raise
            time.sleep(CFG.STORE_BUSY_PAUSE_S)
        else:
            return


def _write_tracks_or_release(
    ctx: Ctx, batch_id: str, updates: list[TrackUpdate], scan_id: str | None, written: dict[str, AppliedState],
) -> CommittedReadbackError | FenceExitError | None:
    """The track write, with the batch's ownership held through the state commit; a failure releases the batch.
    Returns the error of a write that COMMITTED but whose result could not be re-read: the caller keeps the batch."""
    try:
        _write_in_fence(lambda: ctx.store.write_fence(batch_id, scan_id), lambda: ctx.backend.update_tracks(
            updates, source=CFG.WRITE_SOURCE, mutation_guard=ctx.write_gate, applied_state=written))
    except (CommittedReadbackError, FenceExitError) as exc:  # the write committed: keep the record of it
        return exc
    except Exception as exc:
        _when_store_free(ctx.store.release_batch, batch_id)  # only this batch's own rows; waited out, or they stay claimed
        _when_store_free(ctx.store.discard_batch, batch_id)
        if isinstance(exc, BatchReleasedError):  # reconciled away while this writer was paused: nothing was written
            raise TitleFixError("title_fix_batch_released", str(exc), 409) from exc
        if isinstance(exc, StoreBusyError):  # another writer held the store past the wait: nothing was written
            raise TitleFixError("title_fix_store_busy", str(exc), 409) from exc
        raise
    return None


@dataclass(frozen=True)
class _StoredIdentity:
    """What the tracks row itself holds: the exact artists list (the flat facade joins ["A", "B"] and ["A, B"] alike)
    and whether it is soft-deleted (the backend's bulk read still returns such rows)."""

    artists: list[str]
    removed: bool


def _stored_identity(ctx: Ctx, stable_id: str) -> _StoredIdentity | None:
    conn = state_db.open_ro(ctx.paths.state_db)
    try:
        row = conn.execute("SELECT artists_json, deleted_at FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else _StoredIdentity(_artists_or_refuse(stable_id, row[0]), row[1] is not None)


def _identity_refusal(ctx: Ctx, row: ProposalRow) -> str:
    """Why the stored row, read exactly, cannot take this proposal: removed from the library, or its artists list was
    hand-edited since the scan (TFIX-02: an artist moved since the scan invalidates the proposal). Empty when it can."""
    stored = _stored_identity(ctx, row.stable_id)
    if stored is None or stored.removed:
        return "track removed from the library"
    if stored.artists != row.original_artists:  # whichever field the proposal changes, the artist must not have moved
        return "stale"
    return ""


def _apply_refusal(row: ProposalRow, track: object, seen: set[str]) -> str:
    if row.state != "pending":
        return f"state is {row.state}"
    if row.fixer_status == "unknown":
        return "fixer verdict unknown"
    if row.changes_title and not row.proposed_title.strip():
        return "proposed title is empty"  # a junk-only title with no usable filename: identity_patch would reject it
    if track is None:
        return "track not found"
    if row.stable_id in seen:
        return "another selected proposal already targets this track"
    if _title_text(track.title) != row.original_title or _artist_flat(track.artist) != row.original_artist:  # type: ignore[attr-defined]
        return "stale"
    return ""


def reject_proposals(ctx: Ctx, proposal_ids: list[str]) -> Outcome:
    if not proposal_ids:
        raise TitleFixError("title_fix_empty_selection", "select at least one proposal", 422)
    rows = _get_or_404(ctx, list(dict.fromkeys(proposal_ids)))
    outcome = Outcome()
    pending = [r.proposal_id for r in rows if r.state == "pending"]
    outcome.skipped += [{"proposal_id": r.proposal_id, "reason": f"state is {r.state}"} for r in rows if r.state != "pending"]
    outcome.applied = ctx.store.reject_pending(pending)  # conditional: an apply may have claimed a row since we read it
    outcome.skipped += [{"proposal_id": p, "reason": _CLAIMED} for p in pending if p not in outcome.applied]
    return outcome
