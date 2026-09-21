"""Nightly deck-load KPI capture against a throwaway engine (issue #1506)."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.perf.capture_kpi_ledger import format_appended
from scripts.perf.capture_kpis import capture_s5_against_engine
from scripts.perf.kpi_ceiling import CEILING_FACTOR, ceiling_verdict, trailing_median_ms
from scripts.perf.kpi_ledger_append import append_entries, load_ledger
from scripts.perf.perf_kpi_config import REPO_ROOT, PerfKpiConfig, kpi_name

ENGINE_READY_TIMEOUT_S = 90.0
ENGINE_STOP_GRACE_S = 5.0
SCRATCH_ENGINE_LOG_NAME = "scratch-engine.log"

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
    unknowns: list[dict[str, Any]]
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
        if ceiling_verdict(
            warm_median_ms=item.median_ms,
            trailing_median_ms=trailing,
            factor=CEILING_FACTOR,
        ) == "breach":
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


def find_ceiling_unknowns(
    ledger_entries: list[dict[str, Any]],
    measurements: list[WarmMedian],
    *,
    today: dt.date,
) -> list[dict[str, Any]]:
    unknowns: list[dict[str, Any]] = []
    for item in measurements:
        if item.leg != "anlz" or item.median_ms is None:
            continue
        name = kpi_name(item.leg, item.profile_key)
        trailing = trailing_median_ms(ledger_entries, kpi=name, today=today)
        if ceiling_verdict(
            warm_median_ms=item.median_ms,
            trailing_median_ms=trailing,
            factor=CEILING_FACTOR,
        ) == "unknown":
            unknowns.append(
                {
                    "kpi": name,
                    "warm_median_ms": item.median_ms,
                    "trailing_median_ms": None,
                    "factor": CEILING_FACTOR,
                    "profile_key": item.profile_key,
                    "reason": "no trailing 7-day median",
                }
            )
    return unknowns


def _track_stable_id(config: PerfKpiConfig, key: str) -> str | None:
    for track in config.tracks:
        if track.key == key:
            return track.stable_id
    return None


def append_history_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _port_is_bound(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _health_ok(base_url: str, timeout_s: float = 2.0) -> bool:
    url = f"{base_url.rstrip('/')}/api/v1/health"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def _engine_unavailable(
    config: PerfKpiConfig,
    reason: str,
    *,
    engine_log: Path | None = None,
) -> int:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    row: dict[str, Any] = {
        "ts": stamp,
        "event": "engine_unavailable",
        "machine": config.machine,
        "status": "fail",
        "reason": reason,
    }
    if engine_log is not None:
        row["engine_log"] = str(engine_log)
    append_history_row(config.history_log, row)
    message = f"perf KPI nightly: engine unavailable: {reason}"
    if engine_log is not None:
        message = f"{message} (log: {engine_log})"
    print(message, file=sys.stderr)
    return 1


def stop_scratch_engine(proc: subprocess.Popen[Any]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=ENGINE_STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _scratch_engine_argv(data_dir: Path, port: int) -> list[str] | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    return [
        uv,
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.engine_core",
        "serve",
        "--data-dir",
        str(data_dir),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def _state_db_problem(state_db: Path) -> str | None:
    """Why ``state_db`` cannot be measured against, or None when it can.

    The engine boots happily on a 0-byte file (it migrates it into a fresh,
    empty library), reports healthy, and the nightly then measures nothing
    and exits 0. That is a false green: neither an empty file nor a library
    with no tracks is the operator's library. Refuse before boot so the run
    records ``engine_unavailable`` instead of a clean ledger with no rows.
    """
    if not state_db.is_file():
        return f"missing state database: {state_db}"
    try:
        with state_db.open("rb") as handle:
            header = handle.read(16)
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA schema_version").fetchone()
            if header != b"SQLite format 3\x00":
                return f"state database is not a SQLite file: {state_db}"
            try:
                (track_rows,) = connection.execute("SELECT COUNT(*) FROM tracks").fetchone()
            except sqlite3.OperationalError:
                return f"state database has no tracks table: {state_db}"
    except (OSError, sqlite3.DatabaseError):
        return f"state database is not a SQLite file: {state_db}"
    # A migrated-but-empty library parses fine and boots fine; measuring it
    # yields only unmeasured rows and a clean exit (Devin P2 on PR #3726).
    # The denominator has to be a library with tracks in it.
    if track_rows == 0:
        return f"state database has no tracks: {state_db}"
    return None


def acquire_nightly_engine(
    config: PerfKpiConfig,
    *,
    base_url: str | None,
) -> tuple[str | None, subprocess.Popen[Any] | None, Path | None, int]:
    if base_url is not None:
        return base_url.rstrip("/"), None, None, 0

    data_dir = config.data_dir
    if data_dir is None:
        return None, None, None, _engine_unavailable(config, "MDT_PERF_KPI_DATA_DIR is unset")

    state_db = data_dir / "state" / "state.db"
    state_db_problem = _state_db_problem(state_db)
    if state_db_problem is not None:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        log_path = config.state_dir / SCRATCH_ENGINE_LOG_NAME
        log_path.touch()
        return (
            None,
            None,
            log_path,
            _engine_unavailable(config, state_db_problem, engine_log=log_path),
        )

    port = config.scratch_port
    if _port_is_bound(port):
        return (
            None,
            None,
            None,
            _engine_unavailable(
                config,
                "already bound; refusing to reuse a foreign engine",
            ),
        )

    argv = _scratch_engine_argv(data_dir, port)
    if argv is None:
        path_var = os.environ.get("PATH", "")
        return (
            None,
            None,
            None,
            _engine_unavailable(config, f"uv not found on PATH ({path_var})"),
        )

    config.state_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.state_dir / SCRATCH_ENGINE_LOG_NAME
    url = f"http://127.0.0.1:{port}"
    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + ENGINE_READY_TIMEOUT_S
    while True:
        if proc.poll() is not None:
            stop_scratch_engine(proc)
            return (
                None,
                None,
                log_path,
                _engine_unavailable(
                    config,
                    f"engine exited before healthy (code={proc.returncode})",
                    engine_log=log_path,
                ),
            )
        if _health_ok(url):
            return url, proc, log_path, 0
        if time.monotonic() >= deadline:
            stop_scratch_engine(proc)
            return (
                None,
                None,
                log_path,
                _engine_unavailable(
                    config,
                    f"engine not healthy within {ENGINE_READY_TIMEOUT_S}s",
                    engine_log=log_path,
                ),
            )
        time.sleep(0.25)


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
    s5_rows = capture_s5_against_engine(
        engine=base_url,
        ledger=config.ledger_path,
        data_dir=config.data_dir,
        small=_track_stable_id(config, "small_mp3"),
        large=_track_stable_id(config, "large_mp3"),
        stemmed=_track_stable_id(config, "stemmed_mp3"),
        sha=git_sha,
    )
    for s5_row in s5_rows:
        print(format_appended(s5_row))
    ledger = load_ledger(config.ledger_path)
    breaches = find_ceiling_breaches(ledger["entries"], measurements, today=today)
    unknowns = find_ceiling_unknowns(ledger["entries"], measurements, today=today)
    exit_code = 0
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if breaches:
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
    if unknowns:
        append_history_row(
            config.history_log,
            {
                "ts": stamp,
                "event": "ceiling_unknown",
                "machine": config.machine,
                "status": "ok",
                "git_sha": git_sha,
                "reason": "no trailing 7-day median",
                "unknowns": unknowns,
            },
        )
    return NightlyOutcome(
        entries=rows,
        breaches=breaches,
        unknowns=unknowns,
        exit_code=exit_code,
    )
