"""SMART-01/02 -- SmartlistsRepo CRUD tests."""
from __future__ import annotations

import pytest

from apps.shared.smartlists import SmartlistRuleError
from apps.smartlists.repo import SmartlistsRepo, SmartlistsRepoError


pytestmark = pytest.mark.requirement("SMART-02")


def _rule() -> dict:
    return {
        "op": "and",
        "children": [
            {"field": "genre", "op": "=", "value": "House"},
            {"field": "bpm", "op": "between", "value": [120, 128]},
        ],
    }


def test_create_and_get_by_name(smartlists_repo: SmartlistsRepo) -> None:
    row = smartlists_repo.create("Fresh House", _rule())
    assert row.name == "Fresh House"
    assert row.referenced_fields == frozenset({"genre", "bpm"})
    fetched = smartlists_repo.get_by_name("Fresh House")
    assert fetched is not None and fetched.id == row.id


def test_create_rejects_bad_order_by(smartlists_repo: SmartlistsRepo) -> None:
    with pytest.raises(SmartlistsRepoError):
        smartlists_repo.create("x", _rule(), order_by="title desc")


def test_create_validates_rule(smartlists_repo: SmartlistsRepo) -> None:
    with pytest.raises(SmartlistRuleError):
        smartlists_repo.create(
            "x", {"field": "mystery", "op": "=", "value": 1},
        )


def test_duplicate_name_rejected(smartlists_repo: SmartlistsRepo) -> None:
    smartlists_repo.create("dupe", _rule())
    with pytest.raises(SmartlistsRepoError):
        smartlists_repo.create("dupe", _rule())


def test_list_all(smartlists_repo: SmartlistsRepo) -> None:
    smartlists_repo.create("a", _rule())
    smartlists_repo.create("b", _rule())
    names = [r.name for r in smartlists_repo.list_all()]
    assert names == ["a", "b"]


def test_delete_by_name(smartlists_repo: SmartlistsRepo) -> None:
    smartlists_repo.create("gone", _rule())
    assert smartlists_repo.delete_by_name("gone") is True
    assert smartlists_repo.get_by_name("gone") is None


def test_update_rule_refreshes_referenced_fields(
    smartlists_repo: SmartlistsRepo,
) -> None:
    row = smartlists_repo.create("x", _rule())
    new_rule = {"field": "energy", "op": ">=", "value": 7}
    updated = smartlists_repo.update_rule(row.id, new_rule)
    assert updated.referenced_fields == frozenset({"energy"})


def test_mark_materialized(smartlists_repo: SmartlistsRepo) -> None:
    row = smartlists_repo.create("m", _rule())
    smartlists_repo.mark_materialized(row.id, ["a", "b", "c"])
    fetched = smartlists_repo.get_by_id(row.id)
    assert fetched.last_materialized_track_ids == ["a", "b", "c"]
    assert fetched.last_evaluated_at is not None


def test_rule_roundtrips_through_json(smartlists_repo: SmartlistsRepo) -> None:
    row = smartlists_repo.create("rt", _rule())
    fetched = smartlists_repo.get_by_name("rt")
    assert fetched.rule == row.rule
