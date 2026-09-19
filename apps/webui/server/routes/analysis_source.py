"""Per-lane analysis source selection over HTTP (PARITY-02, agent parity).

``GET  /api/v1/analysis/source`` reports, per selection lane, the persisted
default, the in-memory dev toggle, and the resulting effective source.
``PUT  /api/v1/analysis/source`` sets either half.

Agent-native parity is the reason this exists as an endpoint rather than a
UI control with a module-level variable behind it: every option in the
top-left dropdown must be settable and readable by an agent without
touching the UI, and the path must appear in ``apps/webui/openapi.json``.

The toggle is PROCESS-LOCAL by design (it is in-memory, spec section 3),
so this endpoint is also the only correct writer for it. The CLI
(:mod:`apps.analysis.selection_cli`) is a client of this endpoint, never a
second writer: a CLI that mutated its own import of the module would leave
the running server on the old state and report success.

This router writes ONLY to ``analysis_source_default``. It never touches
``track_fields``.

-Claude
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from apps.analysis import selection as sel
from apps.analysis import store as analysis_store
from apps.shared.paths import STATE_DB

from ..backend import InMemoryBackend, StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/analysis", tags=["analysis"])


class LaneSourceOut(BaseModel):
    """One lane's persisted default, dev toggle, and resulting source."""

    default: str = Field(description="Persisted per-lane default source: rbx or own")
    toggle: str = Field(
        description="In-memory dev toggle: unset, rbx or own. Launches unset."
    )
    toggle_revision: int = Field(
        description=(
            "Monotonic counter bumped by every toggle write for this lane, "
            "launches at 0. A VALUE can repeat (own -> rbx -> own reads as "
            "'own' again); this never does, so a client that captures it "
            "after a write can pass it back as `expected_toggle_revision` "
            "to require that nothing has touched the toggle since, not "
            "merely that the value looks unchanged."
        )
    )
    effective: str = Field(
        description="The source actually read: the toggle unless it is unset"
    )


class AnalysisSourceOut(BaseModel):
    lanes: dict[str, LaneSourceOut]
    serving: list[str] = Field(
        description=(
            "Selection lanes that currently have a serving own implementation "
            "on this daemon. Lanes absent from this list refuse PUT own."
        )
    )
    previous_toggle: str | None = Field(
        default=None,
        description=(
            "The toggle value this PUT's `toggle` just displaced for `lane`, "
            "read and overwritten under the same lock acquisition. Null for a "
            "GET, or a PUT that did not set `toggle`. A client's own prior "
            "GET/PUT response can be stale by the time it issues a later PUT "
            "(a concurrent agent's write can land in between), so a "
            "compensating rollback must restore THIS value, not one read "
            "earlier over a separate round trip."
        ),
    )


class AnalysisSourcePut(BaseModel):
    """Set the default, the toggle, or both, for one lane."""

    lane: str = Field(description="beatgrid, key, waveform, loudness or vocal")
    default: str | None = Field(
        default=None, description="rbx or own. Persisted; survives a relaunch."
    )
    toggle: str | None = Field(
        default=None,
        description="unset, rbx or own. In-memory; resets to unset on relaunch.",
    )
    expected_toggle: str | None = Field(
        default=None,
        description=(
            "Compare-and-set precondition for `toggle`: apply it only if the "
            "lane's CURRENT toggle equals this value, atomically. 409 on a "
            "mismatch. Ignored unless `toggle` is also given; a plain "
            "`toggle` with no `expected_toggle` sets unconditionally, exactly "
            "as before this field existed."
        ),
    )
    expected_toggle_revision: int | None = Field(
        default=None,
        description=(
            "Additional compare-and-set precondition on top of "
            "`expected_toggle`: apply it only if the lane's CURRENT "
            "`toggle_revision` also equals this value. Closes an ABA gap "
            "`expected_toggle` alone cannot: a value-only compare-and-set "
            "still succeeds after the toggle round-trips own -> rbx -> own, "
            "because the current value equals `expected_toggle` again even "
            "though a newer write happened in between "
            "(discussion_r3974993963 P1 BLOCKING). Ignored unless "
            "`expected_toggle` is also given."
        ),
    )


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "analysis_db_path", STATE_DB))


