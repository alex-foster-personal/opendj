"""Scan, apply, reject and undo for the "fix broken track metadata" admin feature.

Mini-PRD
--------
✔︎ T1 scan reads tracks READ-ONLY from state.db, runs ``apps.tags.title_fix.fix_tracks`` in
   chunks, and stores every verdict as a proposal. It never writes a track.
   [if] a scan finishes then every track row in state.db is byte-identical to before [else ⛔️]
   [if] the cumulative cost reaches the spend cap then the scan stops with state capped [else ⛔️]
   [if] no OpenRouter key resolves then TitleFixError("title_fix_key_missing") before any work [else ⛔️]
✔︎ T2 apply writes ONLY selected pending proposals, through ``StateBackend.update_tracks`` (the
   single CAS write chokepoint), with source ``open-dj-tool``; a row whose title/artist moved
   since the scan is marked stale and skipped, never overwritten.
   [if] the track's title changed after the scan then the proposal is stale and untouched [else ⛔️]
   [if] a tag family is disabled in config then its tags are not added [else ⛔️]
✔︎ T3 undo restores the original title, artists list and removes exactly the tags the batch
   added, through the same chokepoint; a row edited since the apply is a conflict, refused
   unless the caller opts to skip conflicts.
   [if] a batch is undone then title and artists read back as the originals [else ⛔️]
   [if] a batch is undone twice then TitleFixError("title_fix_batch_already_undone") [else ⛔️]
✔︎ T4 auto-apply runs after a scan only when the policy is not "off", and only on rows
   ``auto_apply_eligible`` admits (status fixed; safe_tier also requires source safe-tier).
   [if] policy is off then a finished scan applies nothing [else ⛔️]

The resolved key is passed to ``fix_tracks`` as an argument (round 7a): a scan never writes the
process environment, so nothing else in the engine can see or use the key while it runs.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

from apps.shared.state import db as state_db
from apps.tags.http import (
    STORE_CFG,
    FenceExitError,
    ProposalFilter,
    ScanLeaseLostError,
    ScanRow,
    TitleFixConfig,
    auto_apply_eligible,
    chunked,
    load_config,
    resolve_key,
    title_fix,
)

from .backend import (
    CommittedReadbackError,
)
from .etag import compute_etag  # noqa: F401  # re-exported: the tests drive etags through the service
from .title_fix_apply import (  # noqa: F401  # re-exported: the public surface and the names the tests drive
    _plan_row_apply,
    _raise_committed_readback,
    _write_apply_batch,
    _write_in_fence,
    _write_tracks_or_release,
    apply_proposals,
    reject_proposals,
)
from .title_fix_core import (  # noqa: F401  # _get_or_404 is re-exported for the tests
    _ALL_ROWS,
    CFG,
    Ctx,
    Outcome,
    TitleFixError,
    _artists_or_refuse,
    _get_or_404,
    _is_store_contention,
    _now,
    _when_store_free,
)
from .title_fix_undo import (  # noqa: F401  # re-exported: the public surface and the names the tests drive
    _check_undoable,
    _moved_since,
    _reconcile_stalled_batch,
    _reconcile_stalled_undo,
    _restore_tracks_or_release,
    undo_batch,
)

_SCAN_LOCK = threading.Lock()


def read_scan_tracks(
    state_db_path: Path, stable_ids: list[str] | None, limit: int | None
) -> list[tuple[title_fix.Track, list[str]]]:
    """(fixer Track, original artists list) per live track with a title. Read-only."""
    if not Path(state_db_path).is_file():
        raise TitleFixError("title_fix_state_db_missing", f"no state.db at {state_db_path}; ingest a library first")
    conn = state_db.open_ro(Path(state_db_path))
    try:
        rows = _scan_rows(conn, stable_ids, limit)
    finally:
        conn.close()
    if stable_ids is not None:
        missing = sorted(set(stable_ids) - {r[0] for r in rows})
        if missing:
            raise TitleFixError("title_fix_tracks_not_found", f"tracks not found: {missing[:20]}", 404)
    return [_scan_track(*row) for row in rows]  # an empty title is a review row for the fixer, never skipped


def _scan_rows(conn: sqlite3.Connection, stable_ids: list[str] | None, limit: int | None) -> list[sqlite3.Row]:
    """The scan's tracks rows. A list of ids is read in chunks below SQLite's variable cap, then put back in the order
    and under the limit a single query would have given."""
    if stable_ids is None:
        sql, args = _scan_query(None, limit)
        return conn.execute(sql, args).fetchall()
    rows = [
        row
        for chunk in chunked(list(dict.fromkeys(stable_ids)), STORE_CFG.SQL_IN_CHUNK)
        for row in conn.execute(*_scan_query(chunk, None)).fetchall()
    ]
    rows.sort(key=lambda row: row[0])
    return rows if limit is None else rows[:limit]


def _scan_query(stable_ids: list[str] | None, limit: int | None) -> tuple[str, list[object]]:
    sql = "SELECT stable_id, title, artists_json, album, duration_ms, file_path FROM tracks WHERE deleted_at IS NULL"
    args: list[object] = []
    if stable_ids is not None:
        sql += f" AND stable_id IN ({','.join('?' * len(stable_ids))})"
        args.extend(stable_ids)
    sql += " ORDER BY stable_id"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return sql, args


def _scan_track(
    stable_id: str,
    title: str,
    artists_json: str | None,
    album: str | None,
    duration_ms: int | None,
    file_path: str | None,
) -> tuple[title_fix.Track, list[str]]:
    """One tracks row as (fixer Track, original artists list); the fixer sees the artists joined by ", "."""
    artists = _artists_or_refuse(stable_id, artists_json)
    # label / year / bitrate / bit depth / sample rate stay None (unknown): state.db stores none of them, and the
    # project rules read an unknown as "cannot say", never as "absent", so they stay silent rather than guess.
    path = PureWindowsPath(
        file_path or ""
    )  # splits both / and \\ on any host, so a synced Windows path parses the same everywhere
    track = title_fix.Track(
        id=stable_id,
        title=title or "",
        artist=", ".join(a for a in artists if a),
        filename=path.name,
        folder=path.parent.as_posix() if file_path else "",
        album=album,
        length_s=None if duration_ms is None else duration_ms / 1000,
    )
    return track, artists


def expire_stale_scans(ctx: Ctx) -> int:
    """Fail scans whose engine stopped heartbeating, so a status read reports the truth and a retry is allowed.
    A store held by a live writer (a long apply) is contention, not a dead scan: the expiry is skipped this read
    and the status poll carries on; any other database error still raises."""
    try:
        return ctx.store.expire_stale_scans(stale_before=_stale_scan_cutoff())
    except sqlite3.OperationalError as exc:
        if not _is_store_contention(exc):
            raise
        return 0


def _stale_scan_cutoff() -> str:
    return (datetime.now(UTC) - timedelta(seconds=CFG.SCAN_STALE_S)).isoformat()


def start_scan(ctx: Ctx, stable_ids: list[str] | None, limit: int | None, *, background: bool = True) -> ScanRow:
    if stable_ids is not None and limit is not None:  # the limit would silently drop requested ids
        raise TitleFixError("title_fix_scan_ids_and_limit", "pass stable_ids or limit, not both", 422)
    config = load_config(ctx.paths)
    if not config.enabled:
        raise TitleFixError("title_fix_disabled", "title fixing is disabled; enable it in the Title fixes config")
    key, _status = resolve_key(ctx.paths)
    if key is None:
        raise TitleFixError(
            "title_fix_key_missing",
            f"no OpenRouter key: save one in the Title fixes config or set {STORE_CFG.OPENROUTER_KEY_ENV} "
            "in the engine environment. Nothing is scanned without one.",
        )
    with _SCAN_LOCK:
        tracks = read_scan_tracks(ctx.paths.state_db, stable_ids, limit)
        scan = (
            _when_store_free(  # the cross-process lease; nothing has started yet, so waiting out a busy store is safe
                ctx.store.begin_scan,
                len(tracks),
                config.spend_cap_usd,
                auto_apply_policy=config.auto_apply_policy,
                stale_before=_stale_scan_cutoff(),
            )
        )
        if scan is None:
            running = ctx.store.running_scan()
            raise TitleFixError(
                "title_fix_scan_running", f"scan {running.scan_id if running else '?'} is still running"
            )
    if background:
        threading.Thread(
            target=_run_scan,
            args=(ctx, scan.scan_id, tracks, key, config),
            daemon=True,
            name=f"title-fix-scan-{scan.scan_id[:8]}",
        ).start()
        return scan
    _run_scan(ctx, scan.scan_id, tracks, key, config)
    return ctx.store.get_scan(scan.scan_id)


def _run_scan(
    ctx: Ctx,
    scan_id: str,
    tracks: list[tuple[title_fix.Track, list[str]]],
    key: str,
    config: TitleFixConfig,
) -> None:
    """``config`` is the one the scan started under; the cap enforced is the one recorded on the scan row."""
    budget = title_fix.SpendBudget(ctx.store.get_scan(scan_id).spend_cap_usd)  # one budget for the whole scan
    processed, state = 0, "done"
    try:
        for start in range(0, len(tracks), CFG.CHUNK):
            if budget.spent_usd >= budget.limit_usd:
                break  # state becomes capped below
            chunk = tracks[start : start + CFG.CHUNK]
            _when_store_free(
                ctx.store.update_scan, scan_id, processed=processed
            )  # heartbeat before the slow model calls
            with _heartbeating(ctx, scan_id):  # serialized probes can make one chunk outlast SCAN_STALE_S
                verdicts = title_fix.fix_tracks([t for t, _ in chunk], api_key=key, spend_budget=budget)
            cost = budget.spent_usd  # what OpenRouter charged, including paid responses that failed to parse
            scanned = [(row, v) for row, v in zip(chunk, verdicts, strict=True) if _was_scanned(v)]
            for (track, artists), verdict in scanned:  # a row the budget stopped before its call was never scanned
                _when_store_free(ctx.store.add_verdict, scan_id, track, artists, verdict)
            processed += len(scanned)
            _when_store_free(ctx.store.update_scan, scan_id, processed=processed, cost_usd=cost)
            if _stopped_by_cap(verdicts):
                state = "capped"
                break
        if budget.spent_usd >= budget.limit_usd:
            state = "capped"  # the last chunk's last answer can be the one that spends the cap
        auto_batch, auto_warning = None, None
        _when_store_free(
            ctx.store.update_scan, scan_id, processed=processed
        )  # still the lease holder before it applies anything
        if config.auto_apply_policy != "off":
            auto_batch, auto_warning = _auto_apply_batch_id(ctx, scan_id, config.auto_apply_policy, config.tag_families)
        _when_store_free(
            ctx.store.update_scan,
            scan_id,
            state=state,
            finished_at=_now(),
            auto_batch_id=auto_batch,
            error=auto_warning,
        )
    except ScanLeaseLostError:
        return  # another process took the lease over: its row and proposals are not this worker's to touch
    except Exception as exc:  # the scan row must not stay 'running' forever; the error is surfaced on it
        with contextlib.suppress(ScanLeaseLostError):  # a taken-over lease means the new holder owns the row
            _when_store_free(
                ctx.store.update_scan, scan_id, state="failed", finished_at=_now(), error=f"{type(exc).__name__}: {exc}"
            )
        raise


@contextlib.contextmanager
def _heartbeating(ctx: Ctx, scan_id: str) -> Iterator[None]:
    """Keep the scan lease alive while the model calls run. A beat that meets store contention (a long apply holding
    the sidecar lock) is skipped and the next one retries; a lost lease, or a beat failing for any other reason, is
    raised on exit, so the caller never writes the chunk's results under a lease it no longer holds."""
    stop = threading.Event()
    failures: list[BaseException] = []

    def beat() -> None:
        wait_s = CFG.SCAN_STALE_S / CFG.BEATS_PER_STALE_WINDOW
        while not stop.wait(wait_s):
            wait_s = CFG.SCAN_STALE_S / CFG.BEATS_PER_STALE_WINDOW
            try:
                ctx.store.beat_scan(scan_id)
            except sqlite3.OperationalError as exc:
                if not _is_store_contention(exc):
                    failures.append(exc)
                    return
                wait_s = (
                    CFG.SCAN_STALE_S / CFG.CONTENTION_RETRIES_PER_STALE_WINDOW
                )  # retry soon: the margin is shrinking
            except ScanLeaseLostError as exc:
                failures.append(exc)
                return

    thread = threading.Thread(target=beat, name=f"title-fix-heartbeat-{scan_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
    if failures:
        raise failures[0]


def _stopped_by_cap(verdicts: list[title_fix.Verdict]) -> bool:
    """Any row the spend budget stopped, whether before its first call (never scanned) or after paying a stage."""
    return any(title_fix.SPEND_CAP_ERROR in v.error for v in verdicts)


def _was_scanned(verdict: title_fix.Verdict) -> bool:
    """False only for a row the budget stopped before it made any call. A row that paid for a first stage and was
    stopped before its next one is a real, charged result and is kept, or a retry would pay for it again."""
    return not (title_fix.SPEND_CAP_ERROR in verdict.error and verdict.cost_usd == 0)


def _auto_apply_batch_id(
    ctx: Ctx,
    scan_id: str,
    policy: str,
    tag_families: tuple[str, ...],
) -> tuple[str | None, str | None]:
    """The undo batch an auto-apply recorded, and the warning if its write committed but could not be confirmed. A
    write that committed but whose result could not be re-read still changed tracks, so its batch is linked to the
    scan rather than the scan reading as having written nothing, and the scan row carries the warning."""
    try:
        outcome = _auto_apply(ctx, scan_id, policy, tag_families)
    except (CommittedReadbackError, FenceExitError) as exc:
        if exc.batch_id is None:
            raise
        return exc.batch_id, f"{type(exc).__name__}: {exc}"
    return outcome.batch_id, outcome.warning


def _auto_apply(ctx: Ctx, scan_id: str, policy: str, tag_families: tuple[str, ...]) -> Outcome:
    if ctx.write_gate is not None:
        ctx.write_gate()  # the request's own check ended when it returned; a long scan must ask the lock again
    rows, _total = ctx.store.list_proposals(ProposalFilter(state="pending", scan_id=scan_id, limit=_ALL_ROWS))
    eligible = [r.proposal_id for r in rows if auto_apply_eligible(r, policy)]
    return (
        apply_proposals(
            ctx,
            eligible,
            kind="auto",
            policy=policy,
            scan_id=scan_id,
            tag_families=tag_families,
        )
        if eligible
        else Outcome()
    )
