"""SMART-02 -- Materializer + FakeWriter integration tests."""
from __future__ import annotations

import pytest

from apps.smartlists.materializer import Materializer, MaterializeResult
from apps.smartlists.writers import FakeWriter


pytestmark = pytest.mark.requirement("SMART-02")


@pytest.fixture
def rule_house():
    return {"field": "genre", "op": "=", "value": "House"}


@pytest.fixture
def seeded(fixture_library):
    fixture_library.add_many([
        {"stable_id": "a", "fields": {"genre": "House"}},
        {"stable_id": "b", "fields": {"genre": "House"}},
        {"stable_id": "c", "fields": {"genre": "Techno"}},
    ])
    return fixture_library


def test_dry_run_produces_result_without_writes(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox")
    dj = FakeWriter(vendor="djay")
    mat = Materializer(smartlists_repo, [rb, dj])
    result = mat.materialize(row.id, dry_run=True, live=False)
    assert isinstance(result, MaterializeResult)
    assert result.dry_run is True
    assert result.ok
    assert set(result.added_tracks) == {"a", "b"}
    assert result.removed_tracks == []
    assert rb.calls == [] and dj.calls == []


def test_live_run_creates_playlist_on_each_writer(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox")
    dj = FakeWriter(vendor="djay")
    mat = Materializer(smartlists_repo, [rb, dj])
    result = mat.materialize(row.id, dry_run=False, live=True)
    assert result.ok
    assert result.dry_run is False
    assert set(rb.playlists["[SL] House"]) == {"a", "b"}
    assert set(dj.playlists["[SL] House"]) == {"a", "b"}


def test_idempotent_second_run_noop(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox")
    mat = Materializer(smartlists_repo, [rb])
    mat.materialize(row.id, dry_run=False, live=True)
    first_calls = list(rb.calls)
    second = mat.materialize(row.id, dry_run=False, live=True)
    assert second.added_tracks == [] and second.removed_tracks == []
    new_calls = rb.calls[len(first_calls):]
    assert all(c[0] in ("exists", "diff") for c in new_calls)


def test_diff_applied_on_subsequent_run(
    fixture_library, smartlists_repo, rule_house,
) -> None:
    fixture_library.add_track("a")
    fixture_library.add_field("a", "genre", "House")
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox")
    mat = Materializer(smartlists_repo, [rb])
    mat.materialize(row.id, dry_run=False, live=True)
    fixture_library.add_track("b")
    fixture_library.add_field("b", "genre", "House")
    result = mat.materialize(row.id, dry_run=False, live=True)
    assert result.added_tracks == ["b"]
    assert "b" in rb.playlists["[SL] House"]


def test_collision_on_first_run_blocks_without_force(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox",
                    playlists={"[SL] House": ["pre-existing"]})
    mat = Materializer(smartlists_repo, [rb])
    result = mat.materialize(row.id, dry_run=False, live=True)
    assert not result.ok
    assert any("already exists" in e for e in result.errors)


def test_collision_force_adopt(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox",
                    playlists={"[SL] House": ["pre-existing"]})
    mat = Materializer(smartlists_repo, [rb])
    result = mat.materialize(
        row.id, dry_run=False, live=True, force_adopt=True,
    )
    assert result.ok
    assert result.adopted_existing is True
    # Both our tracks are now in the playlist; pre-existing entries
    # remain because force-adopt treats the current state as the "old"
    # baseline (diff is add-only on first adopt).
    assert set(rb.playlists["[SL] House"]) >= {"a", "b"}


def test_writer_error_is_isolated(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    rb = FakeWriter(vendor="rekordbox")
    dj = FakeWriter(vendor="djay", raise_on_apply=True)
    mat = Materializer(smartlists_repo, [rb, dj])
    result = mat.materialize(row.id, dry_run=False, live=True)
    assert not result.ok
    assert result.writers_applied == {"rekordbox": True, "djay": False}
    assert set(rb.playlists["[SL] House"]) == {"a", "b"}
    refreshed = smartlists_repo.get_by_id(row.id)
    assert refreshed.last_materialized_track_ids == []


def test_materialize_all_sequential(
    seeded, smartlists_repo, rule_house,
) -> None:
    smartlists_repo.create("h1", rule_house)
    smartlists_repo.create(
        "high-energy", {"field": "energy", "op": ">=", "value": 8},
    )
    rb = FakeWriter(vendor="rekordbox")
    mat = Materializer(smartlists_repo, [rb])
    results = mat.materialize_all(dry_run=True)
    assert len(results) == 2
    names = {r.smartlist_name for r in results}
    assert names == {"h1", "high-energy"}


def test_no_writer_still_returns_diff(
    seeded, smartlists_repo, rule_house,
) -> None:
    row = smartlists_repo.create("House", rule_house)
    mat = Materializer(smartlists_repo, [])
    result = mat.materialize(row.id, dry_run=True)
    assert set(result.added_tracks) == {"a", "b"}
    assert result.writers_applied == {}
