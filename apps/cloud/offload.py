"""Offload a track's local files to R2, restore them, and re-offload on a timer.

OFFLOAD (spec ``(private path)``). Built from existing parts:
:func:`apps.cloud.asset_store.push_asset` uploads under the one
content-addressed layout (``r2_keys``), :mod:`apps.cloud.offload_verify` proves
the copy before anything is deleted, the synced ``track_locations`` rows are
written through hydration's own stamped helpers, and stem bundles leave through
the stem cache's own claim (``stem_bundles.claim_and_remove``) with their index
entry published only after every part is verified (the PR #6030 P1 rule).

Safety order for one track, every time:

1. refuse when the track is on a deck or a gig is running;
2. hash, upload and VERIFY every file (nothing is deleted yet);
3. publish the stem index entry (verified hashes only) and the remote row;
4. re-check the deck/gig gate and that no file changed since it was hashed;
5. delete the local copies, recording each deletion.

A failure or cancel before step 5 leaves every local file where it was.

-Claude
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp

from . import offload_ledger as ledger
from . import stem_bundles, stem_cache_budget, stem_index, transfer_status
from .asset_store import asset_object_key, push_asset
from .config import CloudConfig
from .hydration import _mark_local_unavailable, _upsert_remote_location
from .lock import S3Client
from .offload_verify import (
    LocalDigest,
    OffloadCancelled,
    OffloadS3,
    OffloadVerifyError,
    download_verified,
    hash_local_file,
    unchanged_since_hash,
    verify_remote_copy,
)

log = logging.getLogger(__name__)

TEMPORARY_RESTORE_HOURS: int = 24
#: An audio file is renamed to ``<name>.offload-deleting-<hex>`` before its final check.
DELETE_CLAIM_INFIX: str = ".offload-deleting-"
SWEEP_INTERVAL_S: float = 15 * 60.0

Progress = Callable[[str, int, int], None]


class OffloadRefused(RuntimeError):
    """The request cannot run now; ``code`` is the agent-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OffloadContext:
    """Everything one offload or restore needs, injected so tests use real seams."""

    data_dir: Path
    state_db: Path
    stems_dir: Path
    cfg: CloudConfig
    assets: OffloadS3
    index_s3: S3Client
    #: stable_ids on a deck now (open-deck registry plus the UI mirror).
    protected_ids: Callable[[], frozenset[str]]
    #: a gig-freeze defer reason, or None (``refuse_sync_round``).
    gate_reason: Callable[[], str | None]
    #: rekordbox's FolderPath for a track (the deck's first candidate), or None.
    primary_path: Callable[[str], str | None] = lambda _sid: None
    cancelled: Callable[[], bool] = lambda: False
    on_progress: Progress = lambda _sid, _done, _total: None
    now: Callable[[], datetime] = ledger.utc_now


@dataclass(frozen=True)
class TrackOutcome:
    stable_id: str
    state: ledger.OffloadState
    bytes_freed: int = 0
    bytes_restored: int = 0
    files: int = 0
    note: str | None = None


@dataclass
class SweepReport:
    reoffloaded: list[str] = field(default_factory=list)
    extended: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    deferred_reason: str | None = None

    def wire(self) -> dict[str, object]:
        return {
            "reoffloaded": self.reoffloaded,
            "extended": self.extended,
            "skipped": self.skipped,
            "deferred_reason": self.deferred_reason,
        }


# --- which files a track has here -------------------------------------------


@dataclass(frozen=True)
class _LocalFile:
    role: ledger.FileRole
    path: Path


def _audio_paths(conn: sqlite3.Connection, stable_id: str, primary: str | None) -> list[Path]:
    """Every regular, non-symlink file the deck could play for ``stable_id`` here."""
    candidates = state_locations.playable_candidate_paths(conn, {stable_id: primary})[stable_id]
    seen: set[Path] = set()
    out: list[Path] = []
    for candidate in candidates:
        path = Path(candidate.path)
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            out.append(path)
    return out


def _stem_bundle_dir(ctx: OffloadContext, stable_id: str) -> Path:
    return ctx.stems_dir / stable_id


def _stem_paths(ctx: OffloadContext, stable_id: str) -> list[Path]:
    bundle = _stem_bundle_dir(ctx, stable_id)
    if bundle.is_symlink() or not bundle.is_dir():
        return []
    return sorted(p for p in bundle.rglob("*") if p.is_file() and not p.is_symlink())


