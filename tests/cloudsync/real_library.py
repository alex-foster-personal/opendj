"""Machinery behind the REAL-LIBRARY fixture tier for the CLOUDSYNC suite.

Every other cloudsync test seeds a handful of synthetic rows. Those prove the
merge rules; they cannot prove the engine survives a supplied real library
with varied writer history, playlists, NFD paths off macOS, and an older
schema that has to migrate under the sync set before a row can be offered.

This module builds that tier. It is a helpers module rather than more
``conftest.py`` because the fixtures themselves are six lines each once the
machinery is out of the way, and ``conftest.py`` is where a reader looks for
what a fixture GUARANTEES, not for how a subset is sliced.

Three decisions worth reading before changing anything:

1. **The source library is READ-ONLY and is never opened for writing.**
   :func:`prepare_library` copies it first, and every later step touches the
   copy. The path is a constant here rather than a discovered default so a
   run that points somewhere else has to say so
   (:data:`REAL_LIBRARY_ENV_VAR`), and an absent file SKIPS rather than
   fabricates a library -- the same gate ``tests/webui/test_settings.py``
   uses, for the same reason: CI has no real library and must not pretend.

2. **The copy is repaired with the documented operator pass before use --
   and there is a SECOND, UNREPAIRED copy for the tests that must not be.**
   A supplied older library may store timestamps that carry no UTC offset
   (SQLite's own ``CURRENT_TIMESTAMP`` spelling), and every synthetic fixture
   in the suite mints canonical stamps, so repairing before use meant NO
   fixture anywhere could put a legacy stamp in a local table and then apply
   an incoming row against it. That is the hole round 5 closed:
   :func:`prepare_library_unrepaired` hands out the library exactly as it is
   on disk, and asserts it still carries unorderable stamps so a library
   that stops carrying them fails this tier loudly instead of silently
   testing nothing. :class:`PreparedLibrary` keeps the scan result either
   way, so a test asserts the legacy rows were THERE rather than assuming it.

3. **The subset is a deterministic slice, never a sample.** ``ORDER BY
   stable_id LIMIT n`` picks the same rows on every run, so a failure is
   reproducible from the seed size alone. Each spoke mints its OWN
   ``location_id`` and carries its OWN ``machine_id`` on ``track_locations``,
   because that is what two real machines holding the same library do: the
   rows are per-machine facts (ADR 08 point 1), and copying one machine's
   ``location_id`` onto another would test a fleet that cannot exist.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, normalize_stamps
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, protocol, service

from .real_library_source import (
    PACKAGED_LIBRARY_STATE_DB,
    LibraryPremiseMissing,
    snapshot_read_only,
)

#: Point the tier at another library without editing this file. Set it to a
#: ``state.db`` path; unset, :data:`DEFAULT_REAL_LIBRARY_STATE_DB` is used.
REAL_LIBRARY_ENV_VAR: str = "MDT_REAL_LIBRARY_STATE_DB"

#: Where a real library sits on this machine, in preference order. The
#: default is the packaged app's own ``state.db``
#: (:data:`~tests.cloudsync.real_library_source.PACKAGED_LIBRARY_STATE_DB`),
#: read only through a read-only snapshot. Every path this replaced sat under
#: ``/Users/dev``, a home directory no current host has, so the whole tier
#: skipped and went green by measuring nothing -- the "board with zero failing
#: checks because it has zero checks" case in
#: ``.claude/rules/verification.md``. Set :data:`REAL_LIBRARY_ENV_VAR` to point
#: at another supplied library copy. Historical private inputs are not
#: included with this source.
#:
#: Absent on CI and in cloud sessions, which is what keeps the skip
#: load-bearing there -- and why the class this tier guards ALSO has a
#: generic regression coverage where the necessary inputs are available.
#: Captured private-input regressions are withheld from the public source.
#: ``just cloudsync-slow`` fails a run in which
#: this tier executed nothing, so a skip there can never read as green.
REAL_LIBRARY_CANDIDATES: tuple[Path, ...] = (PACKAGED_LIBRARY_STATE_DB,)

#: The first candidate, reported when NONE of them exists so the skip names a
#: real path rather than an empty string.
DEFAULT_REAL_LIBRARY_STATE_DB: Path = REAL_LIBRARY_CANDIDATES[0]

#: Tracks per spoke by default. Big enough that the payload, the batching and
#: the natural-key path all see production shapes; small enough that a whole
#: fleet fixture builds in about a second.
DEFAULT_SUBSET_TRACKS: int = 500

#: The synced tables seeded from the library, in FK-safe order.
#: ``playlist_memberships`` is seeded too but never appears here: it is not a
#: top-level pushable table (ADR 04 c5, it rides its ``playlists`` row), so a
#: count of these is exactly what one full offer should carry.
SEEDED_TABLES: tuple[str, ...] = (
    "tracks",
    "track_vendor_ids",
    "track_fields",
    "playlists",
    "track_locations",
)


#: Errnos that mean "this candidate is not reachable here", not "the runner is
#: broken". EIO, ESTALE and EMFILE are environment faults, and swallowing them
#: would skip a broken runner to green, so they propagate.
UNAVAILABLE_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EACCES,
        errno.EPERM,
        errno.ENOENT,
        errno.ENOTDIR,
        errno.EISDIR,
        errno.ELOOP,
        errno.ENAMETOOLONG,
    }
)


def library_is_available(path: Path) -> bool:
    """Can this process actually READ ``path`` as a regular file?

    Probe is ``path.stat()`` + ``S_ISREG`` + ``open("rb")``. Three reasons:

    1. **EACCES must skip.** A candidate this user cannot traverse must read
       as absent.
    2. **Unreadability must skip.** A mode-0000 file in a traversable
       directory still stats as a regular file; ``open`` is the probe callers
       actually need.
    3. **Operational errors must raise.** Only :data:`UNAVAILABLE_ERRNOS` is
       caught; EIO / ESTALE / EMFILE / ENFILE propagate.
    """
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            return False
        with path.open("rb"):
            return True
    except OSError as exc:
        if exc.errno in UNAVAILABLE_ERRNOS:
            return False
        raise


is_readable_state_db = library_is_available


def first_available(candidates: Sequence[Path], default: Path) -> Path:
    """First candidate this process can read, else ``default``.

    Seam so regressions pass real tmp_path files instead of monkeypatching
    REAL_LIBRARY_CANDIDATES (AGENTS.md: no mocks / no monkeypatching).
    """
    for candidate in candidates:
        if library_is_available(candidate):
            return candidate
    return default


def source_state_db() -> Path:
    """The library this tier reads, whether or not it exists on this machine.

    The env override wins outright, including when it names a path that is
    not there: an operator who pointed this somewhere deserves the failure
    about THAT path, not a silent fall-through to a different library. This
    function does not stat the override; the fixture gate is what fails
    loud if that path is unreadable.

    Otherwise the first candidate that is a readable file wins. A candidate
    this process cannot stat (``PermissionError`` / ``OSError`` on a parent
    that exists but is not traversable) is treated as absent, the same as a
    missing path. The first candidate overall is returned when none is
    readable, so the skip message names a path.
    """
    override = os.environ.get(REAL_LIBRARY_ENV_VAR)
    if override:
        return Path(override)
    return first_available(REAL_LIBRARY_CANDIDATES, DEFAULT_REAL_LIBRARY_STATE_DB)


def skip_unless_real_library(source: Path) -> None:
    """Skip this test if ``source`` is not a usable real library.

    An operator who set :data:`REAL_LIBRARY_ENV_VAR` gets a loud error if
    that path is unreadable: they pointed here on purpose. Without the
    override, an unstatable candidate is treated as absent so CI on
    agentbox SKIPS (naming the path) instead of ERROR.
    """
    absent = (
        f"real library state.db not present at {source}; set "
        f"MDT_REAL_LIBRARY_STATE_DB to point at one, or skip this tier"
    )
    override = os.environ.get(REAL_LIBRARY_ENV_VAR)
    try:
        mode = source.stat().st_mode
        if not stat.S_ISREG(mode):
            pytest.skip(absent)
        with source.open("rb"):
            pass
    except OSError as exc:
        errno_val = exc.errno
        if errno_val in (errno.ENOENT, errno.ENOTDIR):
            pytest.skip(absent)
        if errno_val in (errno.EACCES, errno.EPERM):
            if override:
                raise
            pytest.skip(
                f"real library state.db not readable at {source} "
                f"({type(exc).__name__}); set MDT_REAL_LIBRARY_STATE_DB to "
                f"point at one, or skip this tier"
            )
        raise
    else:
        return


# ----- preparing the library --------------------------------------------------


@dataclass(frozen=True)
class PreparedLibrary:
    """A private, migrated, stamp-repaired copy of the real library.

    Guarantees, all of them checked in :func:`prepare_library` rather than
    assumed by its callers:

    * ``path`` is a COPY. The source file was opened read-only and is
      untouched.
    * ``schema_version`` equals ``state_schema.SCHEMA_VERSION`` (v7), reached
      through the real migration ladder from the library's own v5.
    * ``repairs`` is what ``normalize_stamps.scan`` found on the migrated
      copy, and it is never empty -- both builders assert that, because it is
      this tier's premise stated as a PRESENCE check.
    * when ``repaired`` is True the repair ran and a rescan afterwards found
      nothing; when it is False the copy still holds every one of them.
    """

    path: Path
    schema_version: int
    repairs: tuple[normalize_stamps.Repair, ...]
    repaired: bool = True

    @property
    def repairs_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for repair in self.repairs:
            counts[repair.reason] = counts.get(repair.reason, 0) + 1
        return counts

    def first_repair(self, reason: str) -> normalize_stamps.Repair:
        """One real repaired value, for a test that needs an authentic one."""
        for repair in self.repairs:
            if repair.reason == reason:
                return repair
        raise AssertionError(
            f"the real library carried no {reason!r} stamp; this tier's "
            f"premise (a v5 library holds unorderable stamps) no longer holds"
        )


def _migrated_copy(source: Path, dest_dir: Path) -> tuple[Path, int]:
    """Snapshot ``source`` under ``dest_dir`` and climb the real migration ladder.

    The copy lands at ``<dest_dir>/state/state.db`` so
    ``sync_stamp.data_dir_for_connection`` reads ``dest_dir`` as its data dir
    and mints the machine-id file there rather than beside the original. It is
    taken through :func:`snapshot_read_only` (``mode=ro`` plus the backup
    API), never a byte copy: the default source is a live app's WAL database,
    and a byte copy of ``state.db`` alone drops rows still in the WAL.
    """
    target = Path(dest_dir) / "state" / "state.db"
    snapshot_read_only(Path(source), target)
    conn = state_db.open_rw(target)
    try:
        version = int(
            conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        )
    finally:
        conn.close()
    if version != state_schema.SCHEMA_VERSION:
        raise AssertionError(
            f"migrated copy is at schema v{version}, expected "
            f"v{state_schema.SCHEMA_VERSION}"
        )
    return target, version


def _fill_test_identity(conn: sqlite3.Connection) -> None:
    """Give unsyncable inferred rows a unique hash on this disposable copy.

    Rows lacking ``content_hash`` need an identity to pass merge-rule
    preflight in this disposable test copy;
    hashing ``stable_id`` is unique per PK so distinct tracks do not
    collapse. This is not production hashing and it never touches the
    read-only source snapshot.
    """
    rows = conn.execute(
        """
        SELECT stable_id FROM tracks
        WHERE deleted_at IS NULL
          AND stable_id_tier = 'inferred'
          AND (content_hash IS NULL OR content_hash = '')
          AND (isrc IS NULL OR isrc = '')
        """
    ).fetchall()
    for (pk,) in rows:
        conn.execute(
            "UPDATE tracks SET content_hash = ? WHERE stable_id = ?",
            (hashlib.sha256(str(pk).encode("utf-8")).hexdigest(), pk),
        )


def _require_legacy_stamps(
    repairs: list[normalize_stamps.Repair],
) -> tuple[normalize_stamps.Repair, ...]:
    """This tier's premise, as a PRESENCE assertion rather than an absence.

    A library that no longer holds unorderable stamps fails here loudly
    instead of leaving every legacy-stamp test passing vacuously against
    clean data. ``real_library_premise.prepared_or_skip`` turns it into a
    skip only for the DEFAULT source, never for one an operator named.
    """
    if not repairs:
        raise LibraryPremiseMissing(
            "the real library carries no unorderable stored stamp; this "
            "tier's premise (a v5 library holds them) no longer holds, so "
            "every quarantine test above it would pass without testing "
            "anything"
        )
    return tuple(repairs)


def prepare_library(source: Path, dest_dir: Path) -> PreparedLibrary:
    """Copy ``source`` under ``dest_dir``, migrate it to v7, repair its stamps.

    For the merge-rule tests, which want production-shaped data and a sync
    set with nothing held out of it.
    """
    target, version = _migrated_copy(source, dest_dir)
    conn = state_db.open_rw(target)
    try:
        repairs = _require_legacy_stamps(normalize_stamps.scan(conn))
        normalize_stamps.apply_repairs(conn, list(repairs))
        remaining = normalize_stamps.scan(conn)
        if remaining:
            raise AssertionError(
                f"{len(remaining)} unorderable stamp(s) survived the repair, "
                f"first: {remaining[0].describe()}"
            )
        _fill_test_identity(conn)
    finally:
        conn.close()
    return PreparedLibrary(
        path=target, schema_version=version, repairs=repairs, repaired=True
    )


def prepare_library_unrepaired(source: Path, dest_dir: Path) -> PreparedLibrary:
    """The same copy, migrated to v7 and NOT repaired.

    For the quarantine tier. Every synthetic fixture in this suite mints
    canonical stamps and :func:`prepare_library` repairs before handing
    anything out, so before round 5 there was no fixture anywhere that could
    put a legacy stamp in a LOCAL table and then apply an incoming row
    against it -- the R10/R12 hole. This builder is that fixture, and it
    asserts the library still carries the stamps rather than assuming it.
    """
    target, version = _migrated_copy(source, dest_dir)
    conn = state_db.open_rw(target)
    try:
        repairs = _require_legacy_stamps(normalize_stamps.scan(conn))
    finally:
        conn.close()
    return PreparedLibrary(
        path=target, schema_version=version, repairs=repairs, repaired=False
    )


# ----- seeding one spoke ------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _seed_subset(
    conn: sqlite3.Connection, machine_id: str, tracks: int
) -> None:
    """Copy the deterministic first ``tracks`` tracks and everything hanging
    off them out of the attached ``lib`` database.

    ``track_locations`` is the one table whose values are rewritten: the
    ``location_id`` is re-minted per spoke and ``machine_id`` /
    ``origin_device_id`` become this machine's, because those rows say "where
    the file lives ON THIS MACHINE" (ADR 08 point 1).
    """
    conn.execute(
        "CREATE TEMP TABLE subset AS "
        "SELECT stable_id FROM lib.tracks ORDER BY stable_id LIMIT ?",
        (tracks,),
    )
    for table in ("tracks", "track_vendor_ids", "track_fields"):
        columns = ", ".join(_columns(conn, table))
        conn.execute(
            f"INSERT INTO {table}({columns}) SELECT {columns} FROM lib.{table} "
            f"WHERE stable_id IN (SELECT stable_id FROM subset)"
        )
    playlist_columns = ", ".join(_columns(conn, "playlists"))
    conn.execute(
        f"INSERT INTO playlists({playlist_columns}) "
        f"SELECT {playlist_columns} FROM lib.playlists WHERE playlist_id IN ("
        f"  SELECT DISTINCT playlist_id FROM lib.playlist_memberships "
        f"  WHERE stable_id IN (SELECT stable_id FROM subset))"
    )
    membership_columns = ", ".join(_columns(conn, "playlist_memberships"))
    conn.execute(
        f"INSERT INTO playlist_memberships({membership_columns}) "
        f"SELECT {membership_columns} FROM lib.playlist_memberships "
        f"WHERE stable_id IN (SELECT stable_id FROM subset)"
    )
    carried = [
        column
        for column in _columns(conn, "track_locations")
        if column not in ("location_id", "machine_id", "origin_device_id")
    ]
    conn.execute(
        f"INSERT INTO track_locations("
        f"location_id, machine_id, origin_device_id, {', '.join(carried)}) "
        f"SELECT lower(hex(randomblob(16))), ?, ?, {', '.join(carried)} "
        f"FROM lib.track_locations "
        f"WHERE stable_id IN (SELECT stable_id FROM subset)",
        (machine_id, machine_id),
    )
    conn.execute("DROP TABLE subset")


@dataclass(frozen=True)
class SeededSpoke:
    """One spoke's data dir and what was actually put in it."""

    data_dir: Path
    machine_id: str
    row_counts: Mapping[str, int]

    @property
    def pushable_rows(self) -> int:
        """Rows one FULL offer from this spoke carries (ADR 04 c5: memberships
        ride their playlist and are not counted)."""
        return sum(self.row_counts[table] for table in SEEDED_TABLES)


