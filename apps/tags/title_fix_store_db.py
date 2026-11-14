"""ProposalStore: the title-fix sidecar sqlite wrapper (split from title_fix_store)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Literal

from ._helpers import chunked
from .title_fix import Track, Verdict
from .title_fix_rows import (
    BatchRow,
    ProposalFilter,
    ProposalRow,
    ScanRow,
    _batch_from_row,
    _proposal_from_row,
    _scan_from_row,
)
from .title_fix_store_config import StorePaths, _atomic_write
from .title_fix_store_schema import (
    SCHEMA,
    _RECOVER_AUTO_BATCH,
    _add_missing_columns,
    _notes_with_producer,
)
from .title_fix_store_types import (
    CFG,
    BatchReleasedError,
    ConfigError,
    FenceExitError,
    IdentifyItem,
    ProposalState,
    ScanLeaseLostError,
    StoreBusyError,
    UndoIntentLostError,
    _utcnow,
    identify_notes,
)


class ProposalStore:
    """Thin sqlite wrapper. One connection per call: the daemon and the CLI may share the file."""

    def __init__(self, paths: StorePaths) -> None:
        self.paths = paths
        with self._conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > CFG.SCHEMA_VERSION:
                raise ConfigError(
                    f"{self.paths.store_db.name} is schema v{version}, newer than this build's v{CFG.SCHEMA_VERSION}; "
                    "refusing to open (or downgrade) it"
                )
            if version == CFG.SCHEMA_VERSION:
                return  # already initialised: a status read must not need the write lock a live apply is holding
            conn.executescript(SCHEMA)
            _add_missing_columns(conn)
            # A store written by the earlier, non-atomic scan start may hold several running rows: keep the newest
            # as the lease holder and fail the rest, or the unique index below cannot be created.
            conn.execute(
                "UPDATE scans SET state = 'failed', finished_at = ?, error = 'superseded duplicate running scan' "
                "WHERE state = 'running' AND scan_id NOT IN "
                "(SELECT scan_id FROM scans WHERE state = 'running' ORDER BY started_at DESC LIMIT 1)",
                (_utcnow(),),
            )
            # The database-level singleton: two engine processes cannot both hold a running scan.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS scans_single_running ON scans(state) WHERE state = 'running'"
            )
            conn.execute(f"PRAGMA user_version = {int(CFG.SCHEMA_VERSION)}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self.paths.store_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.paths.store_db, timeout=CFG.BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- scans
    def create_scan(self, requested: int, spend_cap_usd: float) -> ScanRow:
        scan_id = uuid.uuid4().hex
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO scans(scan_id, started_at, state, requested, spend_cap_usd, heartbeat_at) "
                "VALUES (?,?,?,?,?,?)",
                (scan_id, now, "running", requested, spend_cap_usd, now),
            )
        return self.get_scan(scan_id)

    def expire_stale_scans(self, *, stale_before: str) -> int:
        """Fail 'running' scans whose heartbeat is older than ``stale_before`` (their engine exited); a read can
        call this so a dead scan never leaves a client polling it forever."""
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE scans SET state = 'failed', finished_at = ?, error = 'scan process exited before finishing', "
                f"{_RECOVER_AUTO_BATCH} WHERE state = 'running' AND heartbeat_at < ?",
                (_utcnow(), stale_before),
            )
        return cur.rowcount

    def begin_scan(
        self,
        requested: int,
        spend_cap_usd: float,
        *,
        auto_apply_policy: str,
        stale_before: str,
    ) -> ScanRow | None:
        """The scan lease: fail scans whose heartbeat is older than ``stale_before``, then start one unless a
        live scan holds it. One write transaction, so two engine processes cannot both win; the unique index
        is the backstop. Returns None when a live scan owns the lease."""
        scan_id = uuid.uuid4().hex
        now = _utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE scans SET state = 'failed', finished_at = ?, error = 'scan process exited before finishing', "
                f"{_RECOVER_AUTO_BATCH} WHERE state = 'running' AND heartbeat_at < ?",
                (now, stale_before),
            )
            if conn.execute("SELECT 1 FROM scans WHERE state = 'running'").fetchone() is not None:
                return None
            conn.execute(
                "INSERT INTO scans(scan_id, started_at, state, requested, spend_cap_usd, heartbeat_at, auto_apply_policy) "
                "VALUES (?,?,?,?,?,?,?)",
                (scan_id, now, "running", requested, spend_cap_usd, now, auto_apply_policy),
            )
        return self.get_scan(scan_id)

    def update_scan(self, scan_id: str, **fields: Any) -> None:
        allowed = {"finished_at", "state", "processed", "cost_usd", "error", "auto_batch_id"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"update_scan fields must be a subset of {sorted(allowed)}")
        cols = ", ".join(f"{k} = ?" for k in (*fields, "heartbeat_at"))
        with self._conn() as conn:  # every progress write is also the owning worker's proof of life
            cur = conn.execute(
                f"UPDATE scans SET {cols} WHERE scan_id = ? AND state = 'running'",  # fenced: only the lease holder
                (*fields.values(), _utcnow(), scan_id),
            )
            if cur.rowcount == 0:
                raise ScanLeaseLostError(f"scan {scan_id} no longer holds the scan lease")

    def beat_scan(self, scan_id: str) -> None:
        """Proof of life from a long model call; fenced like update_scan, so a taken-over lease raises."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (_utcnow(), scan_id)
            )
            if cur.rowcount == 0:
                raise ScanLeaseLostError(f"scan {scan_id} no longer holds the scan lease")

    def set_heartbeat(self, scan_id: str, heartbeat_at: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE scans SET heartbeat_at = ? WHERE scan_id = ?", (heartbeat_at, scan_id))

    def get_scan(self, scan_id: str) -> ScanRow:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
        if row is None:
            raise KeyError(f"scan not found: {scan_id}")
        return _scan_from_row(row)

    def list_scans(self, limit: int = 20) -> list[ScanRow]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_scan_from_row(r) for r in rows]

    def running_scan(self) -> ScanRow | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM scans WHERE state = 'running' ORDER BY started_at DESC").fetchone()
        return _scan_from_row(row) if row else None

    # -- proposals
    def add_verdict(self, scan_id: str, track: Track, original_artists: list[str], verdict: Verdict) -> ProposalRow:
        proposal_id = uuid.uuid4().hex
        changed = verdict.title != track.title or verdict.artist != track.artist or bool(verdict.tags)
        state = "pending" if changed and verdict.status != "unknown" else "noop"
        with self._conn() as conn:
            if (
                conn.execute(  # first write of the transaction: proves the lease, so a fenced worker stores nothing
                    "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (_utcnow(), scan_id)
                ).rowcount
                == 0
            ):
                raise ScanLeaseLostError(f"scan {scan_id} no longer holds the scan lease")
            if (
                verdict.status != "unknown"
            ):  # a transient failure must not discard the earlier, still usable paid result
                conn.execute(
                    "UPDATE proposals SET state = 'superseded', decided_at = ? WHERE stable_id = ? AND state = 'pending'",
                    (_utcnow(), track.id),
                )
            conn.execute(
                "INSERT INTO proposals(proposal_id, scan_id, stable_id, created_at, original_title, original_artist, "
                "original_artists_json, proposed_title, proposed_artist, original_artist_credit, tags_json, "
                "fixer_status, source, confidence, jev_kind, notes_json, error, cost_usd, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    scan_id,
                    track.id,
                    _utcnow(),
                    track.title,
                    track.artist,
                    json.dumps(original_artists, ensure_ascii=False),
                    verdict.title,
                    verdict.artist,
                    verdict.original_artist,
                    json.dumps(list(verdict.tags)),
                    verdict.status,
                    verdict.source,
                    verdict.jev_p_ok,
                    verdict.jev_kind,
                    json.dumps(_notes_with_producer(verdict), ensure_ascii=False),
                    verdict.error,
                    verdict.cost_usd,
                    state,
                ),
            )
        return self.get_proposals([proposal_id])[0]

    def add_identify_proposals(self, items: Sequence[IdentifyItem]) -> list[ProposalRow]:
        if not items:
            return []
        scan_id = uuid.uuid4().hex
        now = _utcnow()
        proposal_ids: list[str] = []
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO scans(scan_id, started_at, finished_at, state, requested, processed, "
                "spend_cap_usd, cost_usd, heartbeat_at, auto_apply_policy) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (scan_id, now, now, "done", len(items), len(items), 0.0, 0.0, now, "off"),
            )
            for item in items:
                proposal_id = uuid.uuid4().hex
                fixer_status = "fixed" if item.proposal.status == "confident" else "review"
                best = item.proposal.best
                changed = best.title != item.original_title or best.artist != item.original_artist
                state = "pending" if changed else "noop"
                conn.execute(
                    "UPDATE proposals SET state = 'superseded', decided_at = ? WHERE stable_id = ? AND state = 'pending'",
                    (now, item.stable_id),
                )
                conn.execute(
                    "INSERT INTO proposals(proposal_id, scan_id, stable_id, created_at, original_title, original_artist, "
                    "original_artists_json, proposed_title, proposed_artist, original_artist_credit, tags_json, "
                    "fixer_status, source, confidence, jev_kind, notes_json, error, cost_usd, state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal_id,
                        scan_id,
                        item.stable_id,
                        now,
                        item.original_title,
                        item.original_artist,
                        json.dumps(item.original_artists, ensure_ascii=False),
                        best.title,
                        best.artist,
                        "",
                        "[]",
                        fixer_status,
                        "identify",
                        item.proposal.score,
                        "",
                        json.dumps(identify_notes(item.proposal), ensure_ascii=False),
                        "",
                        0.0,
                        state,
                    ),
                )
                proposal_ids.append(proposal_id)
        return self.get_proposals(proposal_ids)

    def get_proposals(self, proposal_ids: list[str]) -> list[ProposalRow]:
        if not proposal_ids:
            return []
        rows: list[sqlite3.Row] = []
        with self._conn() as conn:
            for chunk in chunked(proposal_ids, CFG.SQL_IN_CHUNK):  # one IN (...) must stay under the variable cap
                marks = ",".join("?" * len(chunk))
                rows.extend(conn.execute(f"SELECT * FROM proposals WHERE proposal_id IN ({marks})", chunk).fetchall())
        by_id = {r["proposal_id"]: _proposal_from_row(r) for r in rows}
        missing = [p for p in proposal_ids if p not in by_id]
        if missing:
            raise KeyError(f"proposals not found: {missing}")
        return [by_id[p] for p in proposal_ids]

    def list_proposals(self, flt: ProposalFilter) -> tuple[list[ProposalRow], int]:
        where, args = [], []
        for col, value in (
            ("state", flt.state),
            ("fixer_status", flt.fixer_status),
            ("source", flt.source),
            ("scan_id", flt.scan_id),
            ("batch_id", flt.batch_id),
        ):
            if value is not None:
                where.append(f"{col} = ?")
                args.append(value)
        if flt.tag is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(proposals.tags_json) WHERE json_each.value = ?)")
            args.append(flt.tag)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM proposals {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM proposals {clause} ORDER BY created_at DESC, proposal_id LIMIT ? OFFSET ?",
                (*args, flt.limit, flt.offset),
            ).fetchall()
        return [_proposal_from_row(r) for r in rows], total

    def counts_by_state(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM proposals GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def set_state(
        self,
        proposal_ids: list[str],
        state: ProposalState,
        *,
        batch_id: str | None = None,
        tags_added: dict[str, list[str]] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        now = _utcnow()
        with self._scoped(conn) as db:
            for pid in proposal_ids:
                if tags_added is not None:
                    db.execute(
                        "UPDATE proposals SET state = ?, decided_at = ?, batch_id = ?, tags_added_json = ? "
                        "WHERE proposal_id = ?",
                        (state, now, batch_id, json.dumps(tags_added.get(pid, [])), pid),
                    )
                else:
                    db.execute(
                        "UPDATE proposals SET state = ?, decided_at = ? WHERE proposal_id = ?", (state, now, pid)
                    )

    # -- batches
    def create_batch(self, kind: Literal["manual", "auto"], policy: str, proposal_count: int) -> BatchRow:
        batch_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO batches(batch_id, created_at, kind, policy, proposal_count) VALUES (?,?,?,?,?)",
                (batch_id, _utcnow(), kind, policy, proposal_count),
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> BatchRow:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"batch not found: {batch_id}")
        return _batch_from_row(row)

    def list_batches(self, limit: int = 20) -> list[BatchRow]:
        """The latest ``limit`` batches plus EVERY batch not yet undone, newest first: an old applied batch must stay
        discoverable (and so undoable) however many newer batches exist."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM batches WHERE undone_at IS NULL OR batch_id IN "
                "(SELECT batch_id FROM batches ORDER BY created_at DESC LIMIT ?) ORDER BY created_at DESC",
                (limit,),
            ).fetchall()
        return [_batch_from_row(r) for r in rows]

    def claim_pending(
        self,
        proposal_ids: list[str],
        batch_id: str,
        tags_added: dict[str, list[str]],
        *,
        fence_scan_id: str | None = None,
    ) -> list[str]:
        """Atomically move each still-pending proposal into the batch; returns the ids this call won.

        The conditional UPDATE is the claim: two concurrent applies of one proposal cannot both win it.
        ``fence_scan_id`` makes the claim also prove, in the same transaction, that this scan still holds the
        lease: a worker taken over while suspended raises ScanLeaseLostError and claims nothing.
        """
        now = _utcnow()
        claimed: list[str] = []
        with self._conn() as conn:
            if (
                fence_scan_id is not None
                and conn.execute(
                    "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'", (now, fence_scan_id)
                ).rowcount
                == 0
            ):
                raise ScanLeaseLostError(f"scan {fence_scan_id} no longer holds the scan lease")
            for pid in proposal_ids:
                cur = conn.execute(
                    "UPDATE proposals SET state = 'applied', decided_at = ?, batch_id = ?, tags_added_json = ? "
                    "WHERE proposal_id = ? AND state = 'pending'",
                    (now, batch_id, json.dumps(tags_added.get(pid, [])), pid),
                )
                if cur.rowcount:
                    claimed.append(pid)
        return claimed

    def resolve_pending(self, proposal_ids: list[str], state: ProposalState) -> list[str]:
        """Move each still-pending proposal to ``state``; returns the ids this call won (an apply may have claimed one)."""
        now = _utcnow()
        won: list[str] = []
        with self._conn() as conn:
            for pid in proposal_ids:
                cur = conn.execute(
                    "UPDATE proposals SET state = ?, decided_at = ? WHERE proposal_id = ? AND state = 'pending'",
                    (state, now, pid),
                )
                if cur.rowcount:
                    won.append(pid)
        return won

    def reject_pending(self, proposal_ids: list[str]) -> list[str]:
        return self.resolve_pending(proposal_ids, "rejected")

    @contextmanager
    def _hold_write_lock(self, prove_ownership: Callable[[sqlite3.Connection], None]) -> Iterator[sqlite3.Connection]:
        """Take the store's write lock, prove ownership inside it, and hold it until the caller's block returns.

        A scan takeover, a batch or undo reconcile and a release all write this file, so while the lock is held
        none of them can run: the writer's ownership stays true through the state.db commit, not just up to it.
        """
        self.paths.store_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.paths.store_db, timeout=CFG.BUSY_TIMEOUT_S, isolation_level=None)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise StoreBusyError(f"the title-fix store is busy: {exc}") from exc
            prove_ownership(conn)
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                raise FenceExitError(f"the title-fix store could not commit its fence after the write: {exc}") from exc
        finally:
            conn.close()

    def write_fence(self, batch_id: str, fence_scan_id: str | None) -> AbstractContextManager[sqlite3.Connection]:
        """The apply's fence: the batch must still be the uncommitted batch the writer claimed under, and an
        auto-apply's scan must still hold the lease; either failure raises before anything is written."""

        def prove(conn: sqlite3.Connection) -> None:
            if (
                conn.execute(
                    "SELECT 1 FROM batches WHERE batch_id = ? AND committed_at IS NULL AND undone_at IS NULL",
                    (batch_id,),
                ).fetchone()
                is None
            ):
                raise BatchReleasedError(f"batch {batch_id} is no longer open for writing")
            if (
                fence_scan_id is not None
                and conn.execute(
                    "UPDATE scans SET heartbeat_at = ? WHERE scan_id = ? AND state = 'running'",
                    (_utcnow(), fence_scan_id),
                ).rowcount
                == 0
            ):
                raise ScanLeaseLostError(f"scan {fence_scan_id} no longer holds the scan lease")

        return self._hold_write_lock(prove)

    def undo_fence(self, batch_id: str, token: str) -> AbstractContextManager[sqlite3.Connection]:
        """The undo's fence: the intent recorded under ``token`` must still be this writer's, not cleared and
        re-taken by an undo that judged it abandoned."""

        def prove(conn: sqlite3.Connection) -> None:
            if (
                conn.execute(
                    "SELECT 1 FROM batches WHERE batch_id = ? AND undo_started_at = ? AND undone_at IS NULL",
                    (batch_id, token),
                ).fetchone()
                is None
            ):
                raise UndoIntentLostError(f"the undo of batch {batch_id} no longer owns its intent")

        return self._hold_write_lock(prove)

    def exclusive(self) -> AbstractContextManager[sqlite3.Connection]:
        """The store's write lock, held for the caller's whole decision AND action. A reconcile must read the tracks
        and act on what it read under this lock, or a live writer's commit can land between the two."""
        return self._hold_write_lock(lambda _conn: None)

    @contextmanager
    def _scoped(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """The held connection when the caller owns the write lock, else a fresh one."""
        if conn is not None:
            yield conn
        else:
            with self._conn() as own:
                yield own

    def mark_batch_committed(
        self,
        batch_id: str,
        *,
        applied_state: dict[str, dict[str, Any]] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """The batch's track write landed: from here its rows are real changes and Undo may restore them.
        applied_state (proposal_id -> {"etag", "tags"} as of the write) lets Undo see ANY later row or tag edit."""
        state_json = None if not applied_state else json.dumps(applied_state, sort_keys=True)
        with self._scoped(conn) as db:
            db.execute(
                "UPDATE batches SET committed_at = ?, applied_state_json = ? WHERE batch_id = ?",
                (_utcnow(), state_json, batch_id),
            )

    def release_batch(self, batch_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Return only the rows this batch claimed; another batch's rows are never touched.

        A claimed row could not be superseded by a newer scan while it was applied, so one that now has a newer
        pending proposal for its track is released as superseded: at most one pending row per track.
        """
        now = _utcnow()
        with self._scoped(conn) as db:
            db.execute(
                "UPDATE proposals SET state = 'superseded', decided_at = ?, batch_id = NULL, tags_added_json = '[]' "
                "WHERE batch_id = ? AND state = 'applied' AND EXISTS ("
                "SELECT 1 FROM proposals newer WHERE newer.stable_id = proposals.stable_id AND newer.state = 'pending')",
                (now, batch_id),
            )
            db.execute(
                "UPDATE proposals SET state = 'pending', batch_id = NULL, tags_added_json = '[]' "
                "WHERE batch_id = ? AND state = 'applied'",
                (batch_id,),
            )

    def set_batch_count(self, batch_id: str, proposal_count: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE batches SET proposal_count = ? WHERE batch_id = ?", (proposal_count, batch_id))

    def discard_batch(self, batch_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Drop a batch whose track write failed: nothing of it landed, so no undo record may exist."""
        with self._scoped(conn) as db:
            db.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))

    def mark_undo_started(self, batch_id: str, target_proposal_ids: list[str]) -> str | None:
        """Record the intent to undo, and exactly which proposals it restores, before any track is restored; returns
        its token, or None when another undo already holds it. The token is how the writer later proves the intent
        is still its own; the targets are how a crashed undo is later judged without counting a skipped conflict."""
        token = _utcnow() + "#" + uuid.uuid4().hex[:8]
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE batches SET undo_started_at = ?, undo_targets_json = ? WHERE batch_id = ? "
                "AND undone_at IS NULL AND undo_started_at IS NULL",
                (token, json.dumps(target_proposal_ids), batch_id),
            )
        return token if cur.rowcount == 1 else None

    def finish_undo(
        self,
        batch_id: str,
        *,
        complete: bool,
        token: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Clear the intent; a complete undo also stamps the batch undone. With a ``token`` only the intent's
        owner may finish it: an undo that lost its intent changes nothing."""
        with self._scoped(conn) as db:
            db.execute(
                "UPDATE batches SET undo_started_at = NULL, undo_targets_json = NULL, undone_at = CASE WHEN ? THEN ? ELSE undone_at END "
                "WHERE batch_id = ? AND (? IS NULL OR undo_started_at = ?)",
                (complete, _utcnow(), batch_id, token, token),
            )

    def mark_batch_undone(self, batch_id: str) -> None:
        self.finish_undo(batch_id, complete=True)
