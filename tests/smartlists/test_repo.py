"""SMART-01/02 -- SmartlistsRepo CRUD tests."""
from __future__ import annotations

import pytest

from apps.shared.smartlists import SmartlistRuleError
from apps.smartlists.repo import (
    SmartlistRevisionConflict,
    SmartlistsRepo,
    SmartlistsRepoError,
    smartlist_revision,
)

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
    updated = smartlists_repo.update_rule(
        row.id, new_rule, expected_revision=smartlist_revision(row),
    )
    assert updated.referenced_fields == frozenset({"energy"})


def test_update_rule_rejects_stale_revision_without_mutating_any_field(
    smartlists_repo: SmartlistsRepo,
) -> None:
    """A stale compare-and-swap must preserve the exact persisted row."""
    row = smartlists_repo.create("atomic", _rule())
    current = smartlists_repo.update_rule(
        row.id,
        {"field": "energy", "op": ">=", "value": 7},
        expected_revision=smartlist_revision(row),
        order_by="energy desc",
    )
    before = smartlists_repo.conn.execute(
        "SELECT * FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()

    with pytest.raises(SmartlistRevisionConflict) as raised:
        smartlists_repo.update_rule(
            row.id,
            {"field": "rating", "op": ">=", "value": 4},
            expected_revision=smartlist_revision(row),
            order_by="rating desc",
        )

    assert raised.value.current_revision == smartlist_revision(current)
    after = smartlists_repo.conn.execute(
        "SELECT * FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()
    assert after == before


def test_update_rule_rolls_back_if_persisted_readback_fails(
    smartlists_repo: SmartlistsRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The update and returned readback are one SQLite transaction."""
    row = smartlists_repo.create("readback", _rule())
    before = smartlists_repo.conn.execute(
        "SELECT * FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()
    statements: list[str] = []
    smartlists_repo.conn.set_trace_callback(statements.append)
    original_get = smartlists_repo.get_by_id
    read_count = 0

    def fail_second_readback(smartlist_id: str):
        nonlocal read_count
        read_count += 1
        if read_count == 2:
            raise RuntimeError("injected readback failure")
        return original_get(smartlist_id)

    monkeypatch.setattr(smartlists_repo, "get_by_id", fail_second_readback)
    with pytest.raises(RuntimeError, match="injected readback failure"):
        smartlists_repo.update_rule(
            row.id,
            {"field": "energy", "op": ">=", "value": 7},
            expected_revision=smartlist_revision(row),
        )

    assert read_count == 2
    assert any(statement == "BEGIN IMMEDIATE" for statement in statements)
    assert any(statement.startswith("UPDATE smartlists SET") for statement in statements)
    assert statements[-1] == "ROLLBACK"
    assert smartlists_repo.conn.in_transaction is False
    after = smartlists_repo.conn.execute(
        "SELECT * FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()
    assert after == before


@pytest.mark.parametrize(
    ("rule", "order_by", "expected_error"),
    [
        ({"field": "mystery", "op": "=", "value": 1}, None, SmartlistRuleError),
        (_rule(), "title desc", SmartlistsRepoError),
    ],
)
def test_update_rule_validates_complete_replacement_before_transaction(
    smartlists_repo: SmartlistsRepo,
    rule: dict,
    order_by: str | None,
    expected_error: type[Exception],
) -> None:
    row = smartlists_repo.create("validated", _rule())
    before = smartlists_repo.conn.execute(
        "SELECT * FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()
    statements: list[str] = []
    smartlists_repo.conn.set_trace_callback(statements.append)

    with pytest.raises(expected_error):
        smartlists_repo.update_rule(
            row.id,
            rule,
            expected_revision=smartlist_revision(row),
            order_by=order_by,
        )

    assert not any(statement.startswith("BEGIN") for statement in statements)
    after = smartlists_repo.conn.execute(
        "SELECT * FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()
    assert after == before


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


def test_update_rule_can_rename(smartlists_repo: SmartlistsRepo) -> None:
    row = smartlists_repo.create("old", _rule())
    updated = smartlists_repo.update_rule(
        row.id,
        _rule(),
        expected_revision=smartlist_revision(row),
        name="new",
    )
    assert updated.name == "new"
    assert smartlists_repo.get_by_name("new") is not None


def test_delete_tombstones_and_rewrites_name(smartlists_repo: SmartlistsRepo) -> None:
    row = smartlists_repo.create("gone", _rule())
    assert smartlists_repo.delete(row.id) is True
    assert smartlists_repo.get_by_id(row.id) is None
    raw = smartlists_repo.conn.execute(
        "SELECT name, deleted_at FROM smartlists WHERE id=?", (row.id,),
    ).fetchone()
    assert raw[1] is not None
    assert "__deleted__" in raw[0]


def test_list_all_omits_tombstones(smartlists_repo: SmartlistsRepo) -> None:
    live = smartlists_repo.create("live", _rule())
    dead = smartlists_repo.create("dead", _rule())
    smartlists_repo.delete(dead.id)
    names = [r.name for r in smartlists_repo.list_all()]
    assert names == [live.name]


def test_duplicate_copies_rule_with_new_id(smartlists_repo: SmartlistsRepo) -> None:
    source = smartlists_repo.create("src", _rule())
    copy = smartlists_repo.duplicate(source.id)
    assert copy.id != source.id
    assert copy.rule == source.rule
    assert copy.name == "src (copy)"


def test_unused_name_skips_live_names(smartlists_repo: SmartlistsRepo) -> None:
    smartlists_repo.create("base", _rule())
    smartlists_repo.create("base 2", _rule())
    assert smartlists_repo.unused_name("base") == "base 3"