def seed_spoke(
    library: Path, data_dir: Path, name: str, tracks: int
) -> SeededSpoke:
    """Build a fresh v7 spoke at ``data_dir`` holding a real library subset.

    The rows arrive UNSTAMPED in the sync sense: no ``local_changelog``
    entries, and the ``updated_at`` values are whatever the library already
    held. That is deliberate and it is the realistic case -- a library that
    predates the changelog is exactly what ``spoke_push``'s full-offer branch
    exists for, and stamping the seed would test a spoke that has never
    existed.
    """
    conn = state_db.open_rw(client.state_db_path(data_dir))
    try:
        identity = machine_identity.register_machine(
            conn, data_dir=Path(data_dir), name=name
        )
        # Attached read-write only because sqlite3 offers no read-only ATTACH
        # without a URI connection. Nothing below writes to ``lib``.
        conn.execute("ATTACH DATABASE ? AS lib", (str(library),))
        try:
            conn.execute("BEGIN")
            try:
                _seed_subset(conn, identity.machine_id, tracks)
            except Exception:
                # Explicit: DETACH below cannot run inside a transaction, so
                # a half-seeded spoke must not be left open here.
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.execute("DETACH DATABASE lib")
        counts = {
            table: int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in protocol.DIGEST_TABLES
        }
    finally:
        conn.close()
    if counts["tracks"] != tracks:
        raise AssertionError(
            f"asked for {tracks} tracks, seeded {counts['tracks']}; the "
            f"library is smaller than the subset size"
        )
    return SeededSpoke(
        data_dir=Path(data_dir), machine_id=identity.machine_id, row_counts=counts
    )


