"""Capture Gig vs Library steady-state footprint and CPU ratios (PERFMODE-14).

Reference Mac only for scored ledger rows. Linux exits 69 UNAVAILABLE before sampling.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.perf.capture_build_identity import _git_sha
from scripts.perf.capture_kpi_ledger import append_entries, format_appended
from scripts.perf.capture_library_targets import _verify_capture_targets

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.library-mode-perf.config.ts"
_EXIT_UNAVAILABLE = 69
_DEFAULT_CAPTURE_TIMEOUT_S = 600
# PERFMODE-14's required post-switch settling period before a dwell is read,
# AND the required length of the dwell itself (performance-register.md's
# "60 s settle" rows). A shorter --settle-seconds or --dwell-seconds is a
# legitimate debug run, but its samples must never enter the scorecard as
# release evidence (Sol P1, PR #4034, discussion_r4128465144; Codex P1, PR
# #4034: settle conforming alone is not the floor -- a --settle-seconds 60
# with a --dwell-seconds 10 still scored until both periods were checked).
_MIN_SCORED_SETTLE_SECONDS = 60

# Codex P1/BLOCKING, PR #4034, discussion_r4138153190: the Playwright spec's
# own floor (library-mode-perf-capture.spec.ts's MIN_SAMPLE_FRACTION) is
# relative to `expectedTicks`, which collapses to 1 when
# KPI_CAPTURE_SAMPLE_INTERVAL_S is set to the full dwell length or longer --
# a single reading would then satisfy that floor and could still flip
# LIB-MODE/PERFMODE-14 to PASS without establishing steady-state behavior.
# This is an ABSOLUTE floor, independent of whatever interval produced the
# samples: at the documented default (60s dwell, 5s interval) a conforming
# capture yields about 12 ticks per mode, so 6 is a conservative minimum that
# only a materially widened interval (or heavy sample failures) can miss.
_MIN_SCORED_SAMPLES = 6


def _machine_name() -> str:
    return socket.gethostname().split(".")[0]


def _require_reference_mac(dry_run: bool) -> None:
    if dry_run:
        return
    if platform.system() != "Darwin":
        print(
            "library mode KPI capture requires the reference Mac (Darwin); "
            "Linux CI proves schema only",
            file=sys.stderr,
        )
        raise SystemExit(_EXIT_UNAVAILABLE)


def _build_capture_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"issue-2700-library-mode-{stamp}"


def _ledger_rows(
    *,
    capture_id: str,
    machine: str,
    app_build_sha: str,
    gig_footprint_mb: float,
    library_footprint_mb: float,
    gig_cpu_percent: float,
    library_cpu_percent: float,
    measured: bool = True,
    note: str | None = None,
) -> list[dict[str, Any]]:
    # A ratio over a zero Gig baseline is undefined, not infinite or zero: an
    # idle Gig (decks loaded, nothing playing) can read 0.0% CPU on every tick.
    for name, baseline in (
        ("gig median_footprint_mb", gig_footprint_mb),
        ("gig median_cpu_percent", gig_cpu_percent),
    ):
        if baseline <= 0:
            raise ValueError(f"{name} is {baseline}; a Library/Gig ratio over it is undefined")
    footprint_ratio = library_footprint_mb / gig_footprint_mb
    cpu_ratio = library_cpu_percent / gig_cpu_percent
    if note is None:
        note = (
            f"capture_id={capture_id} app_build_sha={app_build_sha} "
            "method=scripts/perf/capture_library_mode.py dwell_seconds=60"
        )
    today = datetime.now(UTC).date().isoformat()
    return [
        {
            "date": today,
            "round": capture_id,
            "kpi": "library_mode_footprint_ratio",
            "unit": "ratio",
            "value": round(footprint_ratio, 4),
            "machine": machine,
            "source": "capture_library_mode",
            "note": note,
            "measured": measured,
            "capture_id": capture_id,
        },
        {
            "date": today,
            "round": capture_id,
            "kpi": "library_mode_cpu_ratio",
            "unit": "ratio",
            "value": round(cpu_ratio, 4),
            "machine": machine,
            "source": "capture_library_mode",
            "note": note,
            "measured": measured,
            "capture_id": capture_id,
        },
    ]


def _run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    data_dir: str,
    capture_id: str,
    timeout_s: int,
    dwell_seconds: int,
    settle_seconds: int,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        result_path = handle.name
    env = os.environ.copy()
    env["PERFORMANCE_E2E_START_SERVERS"] = "0"
    env["PERFORMANCE_E2E_FIXTURE"] = "0"
    env["PERFORMANCE_E2E_BASE_URL"] = frontend.rstrip("/")
    env["PERFORMANCE_E2E_API_BASE"] = engine.rstrip("/")
    env["MDT_DATA_DIR"] = data_dir
    env["KPI_CAPTURE_RESULT"] = result_path
    env["KPI_CAPTURE_TIMEOUT_S"] = str(timeout_s)
    env["KPI_CAPTURE_ID"] = capture_id
    env["KPI_CAPTURE_DWELL_SECONDS"] = str(dwell_seconds)
    env["KPI_CAPTURE_SETTLE_SECONDS"] = str(settle_seconds)
    proc = subprocess.run(
        [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--config",
            _PLAYWRIGHT_CONFIG,
        ],
        cwd=_FRONTEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        raw = Path(result_path).read_text(encoding="utf-8")
        result = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        result = {
            "ok": False,
            "reason": (
                f"playwright capture did not write KPI_CAPTURE_RESULT (exit {proc.returncode})"
            ),
        }
    finally:
        Path(result_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        result["ok"] = False
        result["reason"] = (
            result.get("reason")
            or f"playwright exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return result


def _rows_from_capture_result(
    result: dict[str, Any],
    *,
    capture_id: str,
    machine: str,
    app_build_sha: str,
    dwell_seconds: int,
    settle_seconds: int,
    frontend_mode: str,
) -> list[dict[str, Any]]:
    gig = result.get("gig") if isinstance(result.get("gig"), dict) else None
    library = result.get("library") if isinstance(result.get("library"), dict) else None
    if gig is None or library is None:
        raise ValueError(result.get("reason") or "capture result missing gig/library medians")
    # Sol P1/BLOCKING (PR #4034, discussion_r4148668268): a tick that threw
    # during the dwell was silently dropped from every sample array once
    # enough OTHER ticks cleared the minimum-sample floor, so a mode-
    # correlated failure (more likely in the heavier phase) could thin one
    # phase's denominator without leaving a trace on an otherwise-clean
    # `measured: true` row. Refuse to score any row where either phase
    # reports a failure, rather than silently averaging over survivors.
    # Sol P1/BLOCKING, PR #4540: a missing count is producer drift, not zero
    # failures, so it is refused rather than defaulted.
    for mode, capture in (("gig", gig), ("library", library)):
        failure_count = capture.get("sample_failure_count")
        if type(failure_count) is not int or failure_count < 0:
            raise ValueError(
                f"{mode} phase sample_failure_count must be a nonnegative int, got "
                f"{failure_count!r}; refusing to treat an unreported count as zero failures"
            )
        if failure_count:
            raise ValueError(
                f"{mode} phase had {failure_count} failed sample tick(s) during the dwell; "
                "refusing to score a row over a partial, possibly mode-biased denominator"
            )
    sampling_method = result.get("sampling_method")
    method_note = (
        f"sampling_method={sampling_method}"
        if isinstance(sampling_method, str)
        else "method=scripts/perf/capture_library_mode.py"
    )
    sample_note = _sample_count_note(gig, library)
    # A debug run under the required 60 s settle OR dwell is real data for
    # iterating, but it must never enter the scorecard as PERFMODE-14 release
    # evidence (Sol P1, PR #4034, discussion_r4128465144; Codex P1, PR #4034):
    # mark it unmeasured rather than silently scoring a run that skipped
    # either documented floor. A conforming settle does not excuse a
    # shortened dwell, or the reverse.
    settle_met = settle_seconds >= _MIN_SCORED_SETTLE_SECONDS
    dwell_met = dwell_seconds >= _MIN_SCORED_SETTLE_SECONDS
    stable_ids_met = _valid_deck_stable_ids(result) is not None
    samples_met = _min_samples_met(gig, library)
    measured = settle_met and dwell_met and stable_ids_met and samples_met
    note = (
        f"capture_id={capture_id} app_build_sha={app_build_sha} "
        f"settle_seconds={settle_seconds} dwell_seconds={dwell_seconds} "
        f"frontend_mode={frontend_mode} {sample_note} {_rss_ratio_note(gig, library)} "
        f"{_raw_medians_note(gig, library)} {_deck_stable_ids_note(result)} "
        f"{method_note}"
    )
    if not measured:
        short_periods = ", ".join(
            f"{name}={value}"
            for name, value, met in (
                ("settle_seconds", settle_seconds, settle_met),
                ("dwell_seconds", dwell_seconds, dwell_met),
            )
            if not met
        )
        reasons = [part for part in (short_periods,) if part]
        if not stable_ids_met:
            reasons.append(f"deck_stable_ids invalid (need exactly {_DECKS_PER_CAPTURE})")
        if not samples_met:
            reasons.append(f"insufficient samples ({_min_sample_counts_note(gig, library)})")
        note += (
            f" UNMEASURED: {', '.join(reasons)}; either below the required "
            f"{_MIN_SCORED_SETTLE_SECONDS}s floor or missing the four-deck denominator "
            "proof; this run does not score as PERFMODE-14 release evidence"
        )
    return _ledger_rows(
        capture_id=capture_id,
        machine=machine,
        app_build_sha=app_build_sha,
        gig_footprint_mb=float(gig["median_footprint_mb"]),
        library_footprint_mb=float(library["median_footprint_mb"]),
        gig_cpu_percent=float(gig["median_cpu_percent"]),
        library_cpu_percent=float(library["median_cpu_percent"]),
        measured=measured,
        note=note,
    )


def _rss_ratio_note(gig: dict[str, Any], library: dict[str, Any]) -> str:
    """The same pids' RSS ratio, for continuity with rows captured before phys_footprint."""
    if "median_rss_mb" not in gig or "median_rss_mb" not in library:
        raise ValueError(
            "capture result has no median_rss_mb; the sampler predates pid-tree attribution"
        )
    gig_rss = float(gig["median_rss_mb"])
    library_rss = float(library["median_rss_mb"])
    if gig_rss <= 0:
        raise ValueError(f"gig median_rss_mb is {gig_rss}; an RSS ratio over it is undefined")
    return f"rss_ratio={library_rss / gig_rss:.4f}"


