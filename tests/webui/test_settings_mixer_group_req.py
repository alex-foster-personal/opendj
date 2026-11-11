"""Issue #10085: MIXUX-10672 Mixer settings group requirement in reqs.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REQ_ID = "MIXUX-10672"


def _req_entry(req_id: str) -> dict | None:
    data = json.loads((REPO / "reqs.json").read_text(encoding="utf-8"))
    for category in data.get("v1", {}).values():
        for req in category.get("requirements", []):
            if req.get("id") == req_id:
                return req
    return None


@pytest.mark.requirement("MIXUX-10672")
def test_mixux_10672_mixer_settings_group_is_shipped_in_reqs_json() -> None:
    """[if] MIXUX-10672 is recorded shipped [then] reqs.json lists the mixer settings group requirement, [else stop]."""
    entry = _req_entry(REQ_ID)
    assert entry is not None, f"{REQ_ID} missing from reqs.json"
    assert entry.get("status") == "shipped"
    desc = entry.get("desc", "")
    assert "Mixer group" in desc
    assert "#10085" in desc
