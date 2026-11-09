"""What this machine knows about its own sync position, as one readout.

Round 4 left the spoke protocol correct and INVISIBLE: ``run_sync`` returned
a rich :class:`apps.sync_hub.client.SyncResult` to whichever pytest called
it, and nothing kept it. A DJ with two laptops could not answer "do my
machines agree?" without reading a log, and neither could an agent.

Three sources, all machine-local, none of them new schema:

* ``sync_state`` -- the watermark per peer (ADR 08 point 3). Says where this
  machine's fence sits, not whether the last attempt worked.
* ``local_changelog`` -- one entry per synced-table write. Counting the
  DISTINCT rows above a peer's fence gives the pending-push backlog with no
  bookkeeping of its own to drift.
* ``<data-dir>/sync-status.json`` -- the outcome of the last attempt,
  written by :func:`record_from_result` from
  :func:`apps.sync_hub.maintenance.sync` (the one funnel for CLI, scheduler,
  and ``POST /api/v1/cloudsync/sync``). Distinct from
  ``cloudsync-status.json`` (CSSTATUS). The DB cannot hold this: a run that
  failed to REACH the hub still has to be recorded, and a digest mismatch
  is precisely the state in which writing more synced rows is the wrong
  move. So it is a file, beside ``machine-id`` and ``hub-generation.json``,
  for the same reason those are files (ADR 05 section 1): it describes THIS
  machine, and a DB restored from another machine must not inherit it.

**Honest denominator** (project house rule) on ``pending_push_rows``: it
counts distinct ``(table_name, row_pk)`` entries in ``local_changelog``
above the fence, which is what :func:`apps.sync_hub.engine.spoke_push`
selects from. The rows actually put on the wire can be FEWER, because a
``playlist_memberships`` entry folds into its parent ``playlists`` row
(ADR 04 c5) and several entries for one row collapse to one change. It is an
upper bound on rows owed, never a wire count, and it is not defined at all
until one sync has completed: before that the next push is a full offer of
the whole library (``Watermark.needs_full_offer``), which no changelog
count describes. ``full_offer_pending`` says so rather than reporting a
number that means something else.

Nothing here writes to the state DB, so it is safe on the read-only
connection ``apps/webui/server/routes/cloudsync.py`` opens.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE, canonical_now
from apps.sync_hub import sync_set

#: Beside ``machine-id`` and ``hub-generation.json`` in the data dir, NOT in
#: ``state/``: Litestream replicates the DB directory, and a record of "this
#: machine's last sync attempt" that travels with a restored database is
#: describing the wrong machine.
STATUS_FILENAME: str = "sync-status.json"

#: Every outcome :func:`outcome_for` can record. An allowlist rather than a
#: free string so a typo in the writer becomes a loud read error instead of
#: a readout that quietly never matches ``"ok"``.
OK_OUTCOME: str = "ok"
OUTCOMES: frozenset[str] = frozenset(
    {
        OK_OUTCOME,
        "digest_mismatch",
        "still_moving",
        "version_mismatch",
        "transport_error",
        "apply_error",
        "protocol_error",
        "error",
    }
)


#: Every value :attr:`FleetHealth.verdict` can take, worst first. The UI and
#: the CLI render the same word off the same computation, so an agent and a
#: human never read two different answers to one question.
VERDICTS: tuple[str, ...] = (
    "diverged",
    "failing",
    "never_synced",
    "unknown",
    # Ranked ABOVE "behind" because the two failures are not comparable in
    # cost: "behind" clears itself on the next sync, "quarantined" needs an
    # operator to run normalize_stamps and clears itself never. A machine
    # holding rows outside its sync set that reported "converged" would be a
    # verdict rendered over a subject it did not measure.
    "quarantined",
    "behind",
    "converged",
)


class SyncHealthError(RuntimeError):
    """The status file could not be read or written. Never guessed at.

    Deliberately not swallowed into a "no runs yet" reading: "this machine
    has never synced" and "this machine's record of syncing is unreadable"
    are different facts, and only one of them is fixed by waiting.
    """


# ----- the last attempt ----------------------------------------------------


@dataclass(frozen=True)
class SyncRun:
    """One completed attempt at :func:`apps.sync_hub.client.run_sync`.

    Every count is copied off the ``SyncResult`` the run returned, so a
    failed run carries zeros AND an ``error_kind``; the two are never
    confused. ``consecutive_failures`` counts attempts since the last
    ``ok``, so a readout can distinguish "one blip" from "offline all week"
    without keeping a history.
    """

    started_at: str
    finished_at: str
    outcome: str
    hub_url: str
    consecutive_failures: int
    hub_machine_id: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    pushed: int = 0
    accepted: int = 0
    rejected: int = 0
    pulled: int = 0
    applied: int = 0
    rounds: int = 0
    digest: str | None = None
    hub_restore_detected: bool = False
    #: True when the last digest comparison was inconclusive because a peer
    #: held rows out of it (quarantine on either side), not a genuine
    #: convergence check. Carried from ``SyncResult.digest_inconclusive`` so
    #: :attr:`FleetHealth.verdict` can refuse to call an inconclusive run
    #: ``converged`` -- a locally clean spoke otherwise satisfies every
    #: OTHER condition for that verdict even though the comparison that
    #: would have caught a real divergence never ran.
    digest_inconclusive: bool = False
    #: Rows the HUB holds outside ITS sync set, from ``SyncResult`` (round 6).
    #: ``None`` means the hub did not report the field, never coerced to 0.
    hub_quarantined: int | None = None
    #: Incoming rows THIS machine refused because the local row they would
    #: be compared against cannot be ordered, from ``SyncResult`` (round 6).
    quarantined_incoming: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise SyncHealthError(f"{self.outcome!r} is not a sync outcome; expected one of {sorted(OUTCOMES)}.")
        if self.consecutive_failures < 0:
            raise SyncHealthError(f"consecutive_failures must be >= 0, got {self.consecutive_failures}")

    @property
    def ok(self) -> bool:
        return self.outcome == OK_OUTCOME

    def to_wire(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "hub_url": self.hub_url,
            "consecutive_failures": self.consecutive_failures,
            "hub_machine_id": self.hub_machine_id,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "pushed": self.pushed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "pulled": self.pulled,
            "applied": self.applied,
            "rounds": self.rounds,
            "digest": self.digest,
            "hub_restore_detected": self.hub_restore_detected,
            "digest_inconclusive": self.digest_inconclusive,
            "hub_quarantined": self.hub_quarantined,
            "quarantined_incoming": self.quarantined_incoming,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any], *, source: str) -> SyncRun:
        """Rebuild a run from its JSON form, refusing anything malformed."""
        missing = [
            key
            for key in ("started_at", "finished_at", "outcome", "hub_url")
            if not isinstance(payload.get(key), str) or not payload[key]
        ]
        if missing:
            raise SyncHealthError(
                f"{source} is missing required string field(s) {missing}; delete it to start a fresh sync record."
            )
        known = {field for field in cls.__dataclass_fields__}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise SyncHealthError(f"{source} carries unknown field(s) {unknown}; refusing to guess what wrote it.")
        return cls(**payload)


def status_path(data_dir: Path) -> Path:
    """Location of the status file for ``data_dir``."""
    return Path(data_dir) / STATUS_FILENAME


def read_run(data_dir: Path) -> SyncRun | None:
    """The last recorded attempt, or None when none has been recorded."""
    path = status_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SyncHealthError(f"cannot read the sync status file {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncHealthError(
            f"{path} is not JSON: {exc}. Delete it to start a fresh sync record; nothing but the readout depends on it."
        ) from exc
    if not isinstance(parsed, dict):
        raise SyncHealthError(f"{path} holds {type(parsed).__name__}, want an object")
    return SyncRun.from_wire(parsed, source=str(path))


def write_run(data_dir: Path, run: SyncRun) -> None:
    """Replace the status file atomically, or raise."""
    path = status_path(data_dir)
    payload = json.dumps(run.to_wire(), sort_keys=True)
    temp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise SyncHealthError(f"cannot write the sync status file {path}: {exc}") from exc


def record_outcome(
    data_dir: Path,
    run: SyncRun,
    *,
    previous: SyncRun | None = None,
) -> SyncRun:
    """Persist ``run`` with its failure streak continued from ``previous``.

    ``previous`` is passed in rather than re-read so a caller holding the
    running streak stays the one source; pass None and the streak is read
    off disk, which is what a one-shot CLI run wants.
    """
    prior = read_run(data_dir) if previous is None else previous
    streak = 0 if run.ok else (0 if prior is None else prior.consecutive_failures) + 1
    stamped = replace(run, consecutive_failures=streak)
    write_run(data_dir, stamped)
    return stamped


# ----- the fleet readout ---------------------------------------------------


@dataclass(frozen=True)
class PeerHealth:
    """This machine's watermark against one peer, plus what it still owes."""

    peer: str
    last_push_seq: int
    last_pull_seq: int
    last_sync_at: str | None
    peer_generation: str | None
    pending_push_rows: int
    full_offer_pending: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "peer": self.peer,
            "last_push_seq": self.last_push_seq,
            "last_pull_seq": self.last_pull_seq,
            "last_sync_at": self.last_sync_at,
            "peer_generation": self.peer_generation,
            "pending_push_rows": self.pending_push_rows,
            "full_offer_pending": self.full_offer_pending,
        }