def _raw_medians_note(gig: dict[str, Any], library: dict[str, Any]) -> str:
    """The raw medians the ratio was computed FROM, not just the ratio itself.

    Codex P1/BLOCKING, PR #4034, discussion_r4131907743: `main()` printed these
    to stdout and then deleted the temporary KPI_CAPTURE_RESULT file, so a
    committed ledger row that marks PERFMODE-14 met carried only the derived
    ratio -- nothing an auditor could recompute or sanity-check the ratio
    against, or compare a later capture's medians to, once the ephemeral
    stdout log was gone.
    """
    return (
        f"gig_median_footprint_mb={float(gig['median_footprint_mb']):.1f} "
        f"library_median_footprint_mb={float(library['median_footprint_mb']):.1f} "
        f"gig_median_cpu_percent={float(gig['median_cpu_percent']):.2f} "
        f"library_median_cpu_percent={float(library['median_cpu_percent']):.2f}"
    )


_DECKS_PER_CAPTURE = 4


def _valid_deck_stable_ids(result: dict[str, Any]) -> list[str] | None:
    """The capture's `stable_ids`, or `None` if it is not exactly four
    non-empty strings.

    Codex P1/BLOCKING, PR #4034, discussion_r4138030257: `result.get(
    "stable_ids")` used to render straight into the note, including `None`
    or a malformed value, while the row could still be marked measured and
    score LIB-MODE as PASS -- a missing or malformed result masked contract
    drift and left no proof the denominator was Gig with four loaded decks.
    """
    ids = result.get("stable_ids")
    if not isinstance(ids, list) or len(ids) != _DECKS_PER_CAPTURE:
        return None
    if not all(isinstance(stable_id, str) and stable_id for stable_id in ids):
        return None
    return ids


