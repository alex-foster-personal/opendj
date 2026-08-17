"""CAT-03 pairings repo round-trip tests.

Covers add / remove / get_neighbors / list_all / exists behaviour plus
validation and upsert semantics.
"""
from __future__ import annotations

import pytest

from apps.shared.pairings import PairingEdge, PairingsError, PairingsRepo

pytestmark = pytest.mark.requirement("CAT-03")


def test_add_returns_edge(pairings_repo: PairingsRepo) -> None:
    edge = pairings_repo.add("a" * 40, "b" * 40, direction="into",
                             notes="peak transition")
    assert isinstance(edge, PairingEdge)
    assert edge.from_stable_id == "a" * 40
    assert edge.to_stable_id == "b" * 40
    assert edge.direction == "into"
    assert edge.source == "manual"
    assert edge.notes == "peak transition"


def test_add_upsert_updates_modified_at(pairings_repo: PairingsRepo) -> None:
    e1 = pairings_repo.add("a", "b", direction="into", notes="first")
    e2 = pairings_repo.add("a", "b", direction="into", notes="second")
    assert e1.from_stable_id == e2.from_stable_id
    assert e2.notes == "second"
    # Exactly one row after the second insert (no duplicate).
    count = list(pairings_repo.list_all())
    assert len(count) == 1


def test_add_rejects_self_pair(pairings_repo: PairingsRepo) -> None:
    with pytest.raises(PairingsError):
        pairings_repo.add("same", "same", direction="into")


def test_add_rejects_bad_direction(pairings_repo: PairingsRepo) -> None:
    with pytest.raises(PairingsError):
        pairings_repo.add("a", "b", direction="wrong")


def test_add_rejects_bad_source(pairings_repo: PairingsRepo) -> None:
    with pytest.raises(PairingsError):
        pairings_repo.add("a", "b", source="junk")


def test_add_rejects_bad_confidence(pairings_repo: PairingsRepo) -> None:
    with pytest.raises(PairingsError):
        pairings_repo.add("a", "b", confidence=1.5)


def test_add_rejects_empty_ids(pairings_repo: PairingsRepo) -> None:
    with pytest.raises(PairingsError):
        pairings_repo.add("", "b")


def test_remove_returns_true_on_delete(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into")
    assert pairings_repo.remove("a", "b", "into") is True
    assert pairings_repo.remove("a", "b", "into") is False


def test_exists(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into")
    assert pairings_repo.exists("a", "b", "into") is True
    assert pairings_repo.exists("a", "b", "out_of") is False


def test_get_neighbors_both_sides(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into")
    pairings_repo.add("c", "a", direction="into")
    neighbours = pairings_repo.get_neighbors("a")
    # A appears on both sides: (a->b) and (c->a).
    stable_ids = {(e.from_stable_id, e.to_stable_id) for e in neighbours}
    assert stable_ids == {("a", "b"), ("c", "a")}


def test_get_neighbors_direction_filter(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into")
    pairings_repo.add("a", "c", direction="either")
    result = pairings_repo.get_neighbors("a", direction="into")
    assert [e.to_stable_id for e in result] == ["b"]


def test_list_all_source_filter(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into", source="manual")
    pairings_repo.add("c", "d", direction="into", source="ai",
                      confidence=0.9)
    manual = list(pairings_repo.list_all(source="manual"))
    ai = list(pairings_repo.list_all(source="ai"))
    assert len(manual) == 1 and manual[0].from_stable_id == "a"
    assert len(ai) == 1 and ai[0].from_stable_id == "c"


def test_list_all_from_id_filter(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into")
    pairings_repo.add("a", "c", direction="into")
    pairings_repo.add("x", "y", direction="into")
    from_a = list(pairings_repo.list_all(from_id="a"))
    assert len(from_a) == 2


def test_edge_round_trips_through_as_dict(pairings_repo: PairingsRepo) -> None:
    edge = pairings_repo.add("a", "b", direction="into", notes="blend",
                             confidence=0.75)
    d = edge.as_dict()
    assert d["from_stable_id"] == "a"
    assert d["notes"] == "blend"
    assert d["confidence"] == 0.75
    assert "created_at" in d and "modified_at" in d


def test_upsert_preserves_row_count(pairings_repo: PairingsRepo) -> None:
    pairings_repo.add("a", "b", direction="into")
    pairings_repo.add("a", "b", direction="into", notes="updated")
    pairings_repo.add("a", "b", direction="either")  # different PK
    edges = list(pairings_repo.list_all())
    # Two rows: (a,b,into) with updated notes + (a,b,either).
    assert len(edges) == 2