# ----- transport that also weighs the wire -------------------------------------


class CountingTransport:
    """``client.HubTransport`` over ``TestClient``, counting bytes and calls.

    The same real router over the same real ASGI stack that
    ``tests/cloudsync/test_hub_sync.py`` drives; nothing here is a mock of the
    hub. It exists because a payload-size regression guard has to weigh what
    actually crossed the boundary, and a bound asserted against a number
    nobody measured is not a guard.
    """

    def __init__(self, http: TestClient) -> None:
        self._http = http
        self.request_bytes: int = 0
        self.response_bytes: int = 0
        self.requests: int = 0

    def _decoded(self, response: Any, label: str) -> dict[str, Any]:
        self.requests += 1
        self.response_bytes += len(response.content)
        if response.status_code >= 400:
            raise client.SyncTransportError(
                f"{label} -> HTTP {response.status_code}: {response.text}"
            )
        return dict(response.json())

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(dict(payload)).encode("utf-8")
        self.request_bytes += len(body)
        return self._decoded(
            self._http.post(
                path, content=body, headers={"content-type": "application/json"}
            ),
            f"POST {path}",
        )

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        return self._decoded(
            self._http.get(path, params=dict(params)), f"GET {path}"
        )


# ----- the fleet --------------------------------------------------------------


