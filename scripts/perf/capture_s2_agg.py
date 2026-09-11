"""Pure aggregation for S2 press-to-audible KPI capture."""

from __future__ import annotations

from typing import Any

from scripts.perf.capture_kpi_ledger import (
    CaptureMeta,
    build_row,
    withheld_row,
)

S2_KPI = "input_to_audible_ms_p99"
FLOOR_KPI = "audio_output_device_floor_ms"
S2_UNIT = "ms"
S2_METHOD_TEMPLATE = (
    "Playwright {engine} ipc.dispatch play/pause with performance.now() pressT0Ms; "
    "nearest-rank p99 of transport-schedule-press input_to_output_ms "
    "(press + scheduled offset + device floor); autosync off"
)


def nearest_rank_p99(values: list[float]) -> float:
    """Nearest-rank p99, matching scripts/diagnostics/probe_log_store.py."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * 0.99)))
    return ordered[index]


def _is_finite_positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def counts_as_sample(press: dict[str, Any]) -> bool:
    """True when a press row may contribute to the p99 sample."""
    if press.get("kind") != "transport-schedule-press":
        return False
    stages = press.get("stages")
    if not isinstance(stages, dict):
        return False
    if not _is_finite_positive(stages.get("input_to_output_ms")):
        return False
    if not _is_finite_positive(stages.get("base_latency_ms")):
        return False
    if not _is_finite_positive(stages.get("output_latency_ms")):
        return False
    labels = press.get("labels")
    return not (isinstance(labels, dict) and labels.get("latency_floor") != "complete")


def filter_complete_presses(presses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [press for press in presses if counts_as_sample(press)]


def device_floor_ms(sample: dict[str, Any]) -> float:
    stages = sample["stages"]
    base = stages["base_latency_ms"]
    output = stages["output_latency_ms"]
    return round(float(base) + float(output), 2)


def _method(engine: str) -> str:
    return S2_METHOD_TEMPLATE.format(engine=engine or "unknown")


def withheld_rows(
    reason: str, meta: CaptureMeta, *, engine: str = "unknown"
) -> list[dict[str, Any]]:
    method = _method(engine)
    return [
        withheld_row(kpi=S2_KPI, unit=S2_UNIT, method=method, meta=meta, reason=reason),
        withheld_row(kpi=FLOOR_KPI, unit=S2_UNIT, method=method, meta=meta, reason=reason),
    ]


def presses_to_ledger_rows(
    result: dict[str, Any] | None,
    meta: CaptureMeta,
    *,
    requested_presses: int = 32,
) -> list[dict[str, Any]]:
    """Convert a Playwright capture result into one or two ledger rows."""
    engine = "unknown"
    if isinstance(result, dict):
        raw_engine = result.get("engine")
        if isinstance(raw_engine, str) and raw_engine.strip():
            engine = raw_engine.strip()

    if result is None:
        return withheld_rows(
            "playwright capture did not write KPI_CAPTURE_RESULT",
            meta,
            engine=engine,
        )

    if result.get("ok") is not True:
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "S2 capture failed without a reason"
        return withheld_rows(reason, meta, engine=engine)

    raw_presses = result.get("presses")
    if not isinstance(raw_presses, list):
        return withheld_rows("S2 capture returned no press rows", meta, engine=engine)

    samples = filter_complete_presses(raw_presses)
    if len(samples) < requested_presses:
        return withheld_rows(
            (
                f"fewer than {requested_presses} complete-floor ordinary presses "
                f"(got {len(samples)})"
            ),
            meta,
            engine=engine,
        )

    scored = samples[:requested_presses]
    values = [float(item["stages"]["input_to_output_ms"]) for item in scored]
    p99 = nearest_rank_p99(values)
    floor = device_floor_ms(scored[-1])
    method = _method(engine)
    note = (
        f"n={requested_presses} presses, nearest-rank p99; engine={engine}; "
        f"audio_output_device_floor_ms={floor}"
    )
    return [
        build_row(
            kpi=S2_KPI,
            value=p99,
            unit=S2_UNIT,
            method=method,
            meta=meta,
            note=note,
        ),
        build_row(
            kpi=FLOOR_KPI,
            value=floor,
            unit=S2_UNIT,
            method=method,
            meta=meta,
            note=note,
        ),
    ]
