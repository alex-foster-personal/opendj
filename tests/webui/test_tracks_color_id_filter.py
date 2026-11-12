"""Tracks list colour filter (issue #10041).

[if] color_id query params drift from rekordbox semantics [then] these tests fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.webui.server.backend import Track
from tests.webui.app_fixtures import client_fixture  # noqa: F401

pytestmark = pytest.mark.requirement("LIBUX-10679")


def _seed_colours(seed_backend, stamp: str) -> None:
    seed_backend.seed_track(Track(stable_id="c-red", title="Red", color_id=1, created_at=stamp, updated_at=stamp))
    seed_backend.seed_track(Track(stable_id="c-blue", title="Blue", color_id=3, created_at=stamp, updated_at=stamp))
    seed_backend.seed_track(Track(stable_id="c-none", title="Plain", color_id=None, created_at=stamp, updated_at=stamp))


def test_list_tracks_color_id_repeatable_and_zero_means_unlabelled(client, seed_backend) -> None:
    """[if] GET uses color_id=1&color_id=3 or color_id=0 [then] labels match rekordbox, [else stop]."""
    stamp = "2026-11-09T12:00:00Z"
    _seed_colours(seed_backend, stamp)
    both = client.get("/api/v1/tracks", params=[("color_id", 1), ("color_id", 3)])
    assert both.status_code == 200, both.text
    ids = {row["stable_id"] for row in both.json()["items"]}
    assert ids == {"c-red", "c-blue"}

    none = client.get("/api/v1/tracks", params={"color_id": 0})
    assert none.status_code == 200, none.text
    none_ids = {row["stable_id"] for row in none.json()["items"]}
    assert "c-none" in none_ids
    assert "c-red" not in none_ids

    bad = client.get("/api/v1/tracks", params={"color_id": 9})
    assert bad.status_code == 422, bad.text


def test_libux_color_filter_requirement_is_shipped_in_reqs_json() -> None:
    """[if] LIBUX-10679 is missing or pending in reqs.json [then] the flip gate fails, [else stop]."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    doc = json.loads((root / "reqs.json").read_text(encoding="utf-8"))
    found: dict | None = None

    def _walk(node: object) -> None:
        nonlocal found
        if found is not None:
            return
        if isinstance(node, dict):
            if node.get("id") == "LIBUX-10679":
                found = node
                return
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(doc)
    row = found
    assert row is not None, "LIBUX-10679 missing from reqs.json"
    assert row.get("status") == "shipped", row
