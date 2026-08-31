"""``StateWriter`` -- the single supported mutation surface.

Every write is a small transaction that appends one row to ``events`` and
publishes a matching in-process event on :class:`EventBus`. Callers MUST
NOT ``INSERT`` into the state tables directly; this class is the chokepoint
that guarantees the durable log + in-process fanout stay in sync.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator

from . import locations as _locations
from . import provenance as _prov
from . import sync_stamp as _sync_stamp
from .events import EventBus, FakeEventBus
from .types import Event, Source

_Clock = Callable[[], datetime]

# The synced tables this class writes, and the primary key columns whose
# values become the ``local_changelog`` row_pk. Kept beside the writes rather
# than imported from apps.sync_hub: apps.shared must not depend on the sync
# app, and tests/shared/state/test_sync_stamp.py pins the two lists together.
TRACKS_TABLE: str = "tracks"
VENDOR_IDS_TABLE: str = "track_vendor_ids"
PLAYLISTS_TABLE: str = "playlists"
MEMBERSHIPS_TABLE: str = "playlist_memberships"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Canonical sync timestamp for ``dt``.

    A naive datetime is read as UTC (long-standing behaviour of the injected
    test clocks) and then rendered in the one format the LWW comparison can
    order -- fixed-width microseconds, explicit ``+00:00`` (ADR 08 point 2).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _sync_stamp.canonical_from(dt)


def compute_playlist_id(vendor: str, vendor_pl_id: str) -> str:
    """Stable playlist id: ``sha1('<vendor>:<vendor_pl_id>')``.

    Lets us round-trip a playlist between vendors without collisions while
    keeping the id deterministic.
    """
    return hashlib.sha1(f"{vendor}:{vendor_pl_id}".encode("utf-8")).hexdigest()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Acquire SQLite's writer lock before an authoritative read-modify-write.

    Membership replacement is destructive, so an ETag, member validation, and
    replacement must all happen after this lock is held.  Nested callers use a
    SAVEPOINT through :meth:`StateWriter._tx`; this outer primitive fails fast
    if a caller attempts to upgrade an already-open deferred transaction.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "immediate transaction requires an idle connection; acquire it "
            "before reading membership state"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_playlist_revision(
    conn: sqlite3.Connection, playlist_id: str, candidate: str,
) -> str:
    """Return a revision distinct from the playlist's current ``updated_at``.

    Wall-clock timestamps normally differ at microsecond precision.  A fixed
    clock used by a deterministic caller must still rotate an ETag, so advance
    an equal ISO timestamp by one microsecond instead of silently reusing it.
    """
    row = conn.execute(
        "SELECT updated_at FROM playlists WHERE playlist_id = ?", (playlist_id,),
    ).fetchone()
    if row is None:
        return candidate
    previous = datetime.fromisoformat(row[0])
    requested = datetime.fromisoformat(candidate)
    if requested > previous:
        return candidate
    return _iso(previous + timedelta(microseconds=1))


class StateWriter:
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

    def __enter__(self) -> "StateWriter":
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

    # --- tracks -----------------------------------------------------

    def upsert_track(
        self,
        *,
        stable_id: str,
        stable_id_tier: str,
        title: str | None,
        artists: Iterable[str],
        album: str | None,
        isrc: str | None,
        duration_ms: int | None,
        file_path: str | None,
        content_hash: str | None = None,
    ) -> bool:
        """Insert/update ``tracks``. Returns True on change, False on no-op.

        A byte-equal repeat is a no-op (no history, no event).
        """
        artists_json = json.dumps(
            list(artists), sort_keys=False, separators=(",", ":"), ensure_ascii=False
        )
        now = self._now_iso()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT stable_id_tier, title, artists_json, album, isrc, "
                "duration_ms, file_path, content_hash FROM tracks "
                "WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            new_row = (
                stable_id_tier,
                title,
                artists_json,
                album,
                isrc,
                duration_ms,
                file_path,
                content_hash,
            )
            if existing is not None and tuple(existing) == new_row:
                return False
            stamp = self._stamp(TRACKS_TABLE, (stable_id,), now)
            if existing is None:
                conn.execute(
                    "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                    "artists_json, album, isrc, duration_ms, file_path, "
                    "content_hash, created_at, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stable_id,
                        stable_id_tier,
                        title,
                        artists_json,
                        album,
                        isrc,
                        duration_ms,
                        file_path,
                        content_hash,
                        now,
                        stamp.updated_at,
                        stamp.origin_device_id,
                    ),
                )
                kind = "track.insert"
            else:
                conn.execute(
                    "UPDATE tracks SET stable_id_tier=?, title=?, artists_json=?, "
                    "album=?, isrc=?, duration_ms=?, file_path=?, content_hash=?, "
                    "updated_at=?, origin_device_id=? WHERE stable_id=?",
                    (*new_row, stamp.updated_at, stamp.origin_device_id, stable_id),
                )
                kind = "track.update"
            ev = self._append_event(
                kind=kind,
                stable_id=stable_id,
                payload={"tier": stable_id_tier, "had_isrc": bool(isrc)},
                ts=now,
            )
            self.bus.publish(ev)
            _locations.sync_primary_local(
                conn,
                stable_id=stable_id,
                file_path=file_path,
                now=now,
                machine_id=self.machine_id(),
            )
        return True

    def upsert_track_location(
        self,
        *,
        stable_id: str,
        kind: _locations.Kind,
        file_path: str | None = None,
        remote_url: str | None = None,
        role: _locations.Role = "alternate",
        content_hash: str | None = None,
    ) -> str:
        """Record an extra playable copy on THIS machine.

        Does not change ``tracks.file_path``. Returns the ``location_id``
        (uuid4 hex): schema v6 dropped the INTEGER rowid key because it
        collided across machines.
        """
        now = self._now_iso()
        with self._tx() as conn:
            loc_id = _locations.upsert_location(
                conn,
                stable_id=stable_id,
                kind=kind,
                file_path=file_path,
                remote_url=remote_url,
                role=role,
                content_hash=content_hash,
                now=now,
                machine_id=self.machine_id(),
            )
            ev = self._append_event(
                kind="track.location.upsert",
                stable_id=stable_id,
                payload={"location_id": loc_id, "kind": kind, "role": role},
                ts=now,
            )
            self.bus.publish(ev)
        return loc_id

    # --- vendor ids -------------------------------------------------

    def set_vendor_id(
        self, stable_id: str, vendor: str, vendor_id: str
    ) -> None:
        now = self._now_iso()
        with self._tx() as conn:
            stamp = self._stamp(VENDOR_IDS_TABLE, (stable_id, vendor), now)
            # Upsert rather than INSERT OR REPLACE: the latter is a
            # delete-then-insert, which would silently drop ``deleted_at``
            # (and fire ON DELETE cascades) on every vendor-id refresh.
            conn.execute(
                "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id, "
                "updated_at, origin_device_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(stable_id, vendor) DO UPDATE SET "
                "vendor_id=excluded.vendor_id, "
                "updated_at=excluded.updated_at, "
                "origin_device_id=excluded.origin_device_id",
                (
                    stable_id,
                    vendor,
                    vendor_id,
                    stamp.updated_at,
                    stamp.origin_device_id,
                ),
            )
            ev = self._append_event(
                kind="track.vendor_id.set",
                stable_id=stable_id,
                payload={"vendor": vendor, "vendor_id": vendor_id},
                ts=now,
            )
            self.bus.publish(ev)

    # --- wrapped fields --------------------------------------------

    def set_field(
        self,
        stable_id: str,
        field_name: str,
        value: Any,
        *,
        source: Source,
        modified_at: str,
        confidence: float | None = None,
    ) -> bool:
        """Set a provenance-wrapped field. Returns True on change.

        The DB mutation and the bus publish are ordered inside one outer
        SAVEPOINT: if ``bus.publish`` raises, the mutation is rolled back so
        subscribers and the ``track_fields`` row can never drift. This keeps
        the durable log + in-process fanout invariant the module docstring
        promises -- addresses Codex P05 finding on mutation/publish ordering.
        """
        with self._tx():
            changed = _prov.write_field(
                self._conn,
                stable_id=stable_id,
                field_name=field_name,
                value=value,
                source=source,
                modified_at=modified_at,
                confidence=confidence,
                actor=self._actor,
                now=self._now_iso(),
                machine_id=self.machine_id(),
            )
            if changed:
                payload: dict[str, Any] = {
                    "field_name": field_name,
                    "value": value,
                    "source": source,
                    "modified_at": modified_at,
                }
                if confidence is not None:
                    payload["confidence"] = confidence
                # Publish inside the SAVEPOINT: a raising bus propagates out
                # of ``_tx`` and triggers ROLLBACK TO, reverting write_field's
                # already-RELEASEd inner SAVEPOINT.
                self.bus.publish(
                    Event(
                        ts=self._now_iso(),
                        kind="track.field.set",
                        stable_id=stable_id,
                        payload=payload,
                        actor=self._actor,
                    )
                )
        return changed

    # --- playlists --------------------------------------------------

    def insert_playlist(
        self,
        *,
        playlist_id: str,
        name: str,
        vendor: str,
        vendor_pl_id: str,
    ) -> bool:
        """Insert, rename, or reactivate a playlist. Returns True on change.

        A ``playlist_id`` is deterministic (``compute_playlist_id``), so a
        vendor re-ingest can land on a row this machine previously
        soft-deleted (ADR 08 point 5): a rekordbox playlist deleted locally
        and still present on the next scan must come back, not stay a
        permanent tombstone. Reactivation is therefore keyed on
        ``deleted_at`` being set, not only on the name having changed --
        replaying an identical name onto a tombstoned row must still clear
        it.
        """
        now = self._now_iso()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT name, deleted_at FROM playlists WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if existing is None:
                stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
                conn.execute(
                    "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
                    "created_at, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        name,
                        vendor,
                        vendor_pl_id,
                        now,
                        stamp.updated_at,
                        stamp.origin_device_id,
                    ),
                )
                kind = "playlist.insert"
            elif existing[0] != name or existing[1] is not None:
                reactivated = existing[1] is not None
                revision = next_playlist_revision(conn, playlist_id, now)
                stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), revision)
                conn.execute(
                    "UPDATE playlists SET name=?, updated_at=?, "
                    "origin_device_id=?, deleted_at=NULL WHERE playlist_id=?",
                    (name, stamp.updated_at, stamp.origin_device_id, playlist_id),
                )
                kind = "playlist.undelete" if reactivated else "playlist.update"
            else:
                return False
            ev = self._append_event(
                kind=kind,
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "vendor": vendor,
                    "vendor_pl_id": vendor_pl_id,
                    "name": name,
                },
                ts=now,
            )
            self.bus.publish(ev)
        return True

    def set_playlist_memberships(
        self, playlist_id: str, stable_ids: list[str]
    ) -> None:
        """Full-replace playlist memberships. Positions become 0..N-1.

        Also bumps ``playlists.updated_at`` so row-version etags derived
        from it (webui optimistic concurrency) observe membership-only
        changes, not just renames. That bump is load-bearing for sync too:
        membership rows travel attached to their playlist row (ADR 04 c5), so
        a membership edit that left ``playlists.updated_at`` alone would
        never propagate and would strand the fleet on a permanent digest
        mismatch (round 1 finding 5b).
        """
        transaction = self._tx() if self._conn.in_transaction else immediate_transaction(self._conn)
        with transaction as conn:
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            conn.execute(
                "DELETE FROM playlist_memberships WHERE playlist_id = ?",
                (playlist_id,),
            )
            for position, sid in enumerate(stable_ids):
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "INSERT INTO playlist_memberships(playlist_id, stable_id, "
                    "position, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        sid,
                        position,
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
                "WHERE playlist_id = ?",
                (stamp.updated_at, stamp.origin_device_id, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.memberships.set",
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "count": len(stable_ids),
                },
                ts=now,
            )
            self.bus.publish(ev)

    def delete_playlist(self, playlist_id: str) -> bool:
        """Tombstone a playlist and its memberships. Returns True when a live row existed.

        ADR 08 point 5 (round 1 finding 4a): a hard ``DELETE`` on a synced
        table has no way to tell a peer a row is gone -- a spoke that had
        already pushed the row finds nothing left to offer, and a hub that
        gets hard-deleted under it 409s every future pull permanently
        (``hub_changes_since`` refuses to serve a changelog entry for a row
        that no longer exists). Stamping ``deleted_at`` instead keeps the row
        alive for the merge and lets deletion itself propagate as an
        ordinary LWW-losable write.

        Memberships are stamped explicitly rather than left for an
        ``ON DELETE CASCADE`` (there is no cascade to leave -- this is an
        ``UPDATE``, not a ``DELETE``) so each membership row's tombstone gets
        its own ``local_changelog`` entry and travels with the playlist
        bundle (``apps.sync_hub.engine._replace_members`` reads whatever
        currently sits in the table, deleted or not). Already-tombstoned
        memberships are left alone -- re-stamping them would move their
        ``updated_at`` without a real change. Appends one ``playlist.delete``
        event on success.
        """
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT name, vendor, vendor_pl_id FROM playlists "
                "WHERE playlist_id = ? AND deleted_at IS NULL",
                (playlist_id,),
            ).fetchone()
            if existing is None:
                return False
            # Advance past the row's own stored updated_at, the same guard
            # insert_playlist and set_playlist_memberships use: a frozen test
            # clock (or two writes in the same wall-clock tick) must not
            # stamp the tombstone with a sort key equal to what is already
            # stored, which the strict LWW "<=" comparison would then never
            # let this delete overwrite on its own hub.
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            member_positions = [
                row[0]
                for row in conn.execute(
                    "SELECT position FROM playlist_memberships "
                    "WHERE playlist_id = ? AND deleted_at IS NULL",
                    (playlist_id,),
                )
            ]
            for position in member_positions:
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "UPDATE playlist_memberships SET deleted_at=?, updated_at=?, "
                    "origin_device_id=? WHERE playlist_id=? AND position=?",
                    (
                        member_stamp.updated_at,
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        position,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at=?, origin_device_id=?, "
                "deleted_at=? WHERE playlist_id=?",
                (stamp.updated_at, stamp.origin_device_id, stamp.updated_at, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.delete",
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "name": existing[0],
                    "vendor": existing[1],
                    "vendor_pl_id": existing[2],
                },
                ts=now,
            )
            self.bus.publish(ev)
        return True

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
