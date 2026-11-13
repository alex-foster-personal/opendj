"""SYNC-12990: apply_ratings refuses contradictory duplicate vendor targets.

[if] a ratings diff plans two different ratings for one vendor target [then] the run aborts before any DB write, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync import apply_ratings
from apps.sync.apply_ratings import _plan_rating_targets, live_run
from apps.sync.safety import SafetyAbort

pytestmark = pytest.mark.requirement("SYNC-12990")


def _row(rb_id: str, uuid: str, rb_val: int, djay_val: int, resolution: str) -> dict:
    """Build one ratings-diff row with the given values and resolution."""
    return {
        "rb_content_id": rb_id,
        "djay_uuid": uuid,
        "field": "rating",
        "rb_value": str(rb_val),
        "djay_value": str(djay_val),
        "resolution": resolution,
        "action_hint": "",
    }


def test_single_vendor_target_is_planned_once() -> None:
    """[if] a vendor target occurs once in the diff [then] exactly one write is planned, [else stop]."""
    rb, djay = _plan_rating_targets(
        [_row("10", "u-1", 0, 4, "accept_djay"), _row("11", "u-2", 3, 0, "accept_rb")],
        None,
    )
    assert rb == [("10", 4)]
    assert djay == [("u-2", 3)]


def test_same_rating_duplicates_collapse_to_one_write() -> None:
    """[if] a vendor target repeats with the same planned rating [then] it is planned once, [else stop]."""
    rb, djay = _plan_rating_targets(
        [
            _row("10", "u-1", 0, 4, "accept_djay"),
            _row("10", "u-9", 1, 4, "accept_djay"),
            _row("11", "u-2", 3, 0, "accept_rb"),
            _row("12", "u-2", 3, 5, "accept_rb"),
        ],
        None,
    )
    assert rb == [("10", 4)]
    assert djay == [("u-2", 3)]


@pytest.mark.parametrize(
    "rows",
    [
        [_row("10", "u-1", 0, 4, "accept_djay"), _row("10", "u-9", 0, 2, "accept_djay")],
        [_row("11", "u-2", 3, 0, "accept_rb"), _row("12", "u-2", 5, 0, "accept_rb")],
    ],
    ids=["rekordbox", "djay"],
)
def test_conflicting_duplicates_abort_before_any_db_is_touched(
    rows: list[dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a vendor target repeats with different ratings [then] SafetyAbort fires before either DB is touched, [else stop]."""
    touched: list[str] = []

    def _forbid_open(_path: Path) -> None:
        """Record any Rekordbox DB open attempt."""
        touched.append("rb")

    def _forbid_session(*_a: object, **_k: object) -> None:
        """Record any live write session attempt."""
        touched.append("session")

    monkeypatch.setattr("apps.shared.rekordbox_db.open_db", _forbid_open)
    monkeypatch.setattr(apply_ratings, "LiveWriteSession", _forbid_session)
    monkeypatch.setattr(apply_ratings, "_write_djay_rating", lambda *_a: touched.append("djay"))

    with pytest.raises(SafetyAbort, match="conflicting duplicate"):
        live_run(
            rows,
            flag_ok=True,
            rb_db_path=tmp_path / "master.db",
            djay_db_path=tmp_path / "MediaLibrary.db",
        )
    assert touched == []