def local_files(ctx: OffloadContext, stable_id: str) -> list[_LocalFile]:
    with state_db.connect_ro(ctx.state_db) as conn:
        audio = _audio_paths(conn, stable_id, ctx.primary_path(stable_id))
    return [_LocalFile("audio", p) for p in audio] + [
        _LocalFile("stem", p) for p in _stem_paths(ctx, stable_id)
    ]


# --- guards -----------------------------------------------------------------


def _refuse_if_busy(ctx: OffloadContext, stable_id: str) -> None:
    if stable_id in ctx.protected_ids():
        raise OffloadRefused("TRACK_ON_DECK", f"{stable_id} is loaded on a deck; not offloading it")
    reason = ctx.gate_reason()
    if reason is not None:
        raise OffloadRefused("OFFLOAD_DEFERRED", f"cloud offload deferred: {reason}")


class _TrackProgress:
    """Per-track bytes into ``transfer_status`` (rows) and the operation callback."""

    def __init__(self, ctx: OffloadContext, stable_id: str, direction: str, total: int) -> None:
        self._ctx, self._sid, self._total = ctx, stable_id, total
        self._token = transfer_status.begin_transfer(stable_id, direction, bytes_total=total)  # type: ignore[arg-type]
        self.done = 0
        ctx.on_progress(stable_id, 0, total)

    def advance_to(self, done: int) -> None:
        self.done = max(self.done, min(done, self._total))
        transfer_status.update_transfer(self._sid, self._token, self.done)
        self._ctx.on_progress(self._sid, self.done, self._total)

    def close(self) -> None:
        transfer_status.clear_transfer(self._sid, self._token)


# --- offload ----------------------------------------------------------------


def _upload_and_verify(
    ctx: OffloadContext, conn: sqlite3.Connection, stable_id: str, item: _LocalFile,
    progress: _TrackProgress,
) -> LocalDigest:
    base = progress.done
    digest = hash_local_file(item.path, cancelled=ctx.cancelled)
    known = {f.local_path: f for f in ledger.files_for(conn, stable_id)}.get(item.path)
    ledger.record_file(
        conn, stable_id=stable_id, role=item.role, local_path=item.path,
        sha256=digest.sha256, size_bytes=digest.size_bytes,
        object_key=asset_object_key(digest.sha256),
    )
    if ctx.cancelled():
        raise OffloadCancelled(f"cancelled before uploading {item.path}")
    if known is None or known.sha256 != digest.sha256 or known.verified_at is None:
        pushed = push_asset(
            ctx.cfg, ctx.assets, item.path,
            on_progress=lambda sent, _total: progress.advance_to(base + sent),
        )
        if pushed.content_hash != digest.sha256:
            raise OffloadVerifyError(f"{item.path} changed while it was uploading")
    how = verify_remote_copy(ctx.cfg, ctx.assets, digest)
    ledger.mark_verified(conn, stable_id, item.path, how, ctx.now())
    progress.advance_to(base + digest.size_bytes)
    return digest


def _publish_stem_entry(ctx: OffloadContext, stable_id: str, digests: list[LocalDigest]) -> None:
    """Index ONLY verified parts, in R2 first and then in the local cache."""
    bundle = _stem_bundle_dir(ctx, stable_id)
    entry = {
        d.path.relative_to(bundle).as_posix(): d.sha256
        for d in digests
        if stem_index.is_allowed_stem_filename(d.path.relative_to(bundle).as_posix())
    }
    if not entry:
        return
    stem_index.publish_index(ctx.cfg, ctx.index_s3, {stable_id: entry})
    cached = dict(stem_index.load_cached_index(ctx.data_dir))
    cached[stable_id] = entry
    stem_index.save_cached_index(ctx.data_dir, cached)


def _record_remote_rows(ctx: OffloadContext, stable_id: str, audio: list[LocalDigest]) -> None:
    with state_db.connect_rw(ctx.state_db, apply_schema=False) as conn:
        machine_id = sync_stamp.ensure_local_machine(conn)
        now = sync_stamp.canonical_now()
        with sync_stamp.stamped_transaction(conn):
            for digest in audio:
                _upsert_remote_location(
                    conn, stable_id=stable_id, object_key=asset_object_key(digest.sha256),
                    content_hash=digest.sha256, machine_id=machine_id, now=now,
                )


