"""The spoke half of the stale-copy guard, and its remedy.

Contract: :mod:`apps.sync_hub.stale_copy` (CLOUDSYNC-30, issue #4628).

Before every push, :func:`refuse_a_stale_copy_push` asks the hub
(``POST /stale-check``) about every live ``tracks`` row in the offer that
another machine authored. Rows this machine wrote itself are never asked
about, so an ordinary sync, whose fenced offer holds only local writes, makes
no extra request. A copy of somebody else's library is the case that carries
foreign rows, and it is refused here with every orphan named, before
anything is pushed.

The refusal is not a dead end. ``python -m apps.sync_hub stale-tracks``
lists the same orphans and resolves them one of two ways:

* ``--remove``: the fleet dropped them and so does this machine. Each is
  removed from the library here (``StateWriter.remove_from_library``,
  reversible with undelete), and that tombstone, authored by this machine,
  is what the next sync sends: the hub then HOLDS the deletion, so no other
  copy can bring the track back either (#4628 criterion 2).
* ``--keep``: the user wants them back. Each is re-stamped as this machine's
  own write (``StateWriter.claim_track``), which the guard passes, and the
  next sync returns them to the fleet.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.sync_hub import protocol, protocol_common, spoke_credential, stale_copy, wire_version
from apps.sync_hub.client_transport_ops import state_db_path
from apps.sync_hub.service_models import STALE_CHECK_MAX_CANDIDATES
from apps.sync_hub.transport import API_PREFIX, HttpTransport, HubTransport, SyncTransportError

log = logging.getLogger("apps.sync_hub.client")

#: Exit codes of ``stale-tracks``. 0 also covers "nothing is stale".
EXIT_STALE_LISTED: int = 3


class SyncStaleTracks(RuntimeError):
    """This library holds tracks the fleet dropped. Nothing was pushed."""

    def __init__(self, hub_machine_id: str, orphans: Sequence[str]) -> None:
        self.hub_machine_id = hub_machine_id
        self.orphans = tuple(orphans)
        super().__init__(
            f"hub {hub_machine_id}: "
            + stale_copy.refusal_message(len(self.orphans), self.orphans[: stale_copy.EXAMPLES])
        )


# -----------------------------------------------------------------------------
# asking the hub
# -----------------------------------------------------------------------------


def foreign_candidates(
    rows: Sequence[protocol.RowChange], machine_id: str
) -> list[stale_copy.Candidate]:
    """Live ``tracks`` rows in ``rows`` that a machine other than this one wrote."""
    return [
        candidate
        for candidate in stale_copy.candidates_from_changes(rows)
        if candidate.origin_device_id not in (protocol.NO_ORIGIN, machine_id)
    ]


def check(
    channel: HubTransport,
    machine_id: str,
    candidates: Sequence[stale_copy.Candidate],
    *,
    reseed: bool,
) -> stale_copy.Verdict:
    """The hub's verdict on ``candidates``, asked in chunks the hub accepts."""
    orphans: list[str] = []
    unattributable = 0
    for start in range(0, len(candidates), STALE_CHECK_MAX_CANDIDATES):
        chunk = candidates[start : start + STALE_CHECK_MAX_CANDIDATES]
        payload = channel.post(
            f"{API_PREFIX}/stale-check",
            {
                "machine_id": machine_id,
                "schema_version": state_schema.SCHEMA_VERSION,
                "wire_version": wire_version.WIRE_VERSION,
                "candidates": [
                    {
                        "stable_id": candidate.stable_id,
                        "origin_device_id": candidate.origin_device_id,
                    }
                    for candidate in chunk
                ],
                "reseed": reseed,
            },
        )
        found = payload.get("orphans")
        counted = payload.get("unattributable")
        if not isinstance(found, list) or not isinstance(counted, int):
            raise SyncTransportError(
                "stale-check response lacks an 'orphans' list or an integer "
                "'unattributable'; the guard could not decide, so nothing is pushed"
            )
        orphans.extend(str(stable_id) for stable_id in found)
        unattributable += counted
    return stale_copy.Verdict(orphans=tuple(sorted(set(orphans))), unattributable=unattributable)


def refuse_a_stale_copy_push(
    channel: HubTransport,
    machine_id: str,
    hub_machine_id: str,
    rows: Sequence[protocol.RowChange],
    *,
    reseed: bool,
) -> None:
    """Halt BEFORE a push that would bring back tracks the fleet dropped."""
    candidates = foreign_candidates(rows, machine_id)
    if not candidates:
        return
    verdict = check(channel, machine_id, candidates, reseed=reseed)
    if verdict.unattributable:
        log.warning(
            "stale-copy guard: %d offered track(s) name an author the hub does "
            "not know, so the guard could not decide them; they are offered "
            "as before (issue #4628).",
            verdict.unattributable,
        )
    if verdict.orphans:
        raise SyncStaleTracks(hub_machine_id, verdict.orphans)