@dataclass(frozen=True)
class FleetHealth:
    """Everything the CLI and the overview endpoint report, computed once."""

    local_seq: int
    peers: tuple[PeerHealth, ...]
    last_run: SyncRun | None
    #: Rows on THIS machine that are not in the sync set (round 5): a stored
    #: stamp that cannot be ordered, or a row that REFERENCES one -- a held
    #: track takes its locations, vendor ids and fields with it, and a
    #: playlist whose membership names one is held whole. Deliberately on the
    #: fleet readout and NOT on :class:`PeerHealth`: quarantine is a property
    #: of this machine's own rows and is identical against every peer, so
    #: rendering it per peer would print one measurement N times as though it
    #: were N.
    quarantined_rows: int = 0

    @property
    def pending_push_rows(self) -> int | None:
        """Rows owed to the peer that is furthest behind; None with no peer.

        None rather than 0: a machine that has never synced owes its whole
        library to a hub it has not met, and calling that zero would read as
        "converged" on exactly the machine that is not.
        """
        if not self.peers:
            return None
        return max(peer.pending_push_rows for peer in self.peers)

    @property
    def full_offer_pending(self) -> bool:
        """True while any peer's next push is a full-library offer."""
        return any(peer.full_offer_pending for peer in self.peers)

    @property
    def digest_mismatch(self) -> bool:
        """True when the last attempt ended in the ADR 04 c6 alarm.

        ``SyncStillMoving`` is deliberately NOT this: round 3 finding R6
        split concurrent writing from divergence so the corruption alarm
        stays trustworthy, and folding them back together in the readout
        would undo that where a human actually looks.
        """
        return self.last_run is not None and self.last_run.outcome == "digest_mismatch"

    @property
    def verdict(self) -> str:
        """One word for "do my machines agree?". See :data:`VERDICTS`.

        ``unknown`` is a first-class answer, not a fallback: watermarks with
        no recorded outcome mean somebody ran a sync before anything kept
        the result, and reporting that as ``converged`` would be a verdict
        rendered from an unmeasured subject ((private path)).
        The order below is a precedence, worst first, because a machine can
        legitimately be several of these at once and only the worst one is
        worth a DJ's attention before a set.
        """
        if self.digest_mismatch:
            return "diverged"
        if self.last_run is not None and not self.last_run.ok:
            return "failing"
        if not self.peers:
            return "never_synced"
        if self.last_run is None:
            return "unknown"
        # After the last_run-is-None branch, last_run is known. Three
        # independent quarantine signals share one verdict:
        # 1. Local rows outside this machine's sync set.
        # 2. The HUB reported rows outside ITS sync set. Those rows cannot
        #    reach this machine, and this machine cannot see them to count
        #    them in ``quarantined_rows`` -- the hub's own digest is the
        #    only place the number exists. ``digest_inconclusive`` does not
        #    cover it either: if the hub holds an extra quarantined row this
        #    machine simply does not have, both ELIGIBLE-row digests can be
        #    equal and the comparison reads conclusive. So without this the
        #    readout said "converged" about a fleet the hub had just told it
        #    was incomplete. Deliberately ``> 0`` and NOT "not None":
        #    ``None`` means an older hub did not report the field, which is
        #    unknown rather than positive, and treating unknown as a
        #    quarantine here would replace one unmeasured verdict with
        #    another (see ``SyncResult.hub_quarantined`` and
        #    (private path)).
        # 3. The digest comparison that would have caught a real divergence
        #    did not run to a conclusive result (a peer's quarantined rows
        #    were excluded from it); "converged" would be rendered from a
        #    comparison that never happened.
        if self.quarantined_rows > 0 or (self.last_run.hub_quarantined or 0) > 0 or self.last_run.digest_inconclusive:
            return "quarantined"
        if self.full_offer_pending or (self.pending_push_rows or 0) > 0:
            return "behind"
        return "converged"

    def to_wire(self) -> dict[str, Any]:
        """The ONE definition of this readout's JSON shape.

        ``GET /api/v1/cloudsync/overview``'s ``sync`` block carries exactly
        these keys (pinned by a test). Agent-native parity is not two
        serializers that happen to agree today.
        """
        return {
            "verdict": self.verdict,
            "local_seq": self.local_seq,
            "pending_push_rows": self.pending_push_rows,
            "full_offer_pending": self.full_offer_pending,
            "digest_mismatch": self.digest_mismatch,
            "quarantined_rows": self.quarantined_rows,
            "peers": [peer.to_wire() for peer in self.peers],
            "last_run": None if self.last_run is None else self.last_run.to_wire(),
        }