@dataclass(frozen=True)
class RealLibraryFleet:
    """One empty hub plus two spokes, each holding the same real subset.

    Guarantees:

    * the hub's state DB is EMPTY. Bootstrapping the fleet is what the first
      sync does, and a pre-populated hub would skip it.
    * both spokes hold byte-identical domain rows, because both were seeded
      from one library. Their ``track_locations`` differ by design: each row
      carries its own machine's ``machine_id`` and a locally minted
      ``location_id``.
    * neither spoke has synced yet, so ``transport``'s counters are zero and
      the first ``run_sync`` in a test measures a true FIRST sync.
    """

    hub_dir: Path
    spoke_a: SeededSpoke
    spoke_b: SeededSpoke
    transport: CountingTransport
    subset_tracks: int


@contextmanager
def fleet_from(library: Path, root: Path, tracks: int) -> Iterator[RealLibraryFleet]:
    """An empty hub plus two spokes seeded from ``library``.

    One builder for both conftest fixtures, so the repaired and unrepaired
    tiers cannot drift into testing two different fleet shapes.
    """
    hub_dir = root / "hub"
    spoke_a = seed_spoke(library, root / "spoke-a", "spoke-a", tracks)
    spoke_b = seed_spoke(library, root / "spoke-b", "spoke-b", tracks)
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield RealLibraryFleet(
            hub_dir=hub_dir,
            spoke_a=spoke_a,
            spoke_b=spoke_b,
            transport=CountingTransport(http),
            subset_tracks=tracks,
        )


