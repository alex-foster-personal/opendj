"""``StateWriter`` -- the single supported mutation surface.

Every write is a small transaction that appends one row to ``events`` and
publishes a matching in-process event on :class:`EventBus`. Callers MUST
NOT ``INSERT`` into the state tables directly; this class is the chokepoint
that guarantees the durable log + in-process fanout stay in sync.

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

    @property
    def raw_conn(self) -> sqlite3.Connection:
        """Public accessor for the underlying ``sqlite3.Connection``.

        Intended for adapters that need to open an outer SAVEPOINT around a
        batch of writer calls (e.g. a dry-run ingest that rolls the whole
        batch back). Prefer this over poking ``_conn`` from the outside so
        internal refactors do not break call sites.

        Callers MUST NOT issue ``BEGIN``/``COMMIT`` on this connection --
        that would fight the writer's SAVEPOINT semantics. Use
        ``SAVEPOINT <name>`` / ``RELEASE`` / ``ROLLBACK TO`` instead.
        """
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Run the enclosed block in a SAVEPOINT.

        SAVEPOINTs are nestable, implicitly open a transaction when none is
        active, and play well with an outer ``SAVEPOINT`` (used by the
        ingest adapter for dry-run rollback). Each call gets a unique name
        so re-entrant calls on the same writer are safe.
        """
        name = f"sw_{next(self._sp_counter)}"
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield self._conn
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self._conn.execute(f"RELEASE SAVEPOINT {name}")

    @contextmanager
    def playlist_transaction(self) -> Iterator[sqlite3.Connection]:
        """Lock a playlist mutation before reading its CAS precondition.

        Callers must load the playlist and validate its expected revision while
        this context is open. Membership replacement also validates every
        requested stable id here. That sequence is one SQLite write
        transaction across processes.
        """
        with immediate_transaction(self._conn) as conn:
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
