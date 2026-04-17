"""open-dj v0 PlayOrder serde round-trip."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.play_orders import (
    play_order_from_opendj,
    play_order_to_opendj,
)
from apps.shared.play_orders.serde import OPENDJ_SCHEMA_VERSION

pytestmark = [
    pytest.mark.requirement("PLAY-01"),
    pytest.mark.requirement("PLAY-03"),
]


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "opendj"
    / "play_order_sample.json"
)


def test_fixture_exists() -> None:
    assert FIXTURE.exists(), f"fixture missing at {FIXTURE}"


def test_round_trip_preserves_fields() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    po = play_order_from_opendj(raw)
    again = play_order_to_opendj(po)
    # Compare canonically (sort_keys) so key order does not matter.
    # This is a stand-in for RFC 8785 canonicalisation -- documented in
    # the test docstring per Plan 01 Step 4.1.
    assert json.dumps(again, sort_keys=True) == json.dumps(raw, sort_keys=True)


def test_schema_version_emitted() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    po = play_order_from_opendj(raw)
    assert play_order_to_opendj(po)["schema_version"] == OPENDJ_SCHEMA_VERSION


def test_missing_required_field_raises() -> None:
    with pytest.raises(KeyError, match="playlist_id"):
        play_order_from_opendj({"name": "x", "entries": []})


def test_unknown_keys_are_ignored() -> None:
    """Future extensions (x_* keys) must not break older readers."""
    raw = {
        "schema_version": "0.1",
        "playlist_id": "pl1",
        "name": "x",
        "entries": [],
        "x_future_field": {"anything": [1, 2, 3]},
    }
    po = play_order_from_opendj(raw)
    assert po.name == "x"


def test_serialisation_no_u2014_chars() -> None:
    """Repo-wide convention: no U+2014 or U+2013 characters in emitted strings."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    po = play_order_from_opendj(raw)
    s = json.dumps(play_order_to_opendj(po), ensure_ascii=False)
    assert "\u2014" not in s
    assert "\u2013" not in s
