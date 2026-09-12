"""PERFMODE-03 Python/TS app-posture scaler parity."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.shared.app_posture import (
    GIG_LIBRARY_POLL_MS,
    GIG_PREFETCH_BYTES,
    GIG_PREFETCH_TRACKS,
    PREP_LIBRARY_POLL_MS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_PATH = REPO_ROOT / "apps/webui/frontend/src/lib/rb/app-posture.ts"


def _ts_const(text: str, name: str) -> int:
    match = re.search(rf"export const {name} = ([^;]+);", text)
    assert match is not None, f"missing {name} in app-posture.ts"
    expr = match.group(1).strip()
    if "* 1024 * 1024" in expr:
        left = expr.split("*")[0].strip()
        return int(left) * 1024 * 1024
    return int(expr.replace("_", ""))


@pytest.mark.requirement("PERFMODE-03")
def test_ts_posture_constants_match_python() -> None:
    """[if] TS posture constants drift from Python [then] parity test fails, [else stop]."""
    text = TS_PATH.read_text(encoding="utf-8")
    assert _ts_const(text, "PREP_LIBRARY_POLL_MS") == PREP_LIBRARY_POLL_MS
    assert _ts_const(text, "GIG_LIBRARY_POLL_MS") == GIG_LIBRARY_POLL_MS
    assert _ts_const(text, "GIG_PREFETCH_TRACKS") == GIG_PREFETCH_TRACKS
    assert _ts_const(text, "GIG_PREFETCH_BYTES") == GIG_PREFETCH_BYTES