def _open_ro(request: Request) -> sqlite3.Connection:
    """Read-only. A GET must not migrate, and must not need the writer lock.

    `store.open_conn` opens READ-WRITE and runs the shared-state migrations
    plus the analysis DDL, so using it for a GET made a nominal read mutate
    `state.db` -- from a host that may not hold the writer lock, and during a
    lock-probe outage, both of which `deps.py` exists to exclude (Codex P1,
    PR #1549). `get_default` already answers correctly when the table is
    absent, so the read needs no schema work at all.

    `query_only` on top of `mode=ro` so a write is refused by the connection
    rather than by the filesystem: a `state.db` on a writable volume would
    otherwise still accept one.
    """
    path = _db_path(request)
    if not path.exists():
        # `mode=ro` raises on an absent file, so a GET on a fresh daemon --
        # which is exactly when `make_backend()` chose InMemoryBackend
        # BECAUSE state.db does not exist -- would 500 instead of answering
        # with the documented rbx defaults (Codex P2, PR #1549). An in-memory
        # database is the honest stand-in: no table, so `get_default` returns
        # `rbx` for every lane, which is the true answer, and nothing is
        # created on disk.
        return sqlite3.connect(":memory:")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _open(request: Request) -> sqlite3.Connection:
    """Open with the FULL analysis schema, not just the selection table.

    Promoting a lane to own on a database with no `analysis_projection` was
    a real defect (Codex P1, PR #1549): the readers are hardened against it
    now, but the honest fix is that the surface which can say `own` also
    guarantees the store that word refers to. `store.open_conn` runs the
    Phase 5 migrations and the analysis DDL, so a PUT can never leave the
    database in a state its own GET describes wrongly.
    """
    return analysis_store.open_conn(_db_path(request))


def _require_persistent_backend(request: Request) -> None:
    """Refuse to persist a default the serving backend will not read.

    The in-memory backend has no `track_fields` and no projection to read, so
    a persisted `own` would be invisible to it. The DEV TOGGLE is exempt: it
    is process-local and explicitly a testing affordance, so setting it on a
    backend that cannot honour it costs nothing and resets on relaunch.
    """
    backend = getattr(request.app.state, "backend", None)
    if isinstance(backend, InMemoryBackend):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_persistent_library",
                "message": (
                    "this daemon is running on the in-memory backend, which has "
                    "no state.db to read an own analysis from, so persisting a "
                    "lane default here would report `own` while /tracks kept "
                    "serving rekordbox values. Start the daemon against a real "
                    "data dir first."
                ),
            },
        )


def _apply_toggle(
    lane: str,
    toggle: str,
    expected_toggle: str | None,
    expected_toggle_revision: int | None,
) -> sel.ToggleWrite:
    """The `toggle` half of a PUT: a plain set, or a CAS against
    `expected_toggle` (and, when given, `expected_toggle_revision`).

    Split out of `put_analysis_source` so that function's own branching stays
    under the mccabe ceiling; this is the one place that decides between an
    unconditional write and the compare-and-set rollback path needs.

    Routes through `sel.write_toggle` directly (not the `set_toggle`/
    `compare_and_set_toggle` wrappers) so the displaced value and the
    resulting revision are read from ONE locked write, atomically - a caller
    needing to compensate this exact write later (a failed-switch rollback,
    or `put_analysis_source`'s own default-commit-failure compensation
    below) needs the revision THIS write produced, not one re-derived from a
    later, separately-locked read that a concurrent write could land before
    (discussion_r3974993963 P1 BLOCKING).

    `expected_toggle_revision` is only applied when `expected_toggle` is
    also given - a revision check with no value precondition would be an
    unconditional write, sometimes, keyed on an argument the docstring above
    already says an unconditional write ignores.
    """
    result = sel.write_toggle(
        lane,
        toggle,
        expected=expected_toggle,
        expected_revision=expected_toggle_revision if expected_toggle is not None else None,
    )
    if result is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "toggle_changed",
                "message": (
                    f"lane {lane!r}'s toggle no longer holds "
                    f"{expected_toggle!r}"
                    + (
                        f" at revision {expected_toggle_revision!r}"
                        if expected_toggle_revision is not None
                        else ""
                    )
                    + "; someone else changed it since, so this compare-and-set "
                    "was refused rather than overwriting a newer value"
                ),
            },
        )
    return result


def _refuse_unserved_own(lane: str, source: str) -> None:
    if source != "own":
        return
    if lane in sel.SERVING_LANES:
        return
    raise HTTPException(
        status_code=409,
        detail={"error": f"lane {lane!r} has no serving implementation yet"},
    )


def _validate_put_body(body: AnalysisSourcePut) -> None:
    """Validate EVERYTHING before mutating ANYTHING. A PUT carrying a valid
    default and an invalid toggle used to persist the default and then
    return 422, which a client reads as "nothing happened" (Codex P2).
    """
    try:
        sel.check_lane(body.lane)
        if body.default is not None:
            sel.check_source(body.default)
            _refuse_unserved_own(body.lane, body.default)
        if body.toggle is not None:
            sel.check_toggle_state(body.toggle)
            _refuse_unserved_own(body.lane, body.toggle)
        if body.expected_toggle is not None:
            sel.check_toggle_state(body.expected_toggle)
    except sel.SelectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_selection", "message": str(exc)},
        ) from exc