def _require_table(conn: sqlite3.Connection, name: str) -> None:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    if row is None:
        raise SyncHealthError(
            f"this database has no {name!r} table, so it predates schema v7 "
            f"(CloudSync); run apps.shared.state.schema.apply_migrations."
        )


def local_seq(conn: sqlite3.Connection) -> int:
    """Greatest ``local_changelog.seq``: this machine's own write counter.

    Duplicated from :func:`apps.sync_hub.engine_watermark.local_seq` in
    intent only -- this module must stay importable from a READ-ONLY
    connection, and the engine is the write path. Both read the same one
    column of the same table.
    """
    row = conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {LOCAL_CHANGELOG_TABLE}").fetchone()
    return 0 if row is None else int(row[0])


def pending_push_rows(conn: sqlite3.Connection, since_seq: int) -> int:
    """Distinct rows recorded above ``since_seq``. See the honest-denominator
    note in the module docstring: an upper bound on what a push would offer.
    """
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT table_name, row_pk
            FROM {LOCAL_CHANGELOG_TABLE}
            WHERE seq > ?
        )
        """,
        (int(since_seq),),
    ).fetchone()
    return 0 if row is None else int(row[0])


def quarantined_rows(conn: sqlite3.Connection) -> int:
    """Rows this machine holds OUT of the sync set, counted once each.

    ROWS, not values: a row carrying an unorderable ``updated_at`` AND an
    unorderable ``deleted_at`` is one row missing from the sync set, and
    counting it twice would make the readout disagree with the digest's own
    per-table count.

    Delegates to :func:`apps.sync_hub.sync_set.excluded_counts`, which is the
    same predicate :func:`apps.sync_hub.protocol.table_digest` hashes
    against, so the number here and the number the digest reports are one
    computation asked twice rather than two that happen to agree. They did
    NOT agree while this counted stamp faults itself: on the real library it
    read 40 where the digest excluded 82, because a stamp-only count knows
    nothing of the FK cascade or of a membership bundle that holds its whole
    playlist back. The panel it feeds says "rows that are NOT in the sync
    set", so the smaller number was answering a different question than the
    one it was labelled with.
    """
    return sum(sync_set.excluded_counts(conn).values())


def fleet_health(conn: sqlite3.Connection, data_dir: Path) -> FleetHealth:
    """Watermarks, backlog and last outcome for this machine, in one read.

    Raises :class:`SyncHealthError` when the DB predates schema v7 or the
    status file is unreadable. Neither is reported as an empty readout: an
    absence that a caller cannot tell from "nothing has happened yet" is the
    failure mode this whole module exists to remove.
    """
    _require_table(conn, "sync_state")
    _require_table(conn, LOCAL_CHANGELOG_TABLE)
    rows = conn.execute(
        "SELECT peer, last_push_seq, last_pull_seq, last_sync_at, peer_generation FROM sync_state ORDER BY peer"
    ).fetchall()
    peers = tuple(
        PeerHealth(
            peer=str(row[0]),
            last_push_seq=int(row[1]),
            last_pull_seq=int(row[2]),
            last_sync_at=None if row[3] is None else str(row[3]),
            peer_generation=None if row[4] is None else str(row[4]),
            pending_push_rows=pending_push_rows(conn, int(row[1])),
            full_offer_pending=row[3] is None,
        )
        for row in rows
    )
    return FleetHealth(
        local_seq=local_seq(conn),
        peers=peers,
        last_run=read_run(Path(data_dir)),
        quarantined_rows=quarantined_rows(conn),
    )


def outcome_for(exc: BaseException) -> str:
    """Map a ``run_sync`` failure onto :data:`OUTCOMES`."""
    from apps.sync_hub import client

    if isinstance(exc, client.SyncDigestMismatch):
        return "digest_mismatch"
    if isinstance(exc, client.SyncStillMoving):
        return "still_moving"
    if isinstance(exc, client.SyncVersionMismatch):
        return "version_mismatch"
    if isinstance(exc, client.SyncTransportError):
        return "transport_error"
    if isinstance(exc, client.SyncApplyError):
        return "apply_error"
    if isinstance(exc, client.SyncProtocolError):
        return "protocol_error"
    return "error"


def record_from_result(
    data_dir: Path,
    started_at: str,
    hub_url: str,
    *,
    result: Any | None = None,
    exc: BaseException | None = None,
) -> SyncRun:
    """Persist one ``run_sync`` attempt as a :class:`SyncRun`.

    ``maintenance.sync`` is the one funnel; this keeps that module from
    growing a second serializer. Pass ``result`` on success or ``exc`` on
    failure. Does not record :class:`SyncDeferredError` -- that raises
    before the flock, same as CSSTATUS.
    """
    finished_at = canonical_now()
    if exc is not None:
        return record_outcome(
            data_dir,
            SyncRun(
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome_for(exc),
                hub_url=hub_url,
                consecutive_failures=0,
                error_kind=type(exc).__name__,
                error_message=str(exc) or None,
            ),
        )
    if result is None:
        raise SyncHealthError("record_from_result needs result= or exc=; got neither")
    return record_outcome(
        data_dir,
        SyncRun(
            started_at=started_at,
            finished_at=finished_at,
            outcome=OK_OUTCOME,
            hub_url=hub_url,
            consecutive_failures=0,
            hub_machine_id=result.hub_machine_id,
            pushed=result.pushed,
            accepted=result.accepted,
            rejected=result.rejected,
            pulled=result.pulled,
            applied=result.applied,
            rounds=result.rounds,
            digest=result.digest,
            hub_restore_detected=result.hub_restore_detected,
            digest_inconclusive=result.digest_inconclusive,
            hub_quarantined=result.hub_quarantined,
            quarantined_incoming=result.quarantined_incoming,
        ),
    )


__all__ = [
    "OK_OUTCOME",
    "OUTCOMES",
    "STATUS_FILENAME",
    "VERDICTS",
    "FleetHealth",
    "PeerHealth",
    "SyncHealthError",
    "SyncRun",
    "fleet_health",
    "local_seq",
    "outcome_for",
    "pending_push_rows",
    "quarantined_rows",
    "read_run",
    "record_from_result",
    "record_outcome",
    "status_path",
    "write_run",
]
