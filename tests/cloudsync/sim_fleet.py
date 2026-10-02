"""Stateful property simulation of a CloudSync fleet (plan W18).

A hypothesis ``RuleBasedStateMachine`` over 2 to 4 spokes and ONE real hub:
the real ``apps.sync_hub`` router behind a ``TestClient``, real migrated
state DBs, the real ``client.run_sync`` round trip, and the real
:class:`~tests.cloudsync.fault_transport.FaultTransport` for network
failures. It reuses the scenario harness's ``SimRun`` and verbs rather than
growing a second copy of how a writer stamps a row. Nothing is mocked.

Rules: edit a title, soft delete, reinsert, reorder a playlist, sync, sync
through an injected fault, partition/heal a spoke, skew a spoke's clock,
snapshot/restore the hub, prune a changelog.

After EVERY step (:meth:`FleetSim.fleet_invariants`):

* the version LWW says wins is still held by at least one machine;
* every row any machine holds is exactly a write the simulation made;
* the hub still holds every version a completed sync acknowledged (reset to
  the snapshot's acknowledgements when the hub is restored);
* no spoke's push fence moved backwards within one hub generation.

Every example ends with the same deterministic epilogue
(:meth:`FleetSim._epilogue`), because the derandomized search alone reached
the fence-critical and restore steps in 0 of 50 examples on some revisions:
heal everything, prune the hub's changelog, make sure some spoke holds rows
above its push fence and lose a push both ways, sync every spoke twice with
no faults, and COMPARE: every spoke and the hub hold byte-identical sync sets
(all digest tables, tombstones included, in both directions) and every
machine holds exactly the oracle's winner per row. Then rewind the hub to its
empty snapshot, sync every spoke once (each must detect the rewind), and
compare again.

Envelope: a write whose stamp is at or below the version its own spoke
already holds is SKIPPED and counted, never made. That write is the logged
P2 skew wedge (``.planning/debt/1993.md``): the hub rejects it and the spoke
raises ``SyncDigestMismatch`` on every retry, pending an owner decision on
a skew tolerance. :class:`UnguardedFleetSim` lifts the guard, and the
positive control in ``test_sim_property`` shows the simulation finds the
wedge when it does.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from hypothesis import event, note
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from apps.shared.state import sync_stamp
from apps.sync_hub import client, protocol

from .fault_transport import FaultInjected, FaultTransport
from .scenario_runner_actions import _apply_edit, _apply_reorder, _seed
from .scenario_runner_adversarial import (
    _apply_delete,
    _apply_prune,
    _apply_reinsert,
    _apply_restore_hub,
)
from .scenario_runner_common import SimRun, _sim_run
from .sim_fleet_setup import (
    FAULT_MODES,
    FAULT_PATHS,
    HUB,
    MAX_SNAPSHOTS,
    MAX_SPOKES,
    MIN_SPOKES,
    PICK,
    PLAYLIST_ID,
    SETTLE_PASSES,
    SKEW_SECONDS,
    SPOKE,
    TITLES,
    TRACK_IDS,
    MachineView,
    fleet_scenario,
    machine_view,
    seed_versions,
    spoke_clock_offset,
)
from .sim_oracle import PLAYLISTS, TRACKS, LwwKey, LwwOracle, RowKey, Version, read_versions

_T = TypeVar("_T")
#: ``(spoke, stable_id)``: one spoke's own copy of one track.
SpokeTrack = tuple[str, str]


class FleetSim(RuleBasedStateMachine):
    """N spokes, one real hub, and a pure LWW oracle watching every write."""

    #: Skip a write stamped at or below its own spoke's copy (the P2 skew wedge).
    guard_stranded_writes: ClassVar[bool] = True
    #: What the run actually exercised, accumulated across examples, so a
    #: caller can assert on PRESENCE (faults fired, restores detected) rather
    #: than read a green run as proof.
    stats: ClassVar[Counter[str]] = Counter()

    def __init__(self) -> None:
        super().__init__()
        self._stack = ExitStack()
        self._run: SimRun | None = None
        self._readers: dict[str, sqlite3.Connection] = {}
        self.oracle = LwwOracle()
        self.spokes: list[str] = []
        self.offline: set[str] = set()
        self.acked_on_hub: dict[RowKey, LwwKey] = {}
        #: ``(name, hub seq when taken, acknowledgements when taken)``.
        self.snapshots: list[tuple[str, int, dict[RowKey, LwwKey]]] = []
        self.hub_seq: int = 0
        self.fences: dict[tuple[str, str, str | None], int] = {}
        # Refreshed by fleet_invariants after every step; they gate delete and
        # reinsert so neither rule spends a step finding nothing to act on.
        self.live: list[SpokeTrack] = []
        self.tombstoned: list[SpokeTrack] = []
        #: Spokes past their first sync holding changelog entries above their
        #: push fence: the only spokes where losing a push can lose a row.
        self.unpushed: list[str] = []

    # ----- helpers ------------------------------------------------------------

    @property
    def run(self) -> SimRun:
        if self._run is None:
            raise RuntimeError("the fleet is used before build_fleet ran")
        return self._run

    def _count(self, what: str) -> None:
        self.stats[what] += 1
        event(what)

    def _spoke(self, index: int) -> str:
        return self.spokes[index % len(self.spokes)]

    def _read(self, machine: str, reader: Callable[[sqlite3.Connection], _T]) -> _T:
        """Read ``machine``'s DB through its read-only connection for this example.

        Not ``SimRun.conn``: that opens read-write and checks the migration
        ladder on every open, and the invariants read every machine after
        every step (a cProfile of the ci profile put 10,354 opens at 6.85s of
        19.9s). One connection per machine, held in autocommit so every SELECT
        sees the latest commit, including a hub restore, and closed by the
        ExitStack before the temp dir is removed.
        """
        conn = self._readers.get(machine)
        if conn is None:
            path = client.state_db_path(self.run.data_dirs[machine])
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            self._stack.callback(conn.close)
            self._readers[machine] = conn
        return reader(conn)

    def _views(self) -> dict[str, MachineView]:
        return {machine: self._read(machine, machine_view) for machine in (HUB, *self.spokes)}

    def _versions(self, machine: str) -> dict[RowKey, Version]:
        return self._read(machine, read_versions)

    def _write(
        self,
        label: str,
        machine: str,
        row: RowKey,
        content_for: Callable[[str], tuple[Any, ...]],
        apply: Callable[[str], None],
    ) -> bool:
        """Stamp one write on ``machine``'s clock, make it, and tell the oracle.

        Returns whether the write was made: False when the guard skipped it.
        """
        held = self._versions(machine)[row]
        stamp = self.run.next_tick(machine)
        key = (sync_stamp.to_canonical(stamp), self.run.machine_ids[machine])
        if self.guard_stranded_writes and key <= held.key:
            note(f"skipped stranded write on {machine} {row}: {key} <= held {held.key}")
            self._count("skipped: stranded write (debt 1993 skew wedge)")
            return False
        apply(stamp)
        self.oracle.record(row, Version(key, content_for(key[0])))
        self._count(f"write: {label}")
        return True

    def _sync(self, machine: str, transport: FaultTransport) -> client.SyncResult:
        result = client.run_sync(
            self.run.data_dirs[machine], "http://hub.invalid", transport=transport, name=machine
        )
        assert result.quarantined == 0 and not result.digest_inconclusive, (
            f"{machine} synced with a quarantine or an inconclusive digest: {result}"
        )
        # run_sync returned, so this spoke and the hub hold the same sync set:
        # every version this spoke holds is now acknowledged by the hub.
        for row, version in self._versions(machine).items():
            self.acked_on_hub[row] = max(self.acked_on_hub.get(row, version.key), version.key)
        self._count(
            "sync: completed" + (" after hub restore" if result.hub_restore_detected else "")
        )
        return result

    def _partitioned_sync(self, machine: str) -> None:
        transport = FaultTransport(self.run.hub_transport)
        transport.fail_before_send(path="hello")
        try:
            client.run_sync(
                self.run.data_dirs[machine], "http://hub.invalid", transport=transport, name=machine
            )
        except FaultInjected:
            self._count("sync: refused while partitioned")
            return
        raise AssertionError(f"{machine} is partitioned but its sync reached the hub")

    # ----- setup ----------------------------------------------------------------

    @initialize(spoke_count=st.integers(MIN_SPOKES, MAX_SPOKES))
    def build_fleet(self, spoke_count: int) -> None:
        tmp = Path(self._stack.enter_context(tempfile.TemporaryDirectory(prefix="cloudsync-sim-")))
        self.spokes = [f"spoke-{index}" for index in range(spoke_count)]
        self._run = self._stack.enter_context(_sim_run(fleet_scenario(self.spokes), tmp))
        _seed(self._run)
        for row, version in seed_versions().items():
            self.oracle.record(row, version)
        for index, machine in enumerate(self.spokes):
            self.run.clock_skew_s[machine] = spoke_clock_offset(index)
        self.live = [(machine, stable_id) for machine in self.spokes for stable_id in TRACK_IDS]
        # An empty-hub snapshot from the start, so restore_hub is enabled as
        # soon as the first sync moves the hub. Without it the derandomized
        # search restored the hub in 0 of 50 examples on one run.
        self.snapshot_hub()

    # ----- writes -----------------------------------------------------------------

    @rule(spoke=SPOKE, track=st.sampled_from(TRACK_IDS), title=st.sampled_from(TITLES))
    def edit_title(self, spoke: int, track: str, title: str) -> None:
        machine = self._spoke(spoke)
        row = (TRACKS, track)
        _old_title, deleted_at, restored_at = self._versions(machine)[row].content
        self._write(
            "edit",
            machine,
            row,
            lambda _stamp: (title, deleted_at, restored_at),
            lambda stamp: _apply_edit(
                self.run,
                machine,
                {"table": TRACKS, "stable_id": track, "set": {"title": title}, "updated_at": stamp},
            ),
        )

    def _tombstone_write(self, candidates: list[SpokeTrack], pick: int, *, deleting: bool) -> bool:
        machine, track = candidates[pick % len(candidates)]
        row = (TRACKS, track)
        title, _deleted_at, restored_at = self._versions(machine)[row].content
        verb = _apply_delete if deleting else _apply_reinsert
        return self._write(
            "delete" if deleting else "reinsert",
            machine,
            row,
            # A reinsert is the explicit restore: it stamps restored_at, which
            # is what lets it outrank the tombstone (CLOUDSYNC-29).
            lambda stamp: (title, stamp, restored_at) if deleting else (title, None, stamp),
            lambda stamp: verb(
                self.run,
                machine,
                {"table": TRACKS, "pk": {"stable_id": track}, "updated_at": stamp},
            ),
        )

    @precondition(lambda self: bool(self.live))
    @rule(pick=PICK)
    def delete(self, pick: int) -> None:
        self._tombstone_write(self.live, pick, deleting=True)

    @precondition(lambda self: bool(self.tombstoned))
    @rule(pick=PICK)
    def reinsert(self, pick: int) -> None:
        self._tombstone_write(self.tombstoned, pick, deleting=False)

    @rule(spoke=SPOKE, members=st.permutations(TRACK_IDS), keep=st.integers(1, len(TRACK_IDS)))
    def reorder(self, spoke: int, members: list[str], keep: int) -> None:
        machine = self._spoke(spoke)
        row = (PLAYLISTS, PLAYLIST_ID)
        chosen = tuple(members[:keep])
        name, deleted_at, _old = self._versions(machine)[row].content
        self._write(
            "reorder",
            machine,
            row,
            lambda _stamp: (name, deleted_at, chosen),
            lambda stamp: _apply_reorder(
                self.run,
                machine,
                {"playlist_id": PLAYLIST_ID, "members": list(chosen), "updated_at": stamp},
            ),
        )

    # ----- network ------------------------------------------------------------------

    @rule(spoke=SPOKE)
    def sync(self, spoke: int) -> None:
        machine = self._spoke(spoke)
        if machine in self.offline:
            self._partitioned_sync(machine)
            return
        self._sync(machine, FaultTransport(self.run.hub_transport))

    @rule(spoke=SPOKE, path=st.sampled_from(FAULT_PATHS), mode=st.sampled_from(FAULT_MODES))
    def sync_through_fault(self, spoke: int, path: str, mode: str) -> None:
        machine = self._spoke(spoke)
        if machine in self.offline:
            self._partitioned_sync(machine)
            return
        transport = FaultTransport(self.run.hub_transport)
        transport.fail_on_nth(path, 1, mode=mode)  # type: ignore[arg-type]
        try:
            self._sync(machine, transport)
        except FaultInjected:
            self._count(f"fault fired: {mode} on {path}")
            return
        # A sync with nothing to offer sends no /push, so a push fault can go
        # unreached. Counted, so the ci test can require that faults DID fire.
        self._count(f"fault not reached: {path}")

    def _unpushed_online(self) -> list[str]:
        return [machine for machine in self.unpushed if machine not in self.offline]

    def _pick_unpushed(self, pick: int) -> str:
        candidates = self._unpushed_online()
        return candidates[pick % len(candidates)]

    @precondition(lambda self: bool(self._unpushed_online()))
    @rule(pick=PICK)
    def lose_push_before_send(self, pick: int) -> None:
        """A push that MUST carry rows dies before the hub sees it."""
        self._lose_push(self._pick_unpushed(pick), "fail_before_send")

    @precondition(lambda self: bool(self._unpushed_online()))
    @rule(pick=PICK)
    def lose_push_after_commit(self, pick: int) -> None:
        """A push that MUST carry rows commits on the hub; its answer is lost."""
        self._lose_push(self._pick_unpushed(pick), "drop_response_after_commit")

    def _lose_push(self, machine: str, mode: str) -> None:
        """Lose one push that must carry rows (the fence's reason to exist).

        Targeted rather than left to ``sync_through_fault``: a push lost on a
        spoke's FIRST sync is re-sent by the next full offer anyway, so only a
        push past that point can test the fence (ADR 08 point 3). The rules
        above place it mid-run; :meth:`_lose_pushes_before_settling` runs it
        at the end of EVERY example, because the derandomized ci search alone
        reached the ``fail_before_send`` rule in 0 of 56 examples (hypothesis
        favors the first-sorted rules and the lowest choice indices).
        """
        transport = FaultTransport(self.run.hub_transport)
        transport.fail_on_nth("push", 1, mode=mode)  # type: ignore[arg-type]
        try:
            self._sync(machine, transport)
        except FaultInjected:
            # A key of its own: a push fault on a FIRST sync (the generic rule)
            # cannot lose a row, so only this count proves the fence was tested.
            self._count(f"fence test: push lost ({mode})")
            return
        raise AssertionError(
            f"{machine} held changelog entries above its push fence, yet its sync sent "
            f"no /push: {transport.calls}"
        )

    @rule(spoke=SPOKE)
    def partition(self, spoke: int) -> None:
        machine = self._spoke(spoke)
        if machine in self.offline:
            self.offline.discard(machine)
            self._count("partition: healed")
        else:
            self.offline.add(machine)
            self._count("partition: cut")

    @rule(spoke=SPOKE, seconds=st.sampled_from(SKEW_SECONDS))
    def skew(self, spoke: int, seconds: float) -> None:
        machine = self._spoke(spoke)
        offset = spoke_clock_offset(self.spokes.index(machine))
        self.run.clock_skew_s[machine] = seconds + offset
        self._count("clock skewed" if seconds else "clock reset")

    # ----- hub maintenance --------------------------------------------------------

    @precondition(lambda self: len(self.snapshots) < MAX_SNAPSHOTS)
    @rule()
    def snapshot_hub(self) -> None:
        name = f"snap-{len(self.snapshots)}"
        _apply_restore_hub(self.run, HUB, {"op": "snapshot", "name": name})
        self.snapshots.append((name, self.hub_seq, dict(self.acked_on_hub)))
        self._count("hub snapshot")

    def _rewinds(self) -> list[tuple[str, int, dict[RowKey, LwwKey]]]:
        """Snapshots the hub has moved past: restoring one really rewinds it."""
        return [snap for snap in self.snapshots if snap[1] < self.hub_seq]

    @precondition(lambda self: bool(self._rewinds()))
    @rule(pick=PICK)
    def restore_hub(self, pick: int) -> None:
        rewinds = self._rewinds()
        name, _seq, acked_then = rewinds[pick % len(rewinds)]
        _apply_restore_hub(self.run, HUB, {"op": "restore", "name": name})
        # The hub now holds exactly what it held at the snapshot, which covered
        # what had been acknowledged by then, and nothing acknowledged since.
        self.acked_on_hub = dict(acked_then)
        self._count("hub restored")

    @rule(on=st.integers(-1, MAX_SPOKES - 1))
    def prune(self, on: int) -> None:
        """``on`` -1 prunes the hub's changelog, 0 and up a spoke's."""
        machine = HUB if on < 0 else self._spoke(on)
        _apply_prune(self.run, machine, {"keep_days": 0, "keep_rows": 0})
        self._count("prune: hub" if machine == HUB else "prune: spoke")

    # ----- invariants -----------------------------------------------------------------

    @invariant()
    def fleet_invariants(self) -> None:
        if self._run is None:
            return
        views = self._views()
        self.hub_seq = views[HUB].hub_seq
        for row in self.oracle.rows():
            held = [view.versions[row] for view in views.values() if row in view.versions]
            winner = self.oracle.winner(row)
            # "Holds the winner", not "max stamp is the winner's": a track
            # tombstone wins over a later-stamped live edit (CLOUDSYNC-29),
            # so the greatest held stamp need not be the winning version.
            assert winner in held, (
                f"no machine holds the winning version of {row}: winner {winner}, "
                f"held {sorted(version.key for version in held)}"
            )
        for machine, view in views.items():
            for row, version in view.versions.items():
                assert self.oracle.is_known(row, version), (
                    f"{machine} holds {row} = {version}, which no write ever made"
                )
        for row, acked in self.acked_on_hub.items():
            stored = views[HUB].versions.get(row)
            assert stored is not None and stored.key >= acked, (
                f"the hub lost an acknowledged version of {row}: acked {acked}, holds {stored}"
            )
        self._check_fences(views)
        pairs = [(machine, stable_id) for machine in self.spokes for stable_id in TRACK_IDS]
        self.tombstoned = [
            pair
            for pair in pairs
            if views[pair[0]].versions[(TRACKS, pair[1])].content[1] is not None
        ]
        self.live = [pair for pair in pairs if pair not in self.tombstoned]

    def _check_fences(self, views: dict[str, MachineView]) -> None:
        """No push fence moves backwards within a generation; refresh ``unpushed``."""
        unpushed: list[str] = []
        for machine in self.spokes:
            view = views[machine]
            for peer, last_push_seq, generation, last_sync_at in view.fence_rows:
                fence_key = (machine, str(peer), generation)
                previous = self.fences.get(fence_key, 0)
                assert int(last_push_seq) >= previous, (
                    f"{machine}'s push fence against {peer} (generation {generation}) "
                    f"went backwards: {previous} -> {last_push_seq}"
                )
                self.fences[fence_key] = int(last_push_seq)
                if last_sync_at is not None and view.local_seq > int(last_push_seq):
                    unpushed.append(machine)
        self.unpushed = unpushed

    # ----- the epilogue: every example ends the same adversarial way ------------------

    def teardown(self) -> None:
        try:
            if self._run is not None:
                self._epilogue()
        finally:
            self._stack.close()

    def _epilogue(self) -> None:
        """Heal; prune the hub; lose a push both ways; settle and compare; rewind; compare.

        The random rules decide the history, this decides how EVERY history
        ends, so the fence, the prune and the restore belt are tested in every
        example rather than when the derandomized search happens to draw them:
        on some revisions a 50-example ci run drew the fail_before_send rule
        and a hub restore 0 times each. The rewind comes AFTER the first
        comparison on purpose: a restore forces a full re-offer, which would
        re-send any row a broken fence had stepped over and hide the loss.
        """
        self.offline.clear()
        _apply_prune(self.run, HUB, {"keep_days": 0, "keep_rows": 0})
        self._count("prune: hub")
        self._ensure_unpushed_rows()
        self._lose_pushes_before_settling()
        self._settle()
        self._compare("after settling")
        self._rewind_hub_and_resync()
        self._compare("after a hub rewind")

    def _ensure_unpushed_rows(self) -> None:
        """If no spoke holds rows above its fence, leave spoke-0 past a sync with one."""
        self._check_fences(self._views())
        if self.unpushed:
            return
        machine = self.spokes[0]
        self._sync(machine, FaultTransport(self.run.hub_transport))
        held = self._versions(machine)
        for track in TRACK_IDS:
            deleting = held[(TRACKS, track)].content[1] is None
            if self._tombstone_write([(machine, track)], 0, deleting=deleting):
                return
        self._count("epilogue: every write was stranded, so no fence test this example")

    def _lose_pushes_before_settling(self) -> None:
        """Every spoke holding rows above its fence loses a push, before send and after commit.

        A correct fence stays put after ``fail_before_send``, so the spoke is
        still unpushed and also loses one after commit. A fence that stepped
        over the lost row is caught by the settle as a ``SyncDigestMismatch``,
        which is the real symptom.
        """
        self._check_fences(self._views())
        for machine in list(self.unpushed):
            self._lose_push(machine, "fail_before_send")
            self._check_fences(self._views())
            if machine in self.unpushed:
                self._lose_push(machine, "drop_response_after_commit")

    def _settle(self) -> None:
        for _pass in range(SETTLE_PASSES):
            for machine in self.spokes:
                self._sync(machine, FaultTransport(self.run.hub_transport))

    def _rewind_hub_and_resync(self) -> None:
        """Restore the empty-hub snapshot; every spoke must re-offer what it forgot.

        No assertion on ``hub_restore_detected`` here: that would re-read the
        flag a broken belt clears, not the damage. A spoke that misses the
        rewind keeps its fences, offers nothing, and fails on the DATA, as the
        ``SyncDigestMismatch`` from ``run_sync`` or the comparison after.
        """
        name, _seq, acked_then = self.snapshots[0]
        _apply_restore_hub(self.run, HUB, {"op": "restore", "name": name})
        self.acked_on_hub = dict(acked_then)
        self._count("hub restored")
        for machine in self.spokes:
            self._sync(machine, FaultTransport(self.run.hub_transport))

    def _compare(self, stage: str) -> None:
        """Two-sided parity on every digest table, and every machine holds the winners."""
        digests = {
            machine: self._read(machine, protocol.sync_digest) for machine in (HUB, *self.spokes)
        }
        hub = digests[HUB]
        assert set(hub.tables) == set(protocol.DIGEST_TABLES), (
            f"hub digest tables {sorted(hub.tables)} {stage}"
        )
        for machine in self.spokes:
            mine = digests[machine]
            assert not mine.quarantined and not hub.quarantined, (
                f"quarantine {stage}: {machine} {mine.quarantined}, hub {hub.quarantined}"
            )
            divergent = sorted(t for t in protocol.DIGEST_TABLES if mine.tables[t] != hub.tables[t])
            assert not divergent, f"{machine} and the hub disagree on {divergent} {stage}"
        expected = {row: self.oracle.winner(row) for row in self.oracle.rows()}
        for machine in (HUB, *self.spokes):
            held = self._versions(machine)
            wrong = {
                row: (held.get(row), want)
                for row, want in expected.items()
                if held.get(row) != want
            }
            assert set(held) == set(expected) and not wrong, (
                f"{machine} does not hold the LWW winners {stage}: {wrong}"
            )
        self._count(f"compared: fleet matches the oracle {stage}")


class UnguardedFleetSim(FleetSim):
    """:class:`FleetSim` with the stranded-write guard lifted (positive control)."""

    guard_stranded_writes: ClassVar[bool] = False


__all__ = ["MAX_SPOKES", "MIN_SPOKES", "FleetSim", "UnguardedFleetSim"]
