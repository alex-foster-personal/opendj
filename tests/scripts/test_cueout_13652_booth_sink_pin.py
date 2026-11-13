"""Ledger checks for CUEOUT-13652 (issue #11258, booth sink pin and gain).

[if] the CUEOUT-13652 fragment ships [then] the stitched build lists it under v2 CUEOUT, [else stop].
"""

from __future__ import annotations

import pytest

from scripts import build_reqs_json as mod

pytestmark = pytest.mark.requirement("CUEOUT-13652")


def _cueout_13652() -> dict:
    for req in mod._build_payload()["v2"]["CUEOUT"]["requirements"]:
        if req["id"] == "CUEOUT-13652":
            return req
    raise AssertionError("CUEOUT-13652 missing from stitched reqs build")


def test_cueout_13652_ships_in_v2_cueout_category() -> None:
    """[if] CUEOUT-13652 fragment is present [then] v2 CUEOUT lists it shipped, [else stop]."""
    req = _cueout_13652()
    assert req["status"] == "shipped"
    assert "#11258" in req["desc"]
    assert "mixer.booth" in req["desc"]