def largest_playlist(conn: sqlite3.Connection) -> tuple[str, str, int]:
    """The seeded playlist with the most members: ``(id, name, size)``.

    Chosen by a RULE, not by name: playlist names are library data and the
    biggest one can change when the library does, but "the playlist with the
    most members in this subset, ties broken by playlist_id" cannot go stale.
    Tests print the name and assert the rule's consequences.
    """
    row = conn.execute(
        "SELECT p.playlist_id, p.name, COUNT(*) AS members "
        "FROM playlists p JOIN playlist_memberships m USING(playlist_id) "
        "GROUP BY p.playlist_id ORDER BY members DESC, p.playlist_id LIMIT 1"
    ).fetchone()
    if row is None:
        raise AssertionError("the seeded subset contains no playlist members")
    return str(row[0]), str(row[1]), int(row[2])


def members_of(conn: sqlite3.Connection, playlist_id: str) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? "
            "ORDER BY position",
            (playlist_id,),
        )
    )


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in protocol.DIGEST_TABLES
    }


__all__ = [
    "DEFAULT_REAL_LIBRARY_STATE_DB",
    "DEFAULT_SUBSET_TRACKS",
    "REAL_LIBRARY_CANDIDATES",
    "REAL_LIBRARY_ENV_VAR",
    "SEEDED_TABLES",
    "UNAVAILABLE_ERRNOS",
    "CountingTransport",
    "PreparedLibrary",
    "RealLibraryFleet",
    "SeededSpoke",
    "first_available",
    "fleet_from",
    "is_readable_state_db",
    "largest_playlist",
    "library_is_available",
    "members_of",
    "prepare_library",
    "prepare_library_unrepaired",
    "seed_spoke",
    "skip_unless_real_library",
    "source_state_db",
    "table_counts",
]