def _mark_audio_unavailable(ctx: OffloadContext, stable_id: str, paths: list[Path]) -> None:
    with state_db.connect_rw(ctx.state_db, apply_schema=False) as conn:
        machine_id = sync_stamp.ensure_local_machine(conn)
        now = sync_stamp.canonical_now()
        with sync_stamp.stamped_transaction(conn):
            for path in paths:
                _mark_local_unavailable(conn, stable_id, path, machine_id, now)


def _claim_stems(
    ctx: OffloadContext, stable_id: str, scanned: stem_bundles.LocalBundle, stem_digests: list[LocalDigest],
) -> bool:
    """Remove the bundle through the stem cache's guarded claim, or refuse.

    ``claim_if_unchanged`` takes the bundle writer lock, re-checks the
    fingerprint taken BEFORE the upload, re-hashes every part against the
    verified digests, and re-asks the deck registry and hydrate pins under the
    eviction-claim lock, so a republish or recompression that replaced the
    bundle meanwhile is never deleted (Codex P1 on #8598). True: removed;
    False: the stem cache already evicted it (its parts are verified in R2).
    """
    verified = {stable_id: {d.path.relative_to(scanned.path).as_posix(): d.sha256 for d in stem_digests}}
    claimed = stem_cache_budget.claim_if_unchanged(
        scanned, ctx.data_dir, verified, live_protected=ctx.protected_ids
    )
    if claimed is None:
        raise OffloadRefused(
            "STEMS_CHANGED", f"stems for {stable_id} were rewritten, opened or pinned since they were verified; kept"
        )
    return claimed


def _claim_and_delete_audio(digest: LocalDigest) -> None:
    """Delete one audio file only if it is STILL the verified bytes (Codex P1 on #8598).

    The file is first renamed to a claim name in the same directory: a writer
    that replaces the file afterwards (tag edits write a new file and rename it
    over the path) lands at the original path and is never touched. The claimed
    file is then hashed again, so an in-place write that happened before the
    rename is caught; a mismatch puts the file back and refuses. If a writer
    recreated the path meanwhile, the verified old bytes are deleted (they are in
    R2) and the track is refused, so the new file is uploaded on the next pass.
    """
    claimed = digest.path.with_name(f"{digest.path.name}{DELETE_CLAIM_INFIX}{secrets.token_hex(4)}")
    os.rename(digest.path, claimed)
    try:
        again = hash_local_file(claimed)
    except BaseException:
        if not digest.path.exists():
            os.rename(claimed, digest.path)
        raise
    if again.sha256 != digest.sha256:
        if digest.path.exists():
            raise OffloadVerifyError(f"{digest.path} was rewritten twice during deletion; both copies kept, the old as {claimed}")
        os.rename(claimed, digest.path)
        raise OffloadVerifyError(f"{digest.path} changed after it was verified; kept")
    claimed.unlink()
    if digest.path.exists():
        raise OffloadVerifyError(f"{digest.path} was rewritten during deletion; the new file is kept and offloads next pass")


def _delete_verified(
    ctx: OffloadContext, conn: sqlite3.Connection, stable_id: str,
    digests: list[tuple[_LocalFile, LocalDigest]], scanned: stem_bundles.LocalBundle | None,
) -> int:
    if ctx.cancelled():
        raise OffloadCancelled(f"cancelled before deleting anything of {stable_id}")
    _refuse_if_busy(ctx, stable_id)
    changed = [d.path for _item, d in digests if not unchanged_since_hash(d)]
    if changed:
        raise OffloadVerifyError(f"changed since hashing, kept: {', '.join(map(str, changed))}")
    freed = 0
    # Stems first: a refused claim must leave every audio file in place too.
    stem_digests = [d for item, d in digests if item.role == "stem"]
    if stem_digests:
        if scanned is None:
            raise OffloadRefused("STEMS_CHANGED", f"stems for {stable_id} appeared after the scan; kept")
        claimed = _claim_stems(ctx, stable_id, scanned, stem_digests)
        for digest in stem_digests:
            ledger.mark_deleted(conn, stable_id, digest.path, ctx.now())
            freed += digest.size_bytes if claimed else 0
    try:
        for item, digest in digests:
            if item.role == "audio":
                _claim_and_delete_audio(digest)
                ledger.mark_deleted(conn, stable_id, item.path, ctx.now())
                freed += digest.size_bytes
    finally:
        # Every audio copy recorded as deleted, including one an earlier crashed
        # pass removed, reads unavailable here (Codex P2 on #8598).
        gone = [f.local_path for f in ledger.files_for(conn, stable_id) if f.role == "audio" and f.deleted_at]
        if gone:
            _mark_audio_unavailable(ctx, stable_id, gone)
    return freed


