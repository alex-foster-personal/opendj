"""LATENCY-03 quality evaluator: declared input-to-applied latency floor."""

from __future__ import annotations

import json
import re
from pathlib import Path

METRIC_KEY = "latency.input_to_applied_ms"
WIRING_FAIL_VALUE = 999.0

_SOURCE_PATHS = (
    "apps/webui/frontend/src/lib/player/eq-apply.ts",
    "apps/webui/frontend/src/lib/player/mixer-apply.ts",
    "apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts",
    "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts",
    "apps/webui/frontend/src/lib/rb/perf-event-buckets.ts",
)


def _read_floor(repo: Path) -> float:
    baseline = repo / "ops" / "quality" / "baseline.json"
    metrics = json.loads(baseline.read_text(encoding="utf-8"))["metrics"]
    floor = metrics.get(METRIC_KEY)
    if not isinstance(floor, (int, float)) or not (floor > 0) or floor != floor:
        raise RuntimeError(f"{METRIC_KEY} missing or not a positive finite number in baseline.json")
    return float(floor)


def _read_sources(repo: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in _SOURCE_PATHS:
        path = repo / rel
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise RuntimeError(f"latency wiring source {rel} is missing or empty")
        out[rel] = text
    return out


def _wiring_failures(sources: dict[str, str]) -> list[str]:
    failures: list[str] = []
    eq_apply = sources["apps/webui/frontend/src/lib/player/eq-apply.ts"]
    engine = sources["apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts"]
    ipc = sources["apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts"]
    buckets = sources["apps/webui/frontend/src/lib/rb/perf-event-buckets.ts"]

    if "linearRampToValueAtTime" not in eq_apply:
        failures.append("eq-apply.ts missing linearRampToValueAtTime")
    if "cancelScheduledValues" not in eq_apply:
        failures.append("eq-apply.ts missing cancelScheduledValues")
    if "EQ_APPLY_KIND" not in eq_apply and "'eq-apply'" not in eq_apply:
        failures.append("eq-apply.ts missing EQ_APPLY_KIND / eq-apply kind")

    if not re.search(r"setEq\([^)]*pressT0Ms", engine):
        failures.append("audio-engine setEq does not accept pressT0Ms")
    set_eq_body = re.search(r"setEq\([^)]*\)[^{]*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", engine)
    if set_eq_body is None:
        failures.append("audio-engine setEq body not found")
    elif "_setParam" in set_eq_body.group(1):
        failures.append("audio-engine setEq still calls _setParam")

    if "engine.setEq(command.deck, command.band, command.value, pressT0Ms)" not in ipc:
        failures.append("performance-ipc does not forward pressT0Ms into setEq")

    if "'eq-apply'" not in buckets:
        failures.append("perf-event-buckets missing eq-apply bucket")

    if not re.search(r"setFilter\([^)]*pressT0Ms", engine):
        failures.append("audio-engine setFilter does not accept pressT0Ms")
    if not re.search(r"setFader\([^)]*pressT0Ms", engine):
        failures.append("audio-engine setFader does not accept pressT0Ms")
    if not re.search(r"setCrossfader\([^)]*pressT0Ms", engine):
        failures.append("audio-engine setCrossfader does not accept pressT0Ms")
    if not re.search(r"setStemMute\([^)]*pressT0Ms", engine):
        failures.append("audio-engine setStemMute does not accept pressT0Ms")
    if not re.search(r"setStemSolo\([^)]*pressT0Ms", engine):
        failures.append("audio-engine setStemSolo does not accept pressT0Ms")

    if "engine.setFilter(command.deck, command.value, pressT0Ms)" not in ipc:
        failures.append("performance-ipc does not forward pressT0Ms into setFilter")
    if "engine.setFader(command.deck, command.value, pressT0Ms)" not in ipc:
        failures.append("performance-ipc does not forward pressT0Ms into setFader")
    if "engine.setCrossfader(command.value, pressT0Ms)" not in ipc:
        failures.append("performance-ipc does not forward pressT0Ms into setCrossfader")
    if "engine.setStemMute(command.deck, command.stem, command.muted, pressT0Ms)" not in ipc:
        failures.append("performance-ipc does not forward pressT0Ms into setStemMute")
    # Exclusive solo is an optional fifth argument. The event timestamp must
    # remain fourth; accepting an arbitrary trailing argument would hide drift.
    solo_call = (
        r"engine\.setStemSolo\(command\.deck, command\.stem, command\.solo, "
        r"pressT0Ms(?:, command\.exclusive)?\)"
    )
    if not re.search(solo_call, ipc):
        failures.append("performance-ipc does not forward pressT0Ms into setStemSolo")

    if "'mixer-apply'" not in buckets:
        failures.append("perf-event-buckets missing mixer-apply bucket")
    if "'filter-apply'" not in buckets and "filter-apply" not in buckets:
        failures.append("perf-event-buckets missing filter-apply kind routing")

    return failures


def evaluate(repo: Path) -> list[dict[str, object]]:
    """Return one Metric-shaped dict for the latency ratchet key."""
    floor = _read_floor(repo)
    sources = _read_sources(repo)
    failures = _wiring_failures(sources)
    if failures:
        detail = "LATENCY-03 wiring failed: " + "; ".join(failures)
        value = WIRING_FAIL_VALUE
    else:
        detail = (
            f"declared ceiling {floor}ms (input-to-applied, echoed not live-measured; "
            "see LATENCY-03)"
        )
        value = floor
    return [
        {
            "key": METRIC_KEY,
            "value": value,
            "unit": "ms",
            "detail": detail,
        }
    ]
