"""PERF-LEAK-11717 headless leak-cycle reducer (issue #10278).

[if] a 3 MB per cycle heap climb does not report 3.0 and exit 1, or a single returning spike
fails the run [then] fail, [else stop].
[if] a handle kind growing by 1 is not named with exit 1, or canvas growth fails the run
[then] fail, [else stop].
[if] fewer than 5 cycles prints a slope instead of "insufficient cycles" with exit 2 [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.perf.leak_cycle_report import INSUFFICIENT, main

pytestmark = [pytest.mark.requirement("PERF-LEAK-11717")]

_DEFAULT = object()
BASE_HANDLES = {"intervals": 10, "long_timeouts": 2, "window_listeners": 30, "rafs": 1}


def _cycle(n: int, heap: float, handles: object = _DEFAULT, canvas: int = 8) -> dict:
    return {
        "cycle": n,
        "js_heap_used_mb": heap,
        "live_handles": dict(BASE_HANDLES) if handles is _DEFAULT else handles,
        "canvas_count": canvas,
    }


def _write(tmp_path: Path, cycles: list[dict]) -> Path:
    path = tmp_path / "leak.json"
    path.write_text(json.dumps({"schema": "leak-cycle-probe/1", "cycles": cycles}), encoding="utf-8")
    return path


def _run(path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    code = main(["--result", str(path), *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def _flat(n: int = 12) -> list[dict]:
    return [_cycle(i, 100.0) for i in range(1, n + 1)]


def test_a_heap_climbing_3_mb_per_cycle_from_cycle_3_reports_3_0_and_exits_1(tmp_path, capsys):
    cycles = [_cycle(1, 80.0), _cycle(2, 95.0)] + [_cycle(n, 100.0 + 3 * (n - 3)) for n in range(3, 13)]
    path = _write(tmp_path, cycles)
    code, out, _ = _run(path, capsys, "--json")
    assert code == 1
    assert json.loads(out)["js_heap_slope_mb_per_cycle"] == pytest.approx(3.0)
    code, out, _ = _run(path, capsys)
    assert code == 1
    assert "js_heap_slope_mb_per_cycle 3.0" in out


def test_a_single_50_mb_spike_that_returns_is_a_fit_not_a_max_and_exits_0(tmp_path, capsys):
    cycles = _flat()
    cycles[4] = _cycle(5, 150.0)
    code, out, _ = _run(_write(tmp_path, cycles), capsys, "--json")
    assert code == 0
    assert abs(json.loads(out)["js_heap_slope_mb_per_cycle"]) < 2


def test_a_handle_kind_growing_by_one_is_named_and_exits_1(tmp_path, capsys):
    cycles = _flat()
    cycles[-1] = _cycle(12, 100.0, {**BASE_HANDLES, "intervals": 11})
    path = _write(tmp_path, cycles)
    code, out, err = _run(path, capsys)
    assert code == 1
    assert "intervals +1" in out
    assert "intervals" in err
    code, out, _ = _run(path, capsys, "--json")
    assert code == 1
    assert json.loads(out)["handles_delta"]["intervals"] == 1


def test_canvas_growth_is_reported_but_never_fails(tmp_path, capsys):
    cycles = [_cycle(n, 100.0, canvas=8 if n <= 3 else 20) for n in range(1, 13)]
    code, out, _ = _run(_write(tmp_path, cycles), capsys, "--json")
    assert code == 0
    assert json.loads(out)["canvas_delta"] == 12


def test_fewer_than_5_cycles_is_insufficient_and_exits_2_without_a_slope(tmp_path, capsys):
    path = _write(tmp_path, _flat(4))
    code, out, _ = _run(path, capsys)
    assert code == 2
    assert INSUFFICIENT in out
    code, out, _ = _run(path, capsys, "--json")
    assert code == 2
    payload = json.loads(out)
    assert payload["js_heap_slope_mb_per_cycle"] is None
    assert payload["status"] == INSUFFICIENT


def test_missing_live_handles_are_reported_unavailable_and_do_not_fail(tmp_path, capsys):
    path = _write(tmp_path, [_cycle(n, 100.0, None) for n in range(1, 13)])
    code, out, err = _run(path, capsys)
    assert code == 0
    assert "live handles unavailable" in out
    assert "live handles unavailable" in err
    code, out, _ = _run(path, capsys, "--json")
    assert json.loads(out)["handles_available"] is False


def test_the_slope_ignores_the_two_warm_cycles(tmp_path, capsys):
    cycles = [_cycle(1, 0.0), _cycle(2, 500.0), *_flat()[2:]]
    code, out, _ = _run(_write(tmp_path, cycles), capsys, "--json")
    assert code == 0
    assert json.loads(out)["js_heap_slope_mb_per_cycle"] == pytest.approx(0.0)


def test_custom_limits_apply(tmp_path, capsys):
    climb = [_cycle(n, 100.0 + 3 * n) for n in range(1, 13)]
    code, _, _ = _run(_write(tmp_path, climb), capsys, "--max-slope-mb", "5")
    assert code == 0
    grow = _flat()
    grow[-1] = _cycle(12, 100.0, {**BASE_HANDLES, "intervals": 11})
    code, _, _ = _run(_write(tmp_path, grow), capsys, "--max-handle-growth", "1")
    assert code == 0


def test_a_cycle_without_a_heap_reading_is_refused(tmp_path, capsys):
    cycles = _flat()
    cycles[3]["js_heap_used_mb"] = None
    with pytest.raises(SystemExit) as exc:
        main(["--result", str(_write(tmp_path, cycles))])
    assert exc.value.code == 2
