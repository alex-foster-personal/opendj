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


def _ts_keys(block: str) -> set[str]:
    return set(re.findall(r"^\s*([a-z_]+):", block, re.MULTILINE))


def _ts_value(block: str, key: str) -> int | str:
    match = re.search(rf"^\s*{key}:\s*([^,\n]+)", block, re.MULTILINE)
    assert match is not None, f"missing {key} in TS block"
    expr = match.group(1).strip()
    if expr.startswith(("'", '"')):
        return expr.strip("'\"")
    if "* MiB" in expr:
        left = expr.split("*")[0].strip()
        return int(left) * MiB
    return int(expr)


@pytest.mark.requirement("PERFMODE-01")
def test_ts_scaler_table_matches_python() -> None:
    """
    [if] the TS scaler table is parsed [then] every key matches python's scalers_for, [else stop].

    It compares EVERY key, not a named list. The previous revision asserted four
    hard-coded keys, so `stem_decode` and `worker_divisor` were never compared,
    and a new scaler (CUEOUT-15's `preview_pcm_bytes`, Wed 16 Sep 2026) could
    drift between the two tables with this test still green: that was checked
    by setting Python's LOW value to 65 MiB against TypeScript's 64 and
    watching it pass.
    """
    text = TS_PATH.read_text(encoding="utf-8")
    for tier in PerfTier:
        py = scalers_for(tier)
        block = _ts_tier_block(text, tier.value)
        assert _ts_keys(block) == set(py), (
            f"if a scaler exists in only one table then the engine and the page "
            f"disagree about {tier.value} caps - broken: "
            f"TS only {sorted(_ts_keys(block) - set(py))}, "
            f"python only {sorted(set(py) - _ts_keys(block))}"
        )
        for key, value in py.items():
            assert _ts_value(block, key) == value, (
                f"if {tier.value}.{key} differs between python and TS then one "
                f"side enforces a cap the other does not - broken"
            )