def offload_track(ctx: OffloadContext, stable_id: str) -> TrackOutcome:
    """Upload, verify, then delete every local file of ``stable_id`` (idempotent)."""
    with ledger.open_ledger(ctx.data_dir) as conn:
        row = ledger.get_track(conn, stable_id)
        if row is not None and row.state == "offloaded":
            return TrackOutcome(stable_id, "offloaded", note="already offloaded")
        if row is not None and row.state == "restoring":
            raise OffloadRefused("OFFLOAD_BUSY", f"{stable_id} is being restored")
        _refuse_if_busy(ctx, stable_id)
        items = local_files(ctx, stable_id)
        # A crash between unlink() and its ledger write leaves a verified file
        # gone but not recorded as deleted; its bytes are in R2, so record it
        # now or resume and restore would both miss it (Codex P1 on #8598).
        for record in ledger.files_for(conn, stable_id):
            if record.deleted_at is None and record.verified_at is not None and not record.local_path.exists():
                ledger.mark_deleted(conn, stable_id, record.local_path, ctx.now())
        recorded = ledger.files_for(conn, stable_id)
        if not items and recorded and all(f.deleted_at for f in recorded):
            # A crash after the last deletion and before the state write: finish it.
            _mark_audio_unavailable(ctx, stable_id, [f.local_path for f in recorded if f.role == "audio"])
            ledger.set_state(conn, stable_id, "offloaded", now=ctx.now(), error=None)
            return TrackOutcome(stable_id, "offloaded", note="resumed after a crash")
        if not items:
            raise OffloadRefused("NOTHING_LOCAL", f"no local files for {stable_id} on this machine")
        prior: ledger.OffloadState = "temporary" if row is not None and row.state == "temporary" else "local"
        total = sum(item.path.stat().st_size for item in items)
        ledger.set_state(conn, stable_id, "uploading", now=ctx.now(), error=None)
        has_stems = any(item.role == "stem" for item in items)
        # Fingerprinted before any upload, so the guarded claim sees a rewrite made meanwhile.
        scanned = stem_bundles.scan_bundle(_stem_bundle_dir(ctx, stable_id)) if has_stems else None
        progress = _TrackProgress(ctx, stable_id, "upload", total)
        try:
            digests = [(item, _upload_and_verify(ctx, conn, stable_id, item, progress)) for item in items]
            _publish_stem_entry(ctx, stable_id, [d for item, d in digests if item.role == "stem"])
            _record_remote_rows(ctx, stable_id, [d for item, d in digests if item.role == "audio"])
            freed = _delete_verified(ctx, conn, stable_id, digests, scanned)
        except Exception as exc:
            # Nothing past a verified upload has been deleted unless recorded; a
            # track with any recorded deletion stays "uploading" so resume ends it,
            # and one with none goes back to what it was (a temporary restore keeps
            # its expiry, so the next sweep tries again).
            deleted = any(f.deleted_at for f in ledger.files_for(conn, stable_id))
            state: ledger.OffloadState = "uploading" if deleted else prior
            ledger.set_state(conn, stable_id, state, now=ctx.now(), error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            progress.close()
        moment = ctx.now()
        ledger.set_state(
            conn, stable_id, "offloaded", now=moment, offloaded_at=ledger.iso(moment),
            restore_mode=None, expires_at=None, bytes_offloaded=total, error=None,
        )
    return TrackOutcome(stable_id, "offloaded", bytes_freed=freed, files=len(items))


# --- restore ----------------------------------------------------------------


def _mark_audio_available(ctx: OffloadContext, stable_id: str, paths: list[Path]) -> None:
    """The inverse of hydration's ``_mark_local_unavailable``, stamped and logged."""
    with state_db.connect_rw(ctx.state_db, apply_schema=False) as conn:
        machine_id = sync_stamp.ensure_local_machine(conn)
        now = sync_stamp.canonical_now()
        with sync_stamp.stamped_transaction(conn):
            for path in paths:
                normalized = state_locations.normalize_stored_text(str(path))
                rows = conn.execute(
                    "SELECT location_id FROM track_locations WHERE stable_id = ? AND machine_id = ? "
                    "AND kind = 'local' AND file_path = ?",
                    (stable_id, machine_id, normalized),
                ).fetchall()
                for (location_id,) in rows:
                    stamp = sync_stamp.stamp_and_log(conn, "track_locations", (str(location_id),), machine_id, now=now)
                    conn.execute(
                        "UPDATE track_locations SET available = 1, probed_at = ?, updated_at = ?, "
                        "origin_device_id = ? WHERE location_id = ?",
                        (stamp.updated_at, stamp.updated_at, stamp.origin_device_id, str(location_id)),
                    )


def _restore_stems(ctx: OffloadContext, stable_id: str, files: list[ledger.OffloadFile], step: Callable[[int], None]) -> int:
    """All parts into an in-flight dir, verified, then one rename into place."""
    bundle = _stem_bundle_dir(ctx, stable_id)
    staging = bundle.with_name(f"{stable_id}{stem_bundles.IN_FLIGHT_MARKER}{secrets.token_hex(4)}")
    written = 0
    try:
        for record in files:
            dest = staging / record.local_path.relative_to(bundle)
            written += download_verified(
                ctx.cfg, ctx.assets, record.sha256, dest, on_chunk=step, cancelled=ctx.cancelled
            ).bytes_written
        if bundle.exists():  # another path (deck hydrate) already brought it back
            shutil.rmtree(staging)
        else:
            os.rename(staging, bundle)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return written


def _restore_preflight(
    conn: sqlite3.Connection,
    stable_id: str,
    mode: ledger.RestoreMode,
    moment: datetime,
    expiry: str,
) -> TrackOutcome | None:
    """Return an outcome when restore stops early; ``None`` means download the offloaded copy."""
    row = ledger.get_track(conn, stable_id)
    if row is None or row.state == "local":
        return TrackOutcome(stable_id, "local", note="not offloaded")
    if row.state == "uploading":
        raise OffloadRefused("OFFLOAD_BUSY", f"{stable_id} is being offloaded")
    if row.state == "temporary":
        if mode == "permanent":
            ledger.set_state(conn, stable_id, "local", now=moment, restore_mode="permanent", expires_at=None)
            ledger.forget_files(conn, stable_id)
            return TrackOutcome(stable_id, "local", note="kept permanently")
        ledger.set_state(conn, stable_id, "temporary", now=moment, expires_at=expiry)
        return TrackOutcome(stable_id, "temporary", note="expiry extended")
    return None


def _restore_download_pending(
    ctx: OffloadContext,
    conn: sqlite3.Connection,
    stable_id: str,
    pending: list[ledger.OffloadFile],
    step: Callable[[int], None],
) -> tuple[int, int]:
    """Download audio and stems; return ``(bytes_restored, preserved_file_count)``."""
    restored = 0
    preserved = 0
    audio = [f for f in pending if f.role == "audio"]
    for record in audio:
        result = download_verified(
            ctx.cfg, ctx.assets, record.sha256, record.local_path, on_chunk=step, cancelled=ctx.cancelled
        )
        if result.bytes_written == 0:
            step(record.size_bytes)
        if result.outcome == "preserved":
            preserved += 1
        # Available as soon as it is back, so a later failure cannot leave
        # a restored copy reading as missing (Codex P2 on #8598).
        _mark_audio_available(ctx, stable_id, [record.local_path])
        ledger.mark_restored(conn, stable_id, record.local_path)
        restored += result.bytes_written or record.size_bytes
    stems = [f for f in pending if f.role == "stem"]
    if stems:
        restored += _restore_stems(ctx, stable_id, stems, step)
        for record in stems:
            ledger.mark_restored(conn, stable_id, record.local_path)
    return restored, preserved


def _finalize_restore(
    conn: sqlite3.Connection,
    stable_id: str,
    mode: ledger.RestoreMode,
    pending: list[ledger.OffloadFile],
    restored: int,
    note: str | None,
    done: datetime,
) -> TrackOutcome:
    if mode == "permanent":
        ledger.set_state(conn, stable_id, "local", now=done, restored_at=ledger.iso(done), expires_at=None, error=None)
        ledger.forget_files(conn, stable_id)
        return TrackOutcome(stable_id, "local", bytes_restored=restored, files=len(pending), note=note)
    ledger.set_state(
        conn, stable_id, "temporary", now=done, restored_at=ledger.iso(done),
        expires_at=ledger.iso(done + timedelta(hours=TEMPORARY_RESTORE_HOURS)), error=None,
    )
    return TrackOutcome(stable_id, "temporary", bytes_restored=restored, files=len(pending), note=note)


def restore_track(ctx: OffloadContext, stable_id: str, mode: ledger.RestoreMode) -> TrackOutcome:
    """Bring ``stable_id``'s files back; ``temporary`` re-offloads after 24 h."""
    if mode not in ledger.RESTORE_MODES:
        raise OffloadRefused("BAD_RESTORE_MODE", f"mode must be one of {ledger.RESTORE_MODES}")
    moment = ctx.now()
    expiry = ledger.iso(moment + timedelta(hours=TEMPORARY_RESTORE_HOURS))
    with ledger.open_ledger(ctx.data_dir) as conn:
        early = _restore_preflight(conn, stable_id, mode, moment, expiry)
        if early is not None:
            return early
        pending = [f for f in ledger.files_for(conn, stable_id) if f.deleted_at is not None]
        total = sum(f.size_bytes for f in pending)
        ledger.set_state(conn, stable_id, "restoring", now=moment, restore_mode=mode, error=None)
        progress = _TrackProgress(ctx, stable_id, "download", total)

        def step(n: int) -> None:
            progress.advance_to(progress.done + n)

        try:
            restored, preserved = _restore_download_pending(ctx, conn, stable_id, pending, step)
        except BaseException as exc:
            ledger.set_state(conn, stable_id, "offloaded", now=ctx.now(), error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            progress.close()
        note = "kept newer local file" if preserved > 0 else None
        return _finalize_restore(conn, stable_id, mode, pending, restored, note, ctx.now())


# --- sweep and resume ----------------------------------------------------------


def sweep(ctx: OffloadContext, *, dry_run: bool = False) -> SweepReport:
    """Re-offload expired temporary restores; never one on a deck, never in a gig."""
    report = SweepReport()
    moment = ctx.now()
    protected = ctx.protected_ids()
    with ledger.open_ledger(ctx.data_dir) as conn:
        temporary = ledger.tracks_in_state(conn, "temporary")
        for row in temporary:
            if row.stable_id in protected:
                report.extended.append(row.stable_id)
                if not dry_run:
                    ledger.set_state(
                        conn, row.stable_id, "temporary", now=moment, last_played_at=ledger.iso(moment),
                        expires_at=ledger.iso(moment + timedelta(hours=TEMPORARY_RESTORE_HOURS)),
                    )
    report.deferred_reason = ctx.gate_reason()
    if report.deferred_reason is not None:
        return report
    for row in temporary:
        if row.stable_id in protected:
            continue
        if row.expires_at is None or ledger.parse_iso(row.expires_at) > moment:
            continue
        if dry_run:
            report.reoffloaded.append(row.stable_id)
            continue
        try:
            offload_track(ctx, row.stable_id)
            report.reoffloaded.append(row.stable_id)
        except Exception as exc:  # one track's failure never stops the round (Sentry on #8598)
            log.exception("cloud-offload: sweep could not re-offload %s", row.stable_id)
            report.skipped[row.stable_id] = f"{type(exc).__name__}: {exc}"
    return report


def interrupted(data_dir: Path) -> list[ledger.TrackOffload]:
    """Tracks a crash or restart left mid-operation, for the engine to resume."""
    with ledger.open_ledger(data_dir) as conn:
        return ledger.tracks_in_state(conn, "uploading", "restoring")


def offload_states(data_dir: Path, stable_ids: list[str]) -> dict[str, ledger.TrackOffload]:
    """Bulk read for rows and the audio route; a missing ledger is "nothing offloaded"."""
    if not ledger.ledger_path(data_dir).is_file():
        return {}
    with ledger.open_ledger(data_dir) as conn:
        return ledger.get_tracks(conn, stable_ids)


__all__ = [
    "SWEEP_INTERVAL_S",
    "TEMPORARY_RESTORE_HOURS",
    "OffloadContext",
    "OffloadRefused",
    "SweepReport",
    "TrackOutcome",
    "interrupted",
    "local_files",
    "offload_states",
    "offload_track",
    "restore_track",
    "sweep",
]