def _deck_stable_ids_note(result: dict[str, Any]) -> str:
    """Which exact tracks were loaded, so a row can be reproduced or audited.

    Codex P1/BLOCKING, PR #4034, discussion_r4131907743: `deck_stable_ids` was
    printed to stdout alongside the medians above and never retained anywhere
    a committed row points to.
    """
    ids = _valid_deck_stable_ids(result)
    if ids is None:
        raw = result.get("stable_ids")
        return f"deck_stable_ids=INVALID (raw={raw!r}, need exactly {_DECKS_PER_CAPTURE})"
    return f"deck_stable_ids={ids!r}"


_SAMPLE_FIELDS = ("footprint_samples_mb", "cpu_samples_percent")


def _sample_field_counts(gig: dict[str, Any], library: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Real sample count per (mode, field); zero is a refusal, not a row.

    Codex P1/BLOCKING, PR #4034, discussion at sha=09612c7a6f: the prior
    version of this function counted only `footprint_samples_mb` and used
    that single count to represent BOTH fields, so a capture whose
    `cpu_samples_percent` list was much shorter than its footprint list
    (fewer CPU ticks landed, more footprint ticks did) would pass a sample
    floor that never actually looked at the CPU count. Every (mode, field)
    pair is counted independently.
    """
    counts: dict[str, dict[str, int]] = {}
    for mode, capture in (("gig", gig), ("library", library)):
        counts[mode] = {}
        for field in _SAMPLE_FIELDS:
            samples = capture.get(field)
            if not isinstance(samples, list) or len(samples) == 0:
                raise ValueError(
                    f"{mode} {field} holds no samples; a median over none is not a capture"
                )
            counts[mode][field] = len(samples)
    return counts


def _sample_count_note(gig: dict[str, Any], library: dict[str, Any]) -> str:
    """Name how many real samples each median came from."""
    counts = _sample_field_counts(gig, library)
    return (
        f"samples_gig={counts['gig']['footprint_samples_mb']} "
        f"samples_library={counts['library']['footprint_samples_mb']}"
    )


def _min_samples_met(gig: dict[str, Any], library: dict[str, Any]) -> bool:
    """Every (mode, field) count must clear `_MIN_SCORED_SAMPLES`, not merely
    be non-empty, and not merely the footprint field (see
    `_sample_field_counts`)."""
    counts = _sample_field_counts(gig, library)
    return all(
        counts[mode][field] >= _MIN_SCORED_SAMPLES
        for mode in ("gig", "library")
        for field in _SAMPLE_FIELDS
    )


def _min_sample_counts_note(gig: dict[str, Any], library: dict[str, Any]) -> str:
    """The exact counts behind an `insufficient samples` UNMEASURED reason."""
    counts = _sample_field_counts(gig, library)
    parts = ", ".join(
        f"{mode}.{field}={counts[mode][field]}"
        for mode in ("gig", "library")
        for field in _SAMPLE_FIELDS
    )
    return f"{parts} (need >={_MIN_SCORED_SAMPLES} each)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture PERFMODE-14 library mode KPI ratios")
    parser.add_argument("--engine", help="Backend base URL (alias: --api-base)")
    parser.add_argument("--frontend", help="Frontend base URL (alias: --base-url)")
    parser.add_argument("--api-base", help="Backend base URL")
    parser.add_argument("--base-url", help="Frontend base URL")
    parser.add_argument("--data-dir", required=True, help="Real library data directory")
    parser.add_argument("--dwell-seconds", type=int, default=60)
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=60,
        help="Seconds to wait in EACH mode before its dwell (Library is read at 60 s)",
    )
    parser.add_argument("--timeout-s", type=int, default=_DEFAULT_CAPTURE_TIMEOUT_S)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=_REPO / "docs" / "perf" / "kpi-ledger.json",
    )
    parser.add_argument("--machine-tag", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gig-footprint-mb", type=float, default=None)
    parser.add_argument("--library-footprint-mb", type=float, default=None)
    parser.add_argument("--gig-cpu-percent", type=float, default=None)
    parser.add_argument("--library-cpu-percent", type=float, default=None)
    args = parser.parse_args(argv)

    if args.dry_run:
        _require_reference_mac(True)
        capture_id = _build_capture_id()
        machine = args.machine_tag or _machine_name()
        rows = _ledger_rows(
            capture_id=capture_id,
            machine=machine,
            app_build_sha=_git_sha(),
            gig_footprint_mb=1000.0,
            library_footprint_mb=500.0,
            gig_cpu_percent=10.0,
            library_cpu_percent=3.0,
            measured=False,
            note=(
                f"capture_id={capture_id} app_build_sha={_git_sha()} "
                "method=scripts/perf/capture_library_mode.py --dry-run "
                "(hardcoded placeholder values, not a capture)"
            ),
        )
        print(json.dumps({"capture_id": capture_id, "rows": rows}, indent=2))
        return 0

    _require_reference_mac(False)

    engine = args.engine or args.api_base
    frontend = args.frontend or args.base_url
    if engine is None or frontend is None:
        parser.error("--engine and --frontend are required (or --api-base and --base-url)")

    manual = (
        args.gig_footprint_mb,
        args.library_footprint_mb,
        args.gig_cpu_percent,
        args.library_cpu_percent,
    )
    capture_id = _build_capture_id()
    machine = args.machine_tag or _machine_name()
    app_build_sha = _git_sha()

    if all(value is not None for value in manual):
        rows = _ledger_rows(
            capture_id=capture_id,
            machine=machine,
            app_build_sha=app_build_sha,
            gig_footprint_mb=float(args.gig_footprint_mb),
            library_footprint_mb=float(args.library_footprint_mb),
            gig_cpu_percent=float(args.gig_cpu_percent),
            library_cpu_percent=float(args.library_cpu_percent),
            measured=False,
            note=(
                f"capture_id={capture_id} app_build_sha={app_build_sha} "
                "method=scripts/perf/capture_library_mode.py "
                "values supplied by hand on the command line (not a dwell capture)"
            ),
        )
        append_entries(args.ledger, rows)
        for row in rows:
            print(format_appended(row))
        return 0

    targets_reason, frontend_mode, engine_pid = _verify_capture_targets(
        engine, frontend, app_build_sha
    )
    if targets_reason is not None:
        print(targets_reason, file=sys.stderr)
        return 1
    assert frontend_mode is not None  # guaranteed by _verify_capture_targets on success
    assert engine_pid is not None  # guaranteed by _verify_capture_targets on success

    result = _run_playwright_capture(
        engine=engine.rstrip("/"),
        frontend=frontend.rstrip("/"),
        data_dir=args.data_dir,
        capture_id=capture_id,
        timeout_s=args.timeout_s,
        dwell_seconds=args.dwell_seconds,
        settle_seconds=args.settle_seconds,
    )
    if result.get("ok") is not True:
        print(result.get("reason") or "library mode capture failed", file=sys.stderr)
        return 1

    # Codex P1/BLOCKING, PR #4034, discussion_r4138297594 then discussion_r4138422256:
    # the identity and checkout-cleanliness gates above only prove the
    # engine/frontend/checkout were correct BEFORE Playwright started. A
    # capture normally dwells for minutes; if the engine restarts (even from
    # the SAME clean commit) or the frontend's served files are replaced
    # mid-capture, the process-tree sampler happily follows the new engine pid
    # and still returns enough samples, and the resulting rows would be
    # recorded under `app_build_sha` even though the capture mixed builds or
    # process lifetimes. Re-run every one of those gates -- including the
    # engine's own pid, pinned from the pre-capture call above -- right after
    # Playwright finishes, before any row is built or appended.
    post_capture_reason, _post_capture_frontend_mode, _post_capture_engine_pid = (
        _verify_capture_targets(engine, frontend, app_build_sha, expected_engine_pid=engine_pid)
    )
    if post_capture_reason is not None:
        print(
            "post-capture reverification failed (engine, frontend, or checkout changed "
            f"during the capture): {post_capture_reason}",
            file=sys.stderr,
        )
        return 1

    try:
        rows = _rows_from_capture_result(
            result,
            capture_id=capture_id,
            machine=machine,
            app_build_sha=app_build_sha,
            dwell_seconds=args.dwell_seconds,
            settle_seconds=args.settle_seconds,
            frontend_mode=frontend_mode,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    append_entries(args.ledger, rows)
    for row in rows:
        print(format_appended(row))
    for mode in ("gig", "library"):
        capture = result[mode]
        print(
            f"{mode}: median_footprint_mb={capture['median_footprint_mb']:.1f} "
            f"(chromium {capture['median_chromium_footprint_mb']:.1f}, "
            f"engine {capture['median_engine_footprint_mb']:.1f}, "
            f"engine_pids {sorted(set(capture['engine_pid_counts']))}) "
            f"median_rss_mb={capture['median_rss_mb']:.1f} "
            f"median_cpu_percent={capture['median_cpu_percent']:.2f} "
            f"samples={len(capture['footprint_samples_mb'])} "
            f"footprint_samples_mb={[round(v, 1) for v in capture['footprint_samples_mb']]} "
            f"cpu_samples_percent={[round(v, 2) for v in capture['cpu_samples_percent']]}"
        )
        by_type = capture.get("last_chromium_by_type_mb", {})
        print(f"{mode}: chromium_by_type_mb={ {k: round(v) for k, v in sorted(by_type.items())} }")
    print(f"deck_stable_ids={result.get('stable_ids')} frontend_mode={frontend_mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
