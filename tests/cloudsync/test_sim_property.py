"""Property-based fleet simulation against a pure LWW oracle (plan W18).

The machine lives in :mod:`tests.cloudsync.sim_fleet`, the oracle in
:mod:`tests.cloudsync.sim_oracle`. This module runs it under two profiles
and proves it has teeth.

Profiles, picked by ``MDT_CLOUDSYNC_SIM_PROFILE``:

* ``ci`` (the declared default): derandomized, 50 examples, runs in CI.
* ``deep``: 5000 random examples, on demand
  (``just cloudsync-sim-deep``). Any other value raises.

Positive controls. A green property run proves nothing until the same run is
shown able to go red, so each control plants one real, named defect and
requires the simulation to find it:

* the hub-restore belt switched off (a spoke keeps its fences across a hub
  restore, so rows the hub forgot are never re-offered);
* the push fence recorded when rows are SELECTED instead of when they LAND
  (the round 1 loss class, ADR 08 point 3);
* the stranded-write guard lifted, which lets the simulation reach the
  logged P2 skew wedge (``.planning/debt/1993.md``). Deep tier only: it
  costs 164 s on the CI runner, and the strict xfail in
  ``test_hub_sync_clock_skew.py`` is the in-CI flip trigger for D4(c).

[if] a generated interleaving leaves the fleet off the LWW oracle [then] fail, [else stop].
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis.stateful import run_state_machine_as_test

from apps.sync_hub import client, engine

from .sim_fleet import FleetSim, UnguardedFleetSim

pytestmark = pytest.mark.requirement("CLOUDSYNC-04")

PROFILE_ENV: str = "MDT_CLOUDSYNC_SIM_PROFILE"
DEFAULT_PROFILE: str = "ci"
_QUIET = [HealthCheck.too_slow, HealthCheck.data_too_large]
PROFILES: dict[str, settings] = {
    "ci": settings(
        max_examples=50,
        stateful_step_count=20,
        derandomize=True,
        database=None,
        deadline=None,
        suppress_health_check=_QUIET,
        print_blob=False,
    ),
    "deep": settings(
        max_examples=5000,
        stateful_step_count=30,
        derandomize=False,
        database=None,
        deadline=None,
        suppress_health_check=_QUIET,
        print_blob=True,
    ),
}
#: Controls stop at the first counterexample: no shrinking, one report.
CONTROL = settings(
    max_examples=300,
    stateful_step_count=20,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=_QUIET,
    phases=(Phase.explicit, Phase.generate),
    report_multiple_bugs=False,
    print_blob=False,
)

#: What a ci run must have EXERCISED, not merely survived.
MUST_HAVE_HAPPENED: tuple[str, ...] = (
    "write: edit",
    "write: delete",
    "write: reinsert",
    "write: reorder",
    "sync: completed after hub restore",
    "sync: refused while partitioned",
    "fence test: push lost (fail_before_send)",
    "fence test: push lost (drop_response_after_commit)",
    "prune: hub",
    "prune: spoke",
    "clock skewed",
    "compared: fleet matches the oracle after settling",
    "compared: fleet matches the oracle after a hub rewind",
)


def selected_profile() -> settings:
    name = os.environ.get(PROFILE_ENV, DEFAULT_PROFILE)
    if name not in PROFILES:
        raise ValueError(f"{PROFILE_ENV}={name!r} is not one of {sorted(PROFILES)}")
    return PROFILES[name]


def test_fleet_converges_to_the_lww_oracle() -> None:
    """[if] the fleet settles anywhere but the LWW oracle [then] broken, [else stop].

    Rules: edit, delete, reinsert, reorder, sync, faulted sync, partition,
    skew, hub snapshot/restore, prune. The run must also have EXERCISED
    everything in :data:`MUST_HAVE_HAPPENED`, or its green means nothing.
    """
    FleetSim.stats.clear()
    run_state_machine_as_test(FleetSim, settings=selected_profile())
    missing = [what for what in MUST_HAVE_HAPPENED if FleetSim.stats[what] == 0]
    if not any(key.startswith("fault fired: ") for key in FleetSim.stats):
        missing.append("fault fired: <any endpoint>")
    assert not missing, (
        f"the run never exercised {missing}, so its green says nothing about them. "
        f"Exercised: {dict(FleetSim.stats)}"
    )


def _expect_digest_mismatch(machine: type[FleetSim]) -> None:
    """The seeded defect must surface as its real symptom: a spoke that cannot converge.

    Not any AssertionError: one from the simulation's own bookkeeping would
    pass a control that never reached the data.
    """
    with pytest.raises(client.SyncDigestMismatch):
        run_state_machine_as_test(machine, settings=CONTROL)


def test_control_a_disabled_hub_restore_belt_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a disabled hub-restore belt goes unnoticed [then] broken, [else stop].

    The seeded bug: a spoke keeps both fences across a hub restore, so rows
    the restored hub forgot are never offered to it again.
    """

    def belt_off(
        hub_machine_id: str, hub_seq: int, hub_generation: str, watermark: engine.Watermark
    ) -> tuple[engine.Watermark, bool]:
        return watermark, False

    monkeypatch.setattr(client, "_watermark_after_hello", belt_off)
    _expect_digest_mismatch(FleetSim)


def test_control_a_fence_recorded_at_selection_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a fence advanced before its rows land goes unnoticed [then] broken, [else stop].

    The seeded bug: the push fence is recorded when rows are SELECTED rather
    than when they LAND (ADR 08 point 3), so a push lost in flight is never
    offered again.
    """
    real_spoke_push = engine.spoke_push

    def fence_at_selection(
        conn: sqlite3.Connection, *, watermark: engine.Watermark, ceiling: int
    ) -> engine.Offer:
        offer = real_spoke_push(conn, watermark=watermark, ceiling=ceiling)
        engine.write_watermark(conn, dataclasses.replace(watermark, last_push_seq=ceiling))
        return offer

    monkeypatch.setattr(engine, "spoke_push", fence_at_selection)
    _expect_digest_mismatch(FleetSim)


@pytest.mark.skipif(
    os.environ.get(PROFILE_ENV, DEFAULT_PROFILE) != "deep",
    reason=(
        f"deep tier only ({PROFILE_ENV}=deep, `just cloudsync-sim-deep`): about 10 s on a "
        "dev Mac but 164 s on the self-hosted CI runner, and the strict xfail in "
        "test_hub_sync_clock_skew.py stays in CI as the D4(c) flip trigger"
    ),
)
def test_control_the_logged_skew_wedge_is_found_when_unguarded() -> None:
    """[if] the logged skew wedge goes unfound when unguarded [then] broken, [else stop].

    This pins debt 1993's skew wedge from the other side: the day a skew
    tolerance lands, this control stops finding a SyncDigestMismatch and must
    be rewritten against the chosen quarantine or refusal.
    """
    _expect_digest_mismatch(UnguardedFleetSim)
