"""B2BBOOTH-39: the mirror half of the booth rig can say no (display drift and the mirror's plan hop).

- [if] a run whose control page drew every frame within 10 ms of the truth does not score ok [then] fail
- [if] a run with drift p95 over 10 ms scores ok, or a drift measured only on the good side of zero hides a bad side [then] fail
- [if] a run where no position sample reached the control page reads a number instead of UNKNOWN [then] fail
- [if] the plan hop (control receipt to the mirror's planned command) is not reported [then] fail
- POSITIVE CONTROL: [if] a run whose audio page biased its samples K ms ahead does not read K back within 1 ms,
  or a run that reads 0 under a K bias passes [then] the instrument cannot fail and proves nothing, [else stop].

[if] mirror display drift or plan-hop scoring regresses [then] fail with the scored fields, [else stop].
"""

from __future__ import annotations

import pytest

from tests.b2b_booth.rig.score_booth_rig import DISPLAY_DRIFT_BAR_MS, score_mirror

pytestmark = [pytest.mark.requirement("B2BBOOTH-39")]


def _pages(drift: list[float], received: int = 400, plan: list[float] | None = None) -> tuple[dict, dict]:
    audio = {"side": "inviter", "mirror": {"role": "audio", "positions_sent": 400}}
    control = {"side": "joiner", "mirror": {"role": "control", "positions_received": received, "drift_ms": drift,
                                           "plan_ms": plan if plan is not None else [0.05, 0.1, 0.2]}}
    return audio, control


def test_a_clean_display_scores_ok() -> None:
    out = score_mirror(*_pages([0.5, -1.0, 2.0, -3.0] * 50))
    assert out["verdict"] == "ok", out
    assert out["display_drift"]["p95_ms"] == pytest.approx(3.0)
    assert out["display_drift"]["bar_ms"] == DISPLAY_DRIFT_BAR_MS == 10.0
    assert out["delivery"] == {"sent": 400, "received": 400}


def test_drift_over_the_bar_fails_on_either_side_of_zero() -> None:
    assert score_mirror(*_pages([1.0] * 90 + [12.0] * 10))["verdict"] == "fail"
    assert score_mirror(*_pages([1.0] * 90 + [-12.0] * 10))["verdict"] == "fail", "a late display is as wrong as an early one"
    assert score_mirror(*_pages([9.9] * 100))["verdict"] == "ok", "control: just under the bar passes"


def test_no_samples_is_unknown_never_a_number() -> None:
    out = score_mirror(*_pages([], received=0))
    assert out["verdict"] == "unknown"
    assert out["display_drift"]["p95_ms"] is None
    assert "no position sample" in out["reason"]
    missing = score_mirror({"side": "inviter"}, {"side": "joiner"})
    assert missing["verdict"] == "unknown" and "mirror" in missing["reason"]


def test_the_plan_hop_is_reported() -> None:
    out = score_mirror(*_pages([0.0] * 10, plan=[0.1] * 20))
    assert out["plan_hop"]["n"] == 20
    assert out["plan_hop"]["p95_ms"] == pytest.approx(0.1)


def test_a_bias_run_must_read_the_bias_back() -> None:
    audio, control = _pages([20.5, 20.1, 20.3] * 30)
    audio["mirror"]["bias_ms"] = 20.0
    out = score_mirror(audio, control)
    assert out["bias_readback"] == {"expected_ms": 20.0, "read_p50_ms": 20.3, "ok": True}, "within 1 ms reads back"
    off, off_control = _pages([21.5] * 30)
    off["mirror"]["bias_ms"] = 20.0
    assert score_mirror(off, off_control)["verdict"] == "fail", "1.5 ms off the bias is a broken instrument"
    assert out["verdict"] == "ok", "a positive control is judged on its read-back, not the drift bar"
    audio2, control2 = _pages([0.1, -0.1] * 50)
    audio2["mirror"]["bias_ms"] = 20.0
    assert score_mirror(audio2, control2)["verdict"] == "fail", "control: reading 0 under a 20 ms bias fails"
