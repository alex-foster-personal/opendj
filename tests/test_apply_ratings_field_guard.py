"""[if] a ratings-diff row's field is not exactly ``rating`` [then] apply_ratings plans no write for it, [else stop]."""

from __future__ import annotations

import pytest

from apps.sync.apply_ratings import _plan_rating_targets

pytestmark = pytest.mark.requirement("SYNC-12989")


def _row(**overrides: str) -> dict:
    """Return an accepted ratings-diff row with optional field overrides."""
    row = {
        "rb_content_id": "1",
        "djay_uuid": "a",
        "field": "rating",
        "rb_value": "5",
        "djay_value": "2",
        "resolution": "accept_rb",
    }
    row.update(overrides)
    return row


def test_rating_field_rows_build_targets() -> None:
    """[if] a field=rating row has an accepted resolution [then] it plans the rating write, [else stop]."""
    rb, djay = _plan_rating_targets(
        [_row(), _row(rb_content_id="2", djay_uuid="b", resolution="accept_djay")],
        None,
    )
    assert djay == [("a", 5)]
    assert rb == [("2", 2)]


@pytest.mark.parametrize("field", ["cue", "bpm", "Rating", " rating", ""])
def test_non_rating_field_rows_are_skipped(field: str) -> None:
    """[if] a row's field is not exactly ``rating`` [then] it plans no write on either side, [else stop]."""
    rows = [_row(field=field), _row(field=field, resolution="accept_djay")]
    assert _plan_rating_targets(rows, None) == ([], [])


def test_rows_without_field_key_are_skipped() -> None:
    """[if] a row omits the field column entirely [then] it plans no write, [else stop]."""
    row = _row()
    del row["field"]
    assert _plan_rating_targets([row], None) == ([], [])