def _commit_default_or_compensate(
    conn: sqlite3.Connection,
    lane: str,
    default: str,
    toggle: str | None,
    toggle_write: sel.ToggleWrite | None,
) -> None:
    """Commit the persisted default, rolling back a toggle write from the
    same PUT if the commit fails.

    `_apply_toggle` may already have mutated the process-global toggle
    before this runs, which is what makes reads that land right now see a
    source the durable write never actually committed - a combined PUT that
    fails here must not leave that mutation behind (discussion_r3974993965
    P2 BLOCKING). Compensate with the SAME ABA-proof compare this route
    hands its own callers: only restore the toggle if nothing has written
    it since THIS call's own write landed, so a concurrent agent's PUT
    racing the failed commit is never clobbered by a rollback for a request
    that was never about its write.
    """
    try:
        sel.set_default(conn, lane, default)
        conn.commit()
    except sqlite3.Error:
        if toggle_write is not None:
            sel.write_toggle(
                lane,
                toggle_write.previous,
                expected=toggle,
                expected_revision=toggle_write.revision,
            )
        raise


@router.get("/source", response_model=AnalysisSourceOut)
def get_analysis_source(request: Request) -> AnalysisSourceOut:
    conn = _open_ro(request)
    try:
        return AnalysisSourceOut(**sel.source_state(conn))
    finally:
        conn.close()


@router.put("/source", response_model=AnalysisSourceOut)
def put_analysis_source(
    request: Request,
    body: AnalysisSourcePut,
    # Cross-host write exclusion. This route opens state.db READ-WRITE, so it
    # is a mutating route and has to answer to the same lock as every other
    # one: 503 when another host holds the writer lock, and 503 when the lock
    # PROBE itself failed (deps.py fails closed there). Without the
    # dependency it would persist a promotion straight through both states
    # (Codex P1, PR #1549). The returned backend is unused -- the guard is
    # the point.
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> AnalysisSourceOut:
    if body.default is None and body.toggle is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_change_requested",
                "message": "PUT /analysis/source needs at least one of default or toggle",
            },
        )
    _validate_put_body(body)

    # A promotion the RUNNING backend cannot serve is a lie, not a setting.
    # The daemon falls back to InMemoryBackend when `state.db` does not exist
    # (app.py: make_backend()), and this endpoint would then create a fresh
    # database, persist `own` into it, and report `effective: own` while
    # /tracks kept serving the in-memory library until restart (Codex P1).
    # Refused rather than silently migrated: swapping app.state.backend under
    # a live request would change what every in-flight reader is talking to.
    if body.default is not None:
        _require_persistent_backend(request)

    # A toggle-only PUT must not TOUCH the disk. The toggle is process-local
    # by definition, and opening writably would create state.db -- after
    # which `make_backend()` picks SqliteBackend on the NEXT launch purely
    # because the file exists, so a session-only developer toggle would
    # permanently replace an in-memory library with an empty SQLite one
    # (Codex P2, PR #1549). Only a persisted default earns a writable open.
    conn = _open(request) if body.default is not None else _open_ro(request)
    try:
        # The CAS must run BEFORE the default commit. A stale
        # `expected_toggle` raising 409 is a client-visible "nothing
        # happened" - if the default's commit ran first, the durable
        # default would already have taken effect (surviving a relaunch)
        # while the response reported a conflict, the same shape of bug
        # this route's docstring above already fixed once for the
        # validate-everything-before-mutating-anything case (Codex P2,
        # PR #1010, discussion_r3974235466).
        toggle_write = (
            _apply_toggle(
                body.lane, body.toggle, body.expected_toggle, body.expected_toggle_revision
            )
            if body.toggle is not None
            else None
        )
        if body.default is not None:
            _commit_default_or_compensate(
                conn, body.lane, body.default, body.toggle, toggle_write
            )
        state = sel.source_state(conn)
        if toggle_write is not None:
            # Report the revision THIS write produced, not one re-read
            # separately from `state` a moment later - a concurrent write
            # landing in that gap must not make this response claim a
            # revision the request never actually saw (same reasoning as
            # `_apply_toggle`'s docstring).
            state["lanes"][body.lane]["toggle_revision"] = toggle_write.revision
        return AnalysisSourceOut(
            previous_toggle=None if toggle_write is None else toggle_write.previous,
            **state,
        )
    finally:
        conn.close()


__all__ = ["AnalysisSourceOut", "AnalysisSourcePut", "LaneSourceOut", "router"]
