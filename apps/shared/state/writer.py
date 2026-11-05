"""``StateWriter`` -- the single supported mutation surface.

Every write is a small transaction that appends one row to ``events``
inside the SQL unit and stages a matching in-process :class:`EventBus`
event until COMMIT succeeds. Callers MUST NOT ``INSERT`` into the state
tables directly; this class is the chokepoint that keeps the durable log
and in-process fanout ordered (INSERT+COMMIT, then publish). A subscriber
that raises during fanout cannot roll back a committed row.

The track-level writes (tracks, locations, vendor ids, wrapped fields) and
the playlist writes (insert, membership replace, tombstone) moved to
:mod:`apps.shared.state.writer_tracks` and
:mod:`apps.shared.state.writer_playlists` (quality-gate file_size ratchet,
round 4), as mixins :class:`StateWriter` composes below. Both assume the
infrastructure this class defines -- ``_tx``, ``_stamp``, ``_now_iso``,
``machine_id``, ``bus`` -- so neither is usable on its own. The constants
and free helpers both mixins ALSO need (table names,
``immediate_transaction``, ``next_playlist_revision``) live in
:mod:`apps.shared.state.writer_common` rather than here, so this module can
import the two mixins without a cycle: this module and both mixins import
only ``writer_common``, never each other. An earlier version of this split
had the mixins import their constants back from this module instead, which
worked when this module happened to be the first of the three imported and
broke -- reproducibly -- whenever anything imported a mixin module first
(``python -m`` re-importing a package under its real name is one such path;
so is any test collector that imports alphabetically).
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from . import db as _state_db
from . import sync_stamp as _sync_stamp
from .events import EventBus, FakeEventBus
from .types import Event
from .writer_availability import _AvailabilityWriterMixin
from .writer_common import (
    _default_clock,
    _iso,
    compute_playlist_id,
    immediate_transaction,
    next_playlist_revision,
)
from .writer_playlists import _PlaylistWriterMixin
from .writer_tracks import _TrackWriterMixin

_Clock = Callable[[], datetime]


class _QueuedBus:
    """Stand-in that stages ``publish`` until the owning write unit commits."""

    def __init__(
        self, real: EventBus | FakeEventBus, pending: list[Event]
    ) -> None:
        self._real = real
        self._pending = pending

    def publish(self, event: Event) -> None:
        self._pending.append(event)

    def subscribe(self, kind: str, callback: Any) -> None:
        self._real.subscribe(kind, callback)

    def close(self, timeout: float | None = None) -> None:
        self._real.close(timeout)


class StateWriter(_TrackWriterMixin, _PlaylistWriterMixin, _AvailabilityWriterMixin):
    """Write-serialised facade over the state DB.

    Owns its own event bus unless one is injected. Tests can pass in a
    :class:`FakeEventBus` to capture events synchronously.

    Lifecycle
    ---------
    When no bus is injected, ``__init__`` starts a daemon thread via
    :class:`EventBus`. Callers MUST release that thread by one of:

    - using the writer as a context manager (``with StateWriter(...) as w:``),
    - calling :meth:`close` explicitly (preferred in ``try/finally``), or
    - dropping all references and relying on the ``__del__`` backstop.

    The ``__del__`` backstop is best-effort only -- Python does not
    guarantee finaliser ordering, so production code should use explicit
    ``close`` to avoid daemon-thread leaks under long-running processes.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        bus: EventBus | FakeEventBus | None = None,
        *,
        clock: _Clock | None = None,
        actor: str | None = None,
    ) -> None:
        self._conn = conn
        self._own_bus = bus is None
        self.bus: EventBus | FakeEventBus = bus if bus is not None else EventBus()
        self._clock = clock or _default_clock
        self._actor = actor
        self._sp_counter = itertools.count()
        self._closed = False
        self._machine_id: str | None = None
        self._pending_events: list[Event] = []
        self._tx_depth = 0
        self._abort_error: BaseException | None = None

    @property
    def raw_conn(self) -> sqlite3.Connection:
        """Public accessor for the underlying ``sqlite3.Connection``.

        Intended for adapters that need to open an outer writer-managed
        unit around a batch of writer calls (e.g. a dry-run ingest that
        rolls the whole batch back). Prefer this over poking ``_conn``
        from the outside so internal refactors do not break call sites.

        Callers MUST NOT issue ``BEGIN``/``COMMIT``/``SAVEPOINT`` by hand on
        this connection. Open the outer unit with :meth:`transaction`,
        which composes with nested :meth:`_tx` SAVEPOINTs and takes the
        writer lock up front when it is the outermost one (STATE-15).
        """
        return self._conn

    def _raise_if_aborted(self) -> None:
        if self._tx_depth > 0 and self._abort_error is not None:
            raise RuntimeError(
                "StateWriter transaction was aborted by a failed nested operation"
            ) from self._abort_error

    def _guard_unit(self, *, require_idle: bool) -> None:
        self._raise_if_aborted()
        busy = self._conn.in_transaction or self._tx_depth != 0
        if require_idle and busy:
            raise RuntimeError("StateWriter transaction requires an idle connection")
        if not require_idle and self._conn.in_transaction and self._tx_depth == 0:
            raise RuntimeError(
                "caller-owned transaction requires StateWriter.transaction()"
            )

    def _drop_queued_from(self, start: int) -> None:
        del self._pending_events[start:]

    def _flush_pending(self) -> None:
        events = self._pending_events
        self._pending_events = []
        for event in events:
            self.bus.publish(event)

    @contextmanager
    def _managed_write_unit(
        self, *, keep: bool = True, require_idle: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """Writer-owned ``write_unit``: queue bus events, flush after COMMIT."""
        self._guard_unit(require_idle=require_idle)
        pending_start = len(self._pending_events)
        outermost = self._tx_depth == 0
        real_bus = self.bus
        if outermost:
            self.bus = _QueuedBus(real_bus, self._pending_events)  # type: ignore[assignment]
        self._tx_depth += 1
        error: BaseException | None = None
        try:
            with _state_db.write_unit(
                self._conn, f"sw_{next(self._sp_counter)}", keep=keep
            ):
                yield self._conn
                self._raise_if_aborted()
        except BaseException as exc:
            error = exc
            self._drop_queued_from(pending_start)
            if self._abort_error is None:
                self._abort_error = exc
            raise
        else:
            if not keep:
                self._drop_queued_from(pending_start)
        finally:
            self._tx_depth -= 1
            if outermost:
                self.bus = real_bus
                if self._tx_depth == 0:
                    self._abort_error = None
        if error is None and outermost and keep:
            self._flush_pending()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Run the enclosed block as one write unit that holds the writer lock.

        Outermost, the unit is ``BEGIN IMMEDIATE`` ... ``COMMIT``; nested
        inside :meth:`transaction` (or a re-entrant call on this writer) it
        is a uniquely named SAVEPOINT. In-process bus events are queued
        until the outermost unit COMMITs. See
        :func:`apps.shared.state.db.write_unit` for why a bare outermost
        SAVEPOINT is not enough (STATE-15).
        """
        with self._managed_write_unit():
            yield self._conn

    @contextmanager
    def transaction(self, *, commit: bool = True) -> Iterator[sqlite3.Connection]:
        """Outermost writer-managed unit around a batch of mutations.

        Requires an idle connection. Nested :meth:`_tx` calls become
        SAVEPOINTs and do not flush the bus. ``commit=False`` rolls the
        unit back and drops queued events (dry-run ingest).
        """
        with self._managed_write_unit(keep=commit, require_idle=True):
            yield self._conn

    @contextmanager
    def _join_or_begin(self, *, commit: bool = True) -> Iterator[sqlite3.Connection]:
        """Join an open writer unit, or open :meth:`transaction`.

        Nested ``commit=False`` rolls back only this SAVEPOINT so a dry-run
        ingest can sit inside a live parent unit without persisting.
        """
        if self._tx_depth > 0:
            with self._managed_write_unit(keep=commit):
                yield self._conn
            return
        with self.transaction(commit=commit):
            yield self._conn

    @contextmanager
    def playlist_transaction(self) -> Iterator[sqlite3.Connection]:
        """Lock a playlist mutation before reading its CAS precondition.

        Callers must load the playlist and validate its expected revision while
        this context is open. Membership replacement also validates every
        requested stable id here. That sequence is one SQLite write
        transaction across processes; bus events flush after COMMIT.
        """
        with self.transaction() as conn:
            yield conn

    # --- lifecycle --------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._own_bus:
            self.bus.close()

    def __enter__(self) -> StateWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        # [I3] Best-effort backstop for callers that forget to close().
        # Finalisers can run during interpreter shutdown when attributes are
        # already torn down, so swallow everything.
        try:
            if not getattr(self, "_closed", True):
                self.close()
        except Exception:
            pass

    # --- helpers ----------------------------------------------------

    def _now_iso(self) -> str:
        return _iso(self._clock())

    def machine_id(self) -> str:
        """This writer's origin device id, resolved once per instance.

        Every synced-table write carries it as ``origin_device_id``; without
        it the LWW tiebreak is empty on both sides and degenerates to
        first-writer-wins (round 1 finding 2). Resolution fails loudly on a
        connection with no data dir rather than inventing an identity.
        """
        if self._machine_id is None:
            self._machine_id = _sync_stamp.ensure_local_machine(self._conn)
        return self._machine_id

    def _stamp(
        self, table: str, row_pk: tuple[Any, ...], now: str
    ) -> _sync_stamp.Stamp:
        """Stamp one synced-table write and log it to ``local_changelog``."""
        return _sync_stamp.stamp_and_log(
            self._conn, table, row_pk, self.machine_id(), now=now,
        )

    def _append_event(
        self,
        *,
        kind: str,
        stable_id: str | None,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> Event:
        ts_val = ts or self._now_iso()
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        self._conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts_val, kind, stable_id, payload_json, self._actor),
        )
        return Event(
            ts=ts_val,
            kind=kind,
            stable_id=stable_id,
            payload=payload,
            actor=self._actor,
        )

    # --- adapters ---------------------------------------------------

    def register_adapter(
        self,
        adapter_id: str,
        *,
        last_run_at: str,
        last_ok: bool,
        notes: str | None = None,
    ) -> None:
        now = self._now_iso()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO adapters(adapter_id, last_run_at, last_ok, notes) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(adapter_id) DO UPDATE SET "
                "last_run_at=excluded.last_run_at, "
                "last_ok=excluded.last_ok, "
                "notes=excluded.notes",
                (adapter_id, last_run_at, 1 if last_ok else 0, notes),
            )
            ev = self._append_event(
                kind="adapter.run",
                stable_id=None,
                payload={
                    "adapter_id": adapter_id,
                    "last_run_at": last_run_at,
                    "last_ok": bool(last_ok),
                },
                ts=now,
            )
            self.bus.publish(ev)


__all__ = [
    "StateWriter",
    "compute_playlist_id",
    "immediate_transaction",
    "next_playlist_revision",
]
