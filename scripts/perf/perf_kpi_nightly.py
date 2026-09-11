"""Nightly deck-load KPI capture against a throwaway engine (issue #1506)."""

from __future__ import annotations

import datetime as dt
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.perf.kpi_ceiling import CEILING_FACTOR, ceiling_exceeded, trailing_median_ms
from scripts.perf.kpi_ledger_append import append_entries, load_ledger
from scripts.perf.perf_kpi_config import PerfKpiConfig, kpi_name

ProbeResult = tuple[int, float]
ProbeFn = Callable[[str, str], ProbeResult]


@dataclass(frozen=True)
class WarmMedian:
    profile_key: str
    leg: str
    median_ms: float | None
    error: str | None
    denominator: str


@dataclass(frozen=True)
class NightlyOutcome:
    entries: list[dict[str, Any]]
    breaches: list[dict[str, Any]]
    exit_code: int


def warm_median_ms(samples: list[ProbeResult]) -> tuple[float | None, str | None]:
    if len(samples) < 2:
        return None, f"need at least 2 samples, got {len(samples)}"
    warm = [elapsed for status, elapsed in samples[1:] if 200 <= status < 300]
    if not warm:
        statuses = [status for status, _elapsed in samples[1:]]
        return None, f"no warm 2xx among samples 2..n (statuses={statuses})"
    return float(statistics.median(warm)), None


def default_probe(base_url: str, path: str, timeout_s: float = 30.0) -> ProbeResult:
    url = f"{base_url.rstrip('/')}{path}"
    request = urllib.request.Request(url, method="GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = response.status
            response.read()
    except urllib.error.HTTPError as error:
        return error.code, (time.perf_counter() - started) * 1000.0
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, (time.perf_counter() - started) * 1000.0
    return status, (time.perf_counter() - started) * 1000.0


def measure_track(
    config: PerfKpiConfig,
    base_url: str,
    profile_key: str,
    stable_id: str,
    *,
    probe: ProbeFn,
) -> list[WarmMedian]:
    results: list[WarmMedian] = []
    for leg in ("anlz", "audio"):
        samples = [
            probe(base_url, f"/api/v1/tracks/{stable_id}/{leg}")
            for _ in range(config.samples)
        ]
        median, error = warm_median_ms(samples)
        denom = f"n={max(0, len(samples) - 1)} warm of {len(samples)}"
        results.append(WarmMedian(profile_key, leg, median, error, denom))
    return results


def build_ledger_rows(
    config: PerfKpiConfig,
    *,
    capture_id: str,
    git_sha: str,
    today: dt.date,
    measurements: list[WarmMedian],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in measurements:
        name = kpi_name(item.leg, item.profile_key)
        common = {
            "date": today.isoformat(),
            "round": "perf-kpi-nightly",
            "kpi": name,
            "unit": "ms",
            "machine": config.machine,
            "source": "scripts/perf/perf_kpi_job.py nightly",
            "capture_id": capture_id,
            "denominator": item.denominator,
            "git_sha": git_sha,
        }
        if item.error is not None:
            rows.append(
                {
                    **common,
                    "value": None,
                    "status": "error",
                    "measured": False,
                    "note": item.error,
                }
            )
        else:
            rows.append(
                {
                    **common,
                    "value": item.median_ms,
                    "measured": True,
                    "note": f"warm median {item.leg} for {item.profile_key}",
                }
            )
    return rows


def find_ceiling_breaches(
    ledger_entries: list[dict[str, Any]],
    measurements: list[WarmMedian],
    *,
    today: dt.date,
) -> list[dict[str, Any]]:
    breaches: list[dict[str, Any]] = []
    for item in measurements:
        if item.leg != "anlz" or item.median_ms is None:
            continue
        name = kpi_name(item.leg, item.profile_key)
        trailing = trailing_median_ms(ledger_entries, kpi=name, today=today)
        if ceiling_exceeded(
            warm_median_ms=item.median_ms,
            trailing_median_ms=trailing,
            factor=CEILING_FACTOR,
        ):
            breaches.append(
                {
                    "kpi": name,
                    "warm_median_ms": item.median_ms,
                    "trailing_median_ms": trailing,
                    "factor": CEILING_FACTOR,
                    "profile_key": item.profile_key,
                }
            )
    return breaches


def append_history_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def file_ceiling_issue(
    breaches: list[dict[str, Any]],
    *,
    repository: str,
    git_sha: str,
) -> int:
    title = "perf KPI ceiling breach: warm anlz exceeds 3x trailing 7-day median"
    body_lines = [
        "Automatic filing from `com.af.perf-kpi-nightly`.",
        "",
        f"git_sha: `{git_sha}`",
        "",
        "| KPI | warm median ms | trailing 7d median ms | ceiling (3x) |",
        "|---|---:|---:|---:|",
    ]
    for breach in breaches:
        trailing = breach["trailing_median_ms"]
        trailing_text = "UNKNOWN" if trailing is None else f"{trailing:.1f}"
        warm = breach["warm_median_ms"]
        body_lines.append(
            f"| `{breach['kpi']}` | {warm:.1f} | {trailing_text} | {CEILING_FACTOR}x |"
        )
    body = "\n".join(body_lines)
    completed = subprocess.run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repository,
            "--title",
            title,
            "--body",
            body,
            "--label",
            "queue:ready",
            "--label",
            "queue:p0",
            "--label",
            "perf",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    number = completed.stdout.strip().rsplit("/", maxsplit=1)[-1]
    return int(number)


def run_nightly(
    config: PerfKpiConfig,
    *,
    base_url: str,
    git_sha: str,
    probe: ProbeFn,
    today: dt.date | None = None,
    repository: str = "maintainer/music-dj-tools",
    file_issue: bool = True,
) -> NightlyOutcome:
    today = today or dt.datetime.now(dt.UTC).date()
    capture_id = f"perf-kpi-{today.isoformat()}"
    measurements: list[WarmMedian] = []
    for track in config.tracks:
        measurements.extend(
            measure_track(config, base_url, track.key, track.stable_id, probe=probe)
        )
    rows = build_ledger_rows(
        config,
        capture_id=capture_id,
        git_sha=git_sha,
        today=today,
        measurements=measurements,
    )
    append_entries(config.ledger_path, rows)
    ledger = load_ledger(config.ledger_path)
    breaches = find_ceiling_breaches(ledger["entries"], measurements, today=today)
    exit_code = 0
    if breaches:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        append_history_row(
            config.history_log,
            {
                "ts": stamp,
                "event": "ceiling_breach",
                "machine": config.machine,
                "status": "fail",
                "git_sha": git_sha,
                "breaches": breaches,
            },
        )
        if file_issue:
            file_ceiling_issue(breaches, repository=repository, git_sha=git_sha)
        exit_code = 3
    return NightlyOutcome(entries=rows, breaches=breaches, exit_code=exit_code)