def raise_if_stale_library(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    hub_machine_id: str,
    mismatch: Exception,
) -> None:
    """Re-raise a ``tracks`` digest mismatch as :class:`SyncStaleTracks` when it is one.

    Asks only after a settled mismatch, so a healthy sync pays nothing. Every
    live track another machine wrote is a candidate, because the offer may
    not have carried any of them. Any other divergence is left to raise as
    itself.
    """
    digests = getattr(mismatch, "digests", None)
    if digests is None:
        return
    local_digest, remote_digest = digests
    if protocol_common.TRACKS_TABLE not in local_digest.divergent_tables(remote_digest):
        return
    candidates = local_foreign_candidates(conn, machine_id)
    if not candidates:
        return
    verdict = check(channel, machine_id, candidates, reseed=False)
    if verdict.orphans:
        raise SyncStaleTracks(hub_machine_id, verdict.orphans) from mismatch


# -----------------------------------------------------------------------------
# the remedy
# -----------------------------------------------------------------------------


def local_foreign_candidates(
    conn: sqlite3.Connection, machine_id: str
) -> list[stale_copy.Candidate]:
    """Every live track in this library that another machine wrote."""
    return [
        stale_copy.Candidate(stable_id=str(stable_id), origin_device_id=str(origin))
        for stable_id, origin in conn.execute(
            "SELECT stable_id, origin_device_id FROM tracks "
            "WHERE deleted_at IS NULL AND origin_device_id IS NOT NULL "
            "AND origin_device_id NOT IN (?, ?) ORDER BY stable_id",
            (protocol.NO_ORIGIN, machine_id),
        )
    ]


def resolve(
    data_dir: Path,
    channel: HubTransport,
    *,
    action: str | None,
    bus: EventBus | FakeEventBus | None = None,
) -> tuple[str, ...]:
    """List this library's orphans; ``remove`` or ``keep`` them when asked.

    Returns the orphans the hub named. The hub must already know this
    machine, which the refused sync's ``hello`` arranged.
    """
    conn = state_db.open_rw(state_db_path(Path(data_dir)))
    try:
        machine_id = machine_identity.get_or_create_machine_id(Path(data_dir))
        candidates = local_foreign_candidates(conn, machine_id)
        verdict = check(channel, machine_id, candidates, reseed=False)
        if action is None or not verdict.orphans:
            return verdict.orphans
        writer = StateWriter(conn, bus=bus, actor="stale-tracks")
        try:
            for stable_id in verdict.orphans:
                if action == "remove":
                    writer.remove_from_library(stable_id)
                elif action == "keep":
                    writer.claim_track(stable_id)
                else:
                    raise ValueError(f"unknown stale-tracks action {action!r}")
        finally:
            writer.close()
        return verdict.orphans
    finally:
        conn.close()


def add_parser(subcommands: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    command = subcommands.add_parser(
        "stale-tracks",
        parents=[common],
        help="list tracks the fleet dropped that this library still holds; --remove or --keep them",
    )
    command.add_argument("--hub", required=True, help="the hub base URL")
    choice = command.add_mutually_exclusive_group()
    choice.add_argument(
        "--remove",
        dest="action",
        action="store_const",
        const="remove",
        help="remove them from this library too (reversible with undelete)",
    )
    choice.add_argument(
        "--keep",
        dest="action",
        action="store_const",
        const="keep",
        help="claim them as this machine's own, so the next sync restores them fleet-wide",
    )


def run_cli(args: argparse.Namespace, *, transport: HubTransport | None = None) -> int:
    """``stale-tracks``: print the orphans and what was done with them."""
    channel = transport or HttpTransport(
        args.hub, bearer=spoke_credential.read_credential(Path(args.data_dir))
    )
    orphans = resolve(Path(args.data_dir), channel, action=args.action)
    for stable_id in orphans:
        print(stable_id)
    if not orphans:
        print("no stale tracks: the hub holds every track this library got from the fleet")
        return 0
    if args.action is None:
        print(
            f"{len(orphans)} stale track(s). Re-run with --remove or --keep; "
            f"nothing was changed."
        )
        return EXIT_STALE_LISTED
    print(f"{len(orphans)} stale track(s): {args.action} done. Sync again.")
    return 0


__all__ = [
    "EXIT_STALE_LISTED",
    "SyncStaleTracks",
    "add_parser",
    "check",
    "foreign_candidates",
    "local_foreign_candidates",
    "raise_if_stale_library",
    "refuse_a_stale_copy_push",
    "resolve",
    "run_cli",
]
