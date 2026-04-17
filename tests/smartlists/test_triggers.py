"""SMART-03 -- TriggerRunner tests."""
from __future__ import annotations

import pytest

from apps.smartlists.materializer import Materializer
from apps.smartlists.triggers import StateEvent, TriggerRunner
from apps.smartlists.writers import FakeWriter


pytestmark = pytest.mark.requirement("SMART-03")


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def seeded_two_lists(fixture_library, smartlists_repo):
    fixture_library.add_many([
        {"stable_id": "a", "fields": {"genre": "House", "bpm": 125}},
        {"stable_id": "b", "fields": {"genre": "Techno", "bpm": 130}},
    ])
    row_house = smartlists_repo.create(
        "by-genre", {"field": "genre", "op": "=", "value": "House"},
    )
    row_bpm = smartlists_repo.create(
        "fast", {"field": "bpm", "op": ">=", "value": 128},
    )
    return row_house, row_bpm


def _make_runner(sm_repo, clock):
    mat = Materializer(sm_repo, [FakeWriter("rekordbox")])
    return TriggerRunner(
        sm_repo, mat, debounce_seconds=5.0, clock=clock,
        dry_run=False, live=True,
    )


def test_event_touching_genre_wakes_only_genre_rule(
    seeded_two_lists, smartlists_repo, clock,
) -> None:
    row_house, row_bpm = seeded_two_lists
    runner = _make_runner(smartlists_repo, clock)
    runner.handle_event(StateEvent(
        kind="track.tag_edited",
        stable_id="a",
        changed_fields=frozenset({"genre"}),
    ))
    pending = runner.pending()
    assert row_house.id in pending
    assert row_bpm.id not in pending


def test_event_touching_bpm_wakes_only_bpm_rule(
    seeded_two_lists, smartlists_repo, clock,
) -> None:
    row_house, row_bpm = seeded_two_lists
    runner = _make_runner(smartlists_repo, clock)
    runner.handle_event(StateEvent(
        kind="track.tag_edited",
        stable_id="a",
        changed_fields=frozenset({"bpm"}),
    ))
    pending = runner.pending()
    assert row_bpm.id in pending
    assert row_house.id not in pending


def test_track_added_wakes_everything(
    seeded_two_lists, smartlists_repo, clock,
) -> None:
    row_house, row_bpm = seeded_two_lists
    runner = _make_runner(smartlists_repo, clock)
    runner.handle_event(StateEvent(
        kind="track.added", stable_id="new",
        changed_fields=frozenset(),
    ))
    pending = set(runner.pending())
    assert row_house.id in pending and row_bpm.id in pending


def test_burst_coalesces_into_single_eval(
    seeded_two_lists, smartlists_repo, clock,
) -> None:
    row_house, _ = seeded_two_lists
    runner = _make_runner(smartlists_repo, clock)
    for i in range(10):
        clock.advance(0.2)
        runner.handle_event(StateEvent(
            kind="track.tag_edited",
            changed_fields=frozenset({"genre"}),
        ))
    assert runner.pending() == [row_house.id]
    assert runner.run_ready() == []
    clock.advance(5.01)
    results = runner.run_ready()
    assert len(results) == 1
    assert results[0].smartlist_id == row_house.id


def test_field_scope_false_wakes_all(
    seeded_two_lists, smartlists_repo, clock,
) -> None:
    row_house, row_bpm = seeded_two_lists
    mat = Materializer(smartlists_repo, [FakeWriter("rb")])
    runner = TriggerRunner(
        smartlists_repo, mat, debounce_seconds=0.1, clock=clock,
        field_scope=False, dry_run=False, live=True,
    )
    runner.handle_event(StateEvent(
        kind="track.rating_changed",
        changed_fields=frozenset({"rating"}),
    ))
    assert set(runner.pending()) == {row_house.id, row_bpm.id}


def test_arm_all_materialises_every_smartlist(
    seeded_two_lists, smartlists_repo, clock,
) -> None:
    runner = _make_runner(smartlists_repo, clock)
    runner.arm_all()
    clock.advance(10.0)
    results = runner.run_ready()
    assert len(results) == 2
