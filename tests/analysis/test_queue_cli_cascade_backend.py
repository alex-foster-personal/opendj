"""A cascade batch runs the DEPENDENT lane's producer, not the run's.

Live on demon-llama, Thu 1 Oct 2026: 348 key items were enqueued under
``own_beatgrid.backfill`` by the beatgrid run's cascade and all skipped as
already current, so no key was ever computed through that path.

-Claude
"""
from __future__ import annotations

from apps.analysis.backends import get_backend
from apps.analysis.queue_cli import backend_for_lane


def test_a_key_cascade_from_a_beatgrid_run_uses_the_key_producer() -> None:
    beatgrid = get_backend("own_beatgrid.backfill")
    assert backend_for_lane(beatgrid, "key").name == "own_key.backfill", (
        "if a key cascade runs under the beatgrid backend then broken"
    )


def test_the_runs_own_lane_keeps_the_runs_backend() -> None:
    beatgrid = get_backend("own_beatgrid.backfill")
    assert backend_for_lane(beatgrid, "beatgrid") is beatgrid, (
        "if the run's own lane is re-resolved to another class then broken"
    )
