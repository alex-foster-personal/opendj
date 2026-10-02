"""The fixed world the property simulation runs in (plan W18).

Split from :mod:`tests.cloudsync.sim_fleet` so the state machine stays under
the quality gate's file-size limit: the fleet's constants and strategies, the
seed scenario handed to the scenario harness's own seeder, and the single
read the per-step invariants make of each machine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypothesis import strategies as st

from apps.shared.state import sync_stamp
from apps.sync_hub import engine

from .scenario_runner_common import MachineDecl, Scenario
from .sim_oracle import PLAYLISTS, TRACKS, LwwKey, RowKey, Version, read_versions

HUB: str = "hub"
MIN_SPOKES: int = 2
MAX_SPOKES: int = 4
TRACK_IDS: tuple[str, ...] = ("trk-a", "trk-b", "trk-c")
PLAYLIST_ID: str = "pl-1"
PLAYLIST_NAME: str = "sim crate"
SEED_TITLE: str = "seed"
SEED_MEMBERS: tuple[str, ...] = ("trk-a", "trk-b")
#: Below every logical tick (ticks start one second after this instant).
SEED_STAMP: str = sync_stamp.to_canonical("2026-01-01T00:00:00+00:00")
SEED_ORIGIN: str = "seed-fixture"
TITLES: tuple[str, ...] = ("alpha", "bravo", "charlie")
SKEW_SECONDS: tuple[float, ...] = (-5.0, -1.0, 0.0, 2.0, 7.0)
FAULT_PATHS: tuple[str, ...] = ("hello", "push", "pull", "digest")
FAULT_MODES: tuple[str, ...] = ("fail_before_send", "drop_response_after_commit")
SETTLE_PASSES: int = 2
#: Per example. ``snapshot_hub`` takes no arguments, so hypothesis sorts it
#: first and the derandomized search drew it for 220 of ~1000 steps uncapped.
MAX_SNAPSHOTS: int = 3

SPOKE = st.integers(0, MAX_SPOKES - 1)
PICK = st.integers(0, 15)


def spoke_clock_offset(index: int) -> float:
    """A sub-second clock offset unique to one spoke, so no two spokes share a stamp.

    Ticks are whole seconds and ``SKEW_SECONDS`` are whole seconds, so without
    this spoke A at tick t and spoke B at tick t' collide whenever
    t + skew_a == t' + skew_b. LWW then breaks the tie on ``origin_device_id``,
    and machine ids are fresh random UUIDs every example: the winner flips
    between replays, the preconditions that read it flip too, and hypothesis
    raised FlakyStrategyDefinition 12 minutes into the first deep run. (index
    + 1) keeps every spoke stamp off the seed's whole-second stamp as well.
    """
    return (index + 1) / 1000


def fleet_scenario(spokes: list[str]) -> Scenario:
    """A scenario block for the harness's own seeder: three tracks, one playlist."""
    return Scenario(
        name="property-sim",
        description="hypothesis-driven fleet (plan W18)",
        machines=(MachineDecl(HUB, "hub"), *(MachineDecl(name, "spoke") for name in spokes)),
        seed={
            "tracks": [
                {
                    "stable_id": stable_id,
                    "on": spokes,
                    "title": SEED_TITLE,
                    "updated_at": SEED_STAMP,
                }
                for stable_id in TRACK_IDS
            ],
            "playlists": [
                {
                    "playlist_id": PLAYLIST_ID,
                    "name": PLAYLIST_NAME,
                    "members": list(SEED_MEMBERS),
                    "on": spokes,
                    "updated_at": SEED_STAMP,
                }
            ],
        },
        steps=(),
        expected={},
        source=Path(__file__),
    )


def seed_versions() -> dict[RowKey, Version]:
    """What the oracle records for the seed: the same stamp on every machine."""
    key: LwwKey = (SEED_STAMP, SEED_ORIGIN)
    seeded = {(TRACKS, stable_id): Version(key, (SEED_TITLE, None, None)) for stable_id in TRACK_IDS}
    seeded[(PLAYLISTS, PLAYLIST_ID)] = Version(key, (PLAYLIST_NAME, None, SEED_MEMBERS))
    return seeded


@dataclass(frozen=True)
class MachineView:
    """Everything the per-step invariants need from one machine, in one read."""

    versions: dict[RowKey, Version]
    fence_rows: list[tuple[Any, ...]]
    local_seq: int
    hub_seq: int


def machine_view(conn: sqlite3.Connection) -> MachineView:
    """This machine's versions, its ``sync_state`` fence rows, and both seqs."""
    fence_rows = conn.execute(
        "SELECT peer, last_push_seq, peer_generation, last_sync_at FROM sync_state"
    ).fetchall()
    return MachineView(
        read_versions(conn), fence_rows, engine.local_seq(conn), engine.current_seq(conn)
    )
