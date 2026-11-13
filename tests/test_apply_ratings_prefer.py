"""SYNC-12980: ``apply_ratings --prefer`` settles planned ratings conflicts.

[if] a planned ratings conflict meets ``--prefer`` [then] rb/djay pick the written side and newest writes nothing, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync import apply_ratings
from apps.sync.apply_ratings import _plan_rating_targets, live_run

pytestmark = [pytest.mark.requirement("SYNC-12980")]


def _conflict_row(**overrides: str) -> dict[str, str]:
    """Return one planned ``conflict`` row with both sides rated differently."""
    row = {
        "rb_content_id": "10",
        "djay_uuid": "u-10",
        "field": "rating",
        "rb_value": "4",
        "djay_value": "2",
        "resolution": "conflict",
    }
    row.update(overrides)
    return row


def test_prefer_rb_writes_rb_rating_to_djay() -> None:
    """[if] ``--prefer rb`` meets a resolvable conflict row [then] the RB rating is planned for djay [else stop]."""
    targets_rb, targets_djay = _plan_rating_targets([_conflict_row()], None, "rb")
    assert targets_rb == []
    assert targets_djay == [("u-10", 4)]


def test_prefer_djay_writes_djay_rating_to_rb() -> None:
    """[if] ``--prefer djay`` meets a resolvable conflict row [then] the djay rating is planned for RB [else stop]."""
    targets_rb, targets_djay = _plan_rating_targets([_conflict_row()], None, "djay")
    assert targets_rb == [("10", 2)]
    assert targets_djay == []


def test_prefer_newest_without_timestamps_leaves_conflict_unwritten() -> None:
    """[if] ``--prefer newest`` has no timestamp evidence [then] the conflict row is not planned [else stop]."""
    assert _plan_rating_targets([_conflict_row()], None, "newest") == ([], [])


def test_prefer_does_not_override_planned_side() -> None:
    """[if] the plan already resolved a row [then] ``--prefer`` keeps its planned direction [else stop]."""
    row = _conflict_row(resolution="accept_rb")
    assert _plan_rating_targets([row], None, "djay") == ([], [("u-10", 4)])


def test_prefer_skips_conflict_with_unrated_side() -> None:
    """[if] a conflict row has an unrated side [then] ``--prefer`` plans no write [else stop]."""
    row = _conflict_row(djay_value="0")
    assert _plan_rating_targets([row], None, "rb") == ([], [])


def test_live_run_forwards_prefer_to_planning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] ``live_run`` gets ``prefer=djay`` [then] the conflict reaches the RB writer only [else stop]."""
    seen: dict[str, list[tuple[str, int]]] = {}

    def _fake_rb(targets: list[tuple[str, int]], **_kw: object) -> int:
        """Record RB targets instead of writing."""
        seen["rb"] = targets
        return len(targets)

    def _fake_djay(targets: list[tuple[str, int]], **_kw: object) -> int:
        """Record djay targets instead of writing."""
        seen["djay"] = targets
        return len(targets)

    monkeypatch.setattr(apply_ratings, "_apply_rb_rating_targets", _fake_rb)
    monkeypatch.setattr(apply_ratings, "_apply_djay_rating_targets", _fake_djay)
    rc = live_run(
        [_conflict_row()],
        rb_db_path=tmp_path / "rb.db",
        djay_db_path=tmp_path / "djay.db",
        prefer="djay",
    )
    assert rc == 0
    assert seen == {"rb": [("10", 2)]}
