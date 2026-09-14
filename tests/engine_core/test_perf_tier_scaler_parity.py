"""PERFMODE-01 Python/TS scaler table parity."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.shared.perf_tier import PerfTier, scalers_for

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_PATH = REPO_ROOT / "apps/webui/frontend/src/lib/rb/perf-tier.ts"
MiB = 1024 * 1024


def _ts_tier_block(text: str, tier: str) -> str:
    match = re.search(rf"{tier}:\s*\{{(.*?)\n\t\}}", text, re.DOTALL)
    assert match is not None, f"missing {tier} block in perf-tier.ts"
    return match.group(1)


def _ts_int(block: str, key: str) -> int:
    match = re.search(rf"{key}:\s*([^,\n]+)", block)
    assert match is not None, f"missing {key} in TS block"
    expr = match.group(1).strip()
    if "* MiB" in expr:
        left = expr.split("*")[0].strip()
        return int(left) * MiB
    return int(expr)


@pytest.mark.requirement("PERFMODE-01")
def test_ts_scaler_table_matches_python() -> None:
    """
    [if] the TS scaler table is parsed [then] it matches scalers_for's python values, [else stop].
    """
    text = TS_PATH.read_text(encoding="utf-8")
    for tier in PerfTier:
        py = scalers_for(tier)
        block = _ts_tier_block(text, tier.value)
        assert _ts_int(block, "prefetch_tracks") == py["prefetch_tracks"]
        assert _ts_int(block, "prefetch_bytes") == py["prefetch_bytes"]
        assert _ts_int(block, "anlz_entries") == py["anlz_entries"]
        assert _ts_int(block, "anlz_bytes") == py["anlz_bytes"]
