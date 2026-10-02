"""Vocal stems imply vocal analysis, with a switch (PVPIN-18, pin 40d3b9731db7).

The coverage drain (HEALTH-05) is what delivers this ask, so these
are the pin's own two acceptance lines run against that drain.

Regression lines:
  - if a track with a vocal stem bundle needs a manual run to get vocal analysis then broken
  - if a backlog of several such tracks is not worked off by the drain alone then broken
  - if the vocals switch is off and vocal analysis is still derived then broken
  - if the switch defaults off then broken

[if] stems exist and vocal analysis does not follow on its own [then] fail, [else stop].
"""
from __future__ import annotations

import pytest

from tests.health_lights.conftest import Library
from tests.health_lights.test_coverage_drain import Rig, _present

pytestmark = pytest.mark.requirement("PVPIN-18")


def _vocals(library: Library) -> dict[str, int]:
    coverage = library.client.get("/api/v1/ingest/coverage").json()
    return {"done": coverage["done"]["vocals"], "pending": coverage["pending"]["vocals"]}


def test_a_backlog_of_tracks_with_stems_gets_vocal_analysis_without_a_manual_run(
    library: Library,
) -> None:
    backlog = ("a", "b", "c")
    for stable_id in backlog:
        _present(library, stable_id, stems=True)
    rig = Rig(library)
    assert rig.drain.status().enabled is True, "the switch must default on"
    assert _vocals(library) == {"done": 0, "pending": 3}

    ran = [rig.drain.tick() for _ in backlog]

    assert ran == ["ran:vocals"] * 3
    assert rig.vocals_runs == list(backlog)
    assert _vocals(library) == {"done": 3, "pending": 0}


def test_a_track_without_stems_gets_no_vocal_job(library: Library) -> None:
    """Control: the mapping is from STEMS, so no stems means no vocals job."""
    _present(library, "a", stems=False)
    rig = Rig(library)

    rig.drain.tick()

    assert rig.vocals_runs == []


def test_the_drain_switch_off_derives_no_vocal_analysis_from_stems(library: Library) -> None:
    _present(library, "a", stems=True)
    rig = Rig(library)
    rig.drain.set_enabled(False)

    for _ in range(3):
        rig.drain.tick()

    assert rig.vocals_runs == []
    assert _vocals(library) == {"done": 0, "pending": 1}
