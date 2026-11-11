"""DECKUX-10671: KEY SYNC ladder chevron stays inside the button box (issue #10303)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REQS = REPO / "reqs.json"


@pytest.mark.requirement("DECKUX-10671")
def test_if_deckux_10671_is_shipped_then_reqs_json_lists_the_key_sync_chevron_requirement():
    """[if] DECKUX-10671 ships [then] reqs.json records it shipped, [else stop]."""
    data = json.loads(REQS.read_text(encoding="utf-8"))
    ids = {
        req["id"]
        for bucket in ("v1", "v1.1", "v2", "v3")
        for category in data.get(bucket, {}).values()
        for req in category.get("requirements", [])
    }
    shipped = {
        req["id"]
        for bucket in ("v1", "v1.1", "v2", "v3")
        for category in data.get(bucket, {}).values()
        for req in category.get("requirements", [])
        if req.get("status") == "shipped"
    }
    assert "DECKUX-10671" in ids
    assert "DECKUX-10671" in shipped
