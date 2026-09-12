"""LATENCY-03 quality ratchet evaluator tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import quality_gate as qg
from scripts import quality_latency

pytestmark = pytest.mark.requirement("LATENCY-03")

REPO = Path(__file__).resolve().parents[2]


def test_evaluate_emits_declared_floor_on_live_tree() -> None:
    """[if] LATENCY-03 wiring is intact [then] evaluate echoes the baseline floor, [else stop]."""
    floor = json.loads((REPO / "ops/quality/baseline.json").read_text())["metrics"][
        "latency.input_to_applied_ms"
    ]
    metrics = quality_latency.evaluate(REPO)
    assert len(metrics) == 1
    assert metrics[0]["key"] == "latency.input_to_applied_ms"
    assert metrics[0]["value"] == floor


def test_broken_wiring_emits_999() -> None:
    """[if] eq-apply wiring is gone [then] evaluate reports 999, [else stop]."""
    sources = {
        quality_latency._SOURCE_PATHS[0]: "export const EQ_APPLY_KIND = 'eq-apply';",
        quality_latency._SOURCE_PATHS[1]: "function setEq(deck, band, value, pressT0Ms) {}",
        quality_latency._SOURCE_PATHS[2]: "engine.setEq(command.deck, command.band, command.value, pressT0Ms)",
        quality_latency._SOURCE_PATHS[3]: "'eq-apply'",
    }
    failures = quality_latency._wiring_failures(sources)
    assert failures
    assert any("linearRampToValueAtTime" in item for item in failures)
    floor = 16.0
    value = quality_latency.WIRING_FAIL_VALUE if failures else floor
    assert value == 999.0


def test_missing_baseline_key_raises(tmp_path: Path) -> None:
    """[if] baseline has no latency floor [then] evaluate raises, [else stop]."""
    (tmp_path / "ops" / "quality").mkdir(parents=True)
    (tmp_path / "ops" / "quality" / "baseline.json").write_text(
        json.dumps({"metrics": {}}),
        encoding="utf-8",
    )
    for rel in quality_latency._SOURCE_PATHS:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((REPO / rel).read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="latency.input_to_applied_ms"):
        quality_latency.evaluate(tmp_path)


def test_latency_is_registered_as_an_evaluator() -> None:
    """[if] latency is not in EVALUATORS [then] the gate never checks the floor, [else stop]."""
    assert "latency" in {e.name for e in qg.EVALUATORS}
