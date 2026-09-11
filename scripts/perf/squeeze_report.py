"""Acid-test squeeze report validation, writing, and cross-host comparison.

Stdlib-only module extracted from ``scripts/perf_squeeze.sh`` so Linux CI can
exercise the JSON contract without claiming WKWebView or CoreAudio KPIs.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_CAPTURE_KPIS = ("deck_load_ms", "xruns", "ui_latency_ms")
REQUIRED_IDENTITY_FIELDS = (
    "app_build_sha",
    "app_build_dirty",
    "frontend_build_sha",
    "frontend_build_dirty",
    "xrun_session_id",
)
# Real-library acid tests need thousands of tracks; the 2-track e2e bench and
# smaller farm fixtures must not qualify as library-scale numbers.
LIBRARY_SCALE_MIN_TRACKS = 1000
SCHEMA_VERSION = 2

REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "library_scale",
    "machine_tag",
    "hostname",
    "captures",
    "measured_app_build_sha",
    "capture_implementation",
)
PHASES = ("baseline", "during", "pressure-end", "after")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_XRUN_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _error(message: str) -> None:
    print(f"[ERROR] perf-squeeze: {message}", file=sys.stderr)
    raise SystemExit(1)


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _error(f"{label} did not emit valid JSON: {exc}")
    if not isinstance(payload, dict):
        _error(f"{label} must emit a JSON object")
    return payload


def validate_capture(payload: dict[str, Any], phase: str) -> dict[str, Any]:
    """Validate one capture phase payload and return the normalized object."""
    for name in ("deck_load_ms", "ui_latency_ms"):
        value = payload.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            _error(f"{phase} capture requires {name} as a finite number >= 0")
    xruns = payload.get("xruns")
    if isinstance(xruns, bool) or not isinstance(xruns, int) or xruns < 0:
        _error(f"{phase} capture requires xruns as an integer >= 0")
    build_sha = payload.get("app_build_sha")
    if not isinstance(build_sha, str) or _SHA_RE.fullmatch(build_sha) is None:
        _error(f"{phase} capture requires app_build_sha as a full Git SHA")
    if payload.get("app_build_dirty") is not False:
        _error(f"{phase} capture requires app_build_dirty must be false")
    frontend_sha = payload.get("frontend_build_sha")
    if not isinstance(frontend_sha, str) or _SHA_RE.fullmatch(frontend_sha) is None:
        _error(f"{phase} capture requires frontend_build_sha as a full Git SHA")
    if payload.get("frontend_build_dirty") is not False:
        _error(f"{phase} capture requires frontend_build_dirty must be false")
    xrun_session_id = payload.get("xrun_session_id")
    if (
        not isinstance(xrun_session_id, str)
        or _XRUN_SESSION_RE.fullmatch(xrun_session_id) is None
    ):
        _error(f"{phase} capture requires xrun_session_id as a stable identifier")
    if phase in ("baseline", "pressure-end") and payload.get("xrun_boundary_ack") is not True:
        _error(f"{phase} capture requires xrun_boundary_ack after flushing the worklet")
    return payload


def _read_track_count_from_json(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    track_count = payload.get("track_count")
    if isinstance(track_count, bool) or not isinstance(track_count, int):
        return None
    return track_count


def _read_track_count_from_state_db(db_path: Path) -> int | None:
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    count = row[0]
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _resolve_track_count(path: Path) -> int | None:
    if path.is_file() and path.suffix == ".json":
        return _read_track_count_from_json(path)
    if path.is_dir():
        manifest = path / "manifest.json"
        if manifest.is_file():
            return _read_track_count_from_json(manifest)
        state_db = path / "state" / "state.db"
        if state_db.is_file():
            return _read_track_count_from_state_db(state_db)
    return None


def resolve_library_scale(path: Path | None) -> dict[str, Any]:
    """Return a JSON-ready library-scale honesty object."""
    absent_reason = (
        "library-scale fixture is absent; the 2-track bench is not a library-scale number"
    )
    if path is None or not path.exists():
        return {"present": False, "track_count": None, "reason": absent_reason}

    track_count = _resolve_track_count(path)
    if track_count is None:
        return {
            "present": False,
            "track_count": None,
            "reason": "library-scale fixture track_count could not be read",
        }
    if track_count <= 2:
        return {
            "present": False,
            "track_count": track_count,
            "reason": (
                f"track_count={track_count}; the 2-track bench is not a library-scale number"
            ),
        }
    if track_count < LIBRARY_SCALE_MIN_TRACKS:
        return {
            "present": False,
            "track_count": track_count,
            "reason": (
                f"track_count={track_count} is below {LIBRARY_SCALE_MIN_TRACKS}; "
                "it is not a library-scale number"
            ),
        }
    return {"present": True, "track_count": track_count, "reason": None}


def write_report(
    *,
    baseline_path: Path,
    during_path: Path,
    pressure_end_path: Path,
    after_path: Path,
    output_path: Path,
    machine_tag: str,
    hostname: str,
    kind: str,
    level: str,
    duration_seconds: int,
    harness_source_sha: str,
    capture_implementation: str,
    library_scale_fixture: Path | None = None,
) -> dict[str, Any]:
    """Validate four captures and write one squeeze report JSON document."""
    captures = {
        name: validate_capture(_load_json_object(path, f"{name} capture"), name)
        for name, path in (
            ("baseline", baseline_path),
            ("during", during_path),
            ("pressure-end", pressure_end_path),
            ("after", after_path),
        )
    }
    app_build_shas = {capture["app_build_sha"] for capture in captures.values()}
    if len(app_build_shas) != 1:
        _error("app_build_sha changed between captures; report rejected")
    frontend_build_shas = {capture["frontend_build_sha"] for capture in captures.values()}
    if len(frontend_build_shas) != 1:
        _error("frontend_build_sha changed between captures; report rejected")
    xrun_session_ids = {capture["xrun_session_id"] for capture in captures.values()}
    if len(xrun_session_ids) != 1:
        _error("xrun_session_id changed between captures; report rejected")
    ordered_xruns = [captures[phase]["xruns"] for phase in PHASES]
    if any(later < earlier for earlier, later in zip(ordered_xruns, ordered_xruns[1:])):
        _error("xruns regressed between captures; report rejected")

    report = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "machine_tag": machine_tag,
        "hostname": hostname,
        "harness_source_sha": harness_source_sha,
        "harness_source_dirty": False,
        "capture_implementation": capture_implementation,
        "measured_app_build_sha": app_build_shas.pop(),
        "measured_app_build_dirty": False,
        "measured_frontend_build_sha": frontend_build_shas.pop(),
        "measured_frontend_build_dirty": False,
        "xrun_session_id": xrun_session_ids.pop(),
        "library_scale": resolve_library_scale(library_scale_fixture),
        "pressure": {
            "kind": kind,
            "level": level,
            "duration_seconds": int(duration_seconds),
            "simulated_notification_only": kind == "memory-notify",
        },
        "captures": captures,
        "during_minus_baseline": {
            "deck_load_ms": captures["during"]["deck_load_ms"] - captures["baseline"]["deck_load_ms"],
            "xruns": captures["pressure-end"]["xruns"] - captures["baseline"]["xruns"],
            "ui_latency_ms": captures["during"]["ui_latency_ms"]
            - captures["baseline"]["ui_latency_ms"],
        },
    }
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _validate_report_capture(report_label: str, phase: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        _error(f"{report_label} capture {phase} must be a JSON object")
    validate_capture(payload, f"{report_label} {phase}")


def compare_reports(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Reject two reports unless they share schema and required KPIs."""
    for label, report in (("report-a", a), ("report-b", b)):
        if not isinstance(report, dict):
            _error(f"{label} must be a JSON object")
        if report.get("schema_version") != SCHEMA_VERSION:
            _error(f"{label} schema_version must be {SCHEMA_VERSION}")
        for key in REQUIRED_TOP_LEVEL_KEYS:
            if key not in report:
                _error(f"{label} missing required field {key}")
        for phase in PHASES:
            captures = report.get("captures")
            if not isinstance(captures, dict) or phase not in captures:
                _error(f"{label} missing capture phase {phase}")
            capture = captures[phase]
            for name in REQUIRED_CAPTURE_KPIS + ("app_build_sha",):
                if name not in capture:
                    _error(f"{label} {phase} capture missing required field {name}")
            _validate_report_capture(label, phase, capture)

    if a.get("capture_implementation") != b.get("capture_implementation"):
        _error("capture_implementation differs between reports; comparison rejected")

    a_present = a.get("library_scale", {}).get("present")
    b_present = b.get("library_scale", {}).get("present")
    if a_present != b_present:
        _error(
            "library_scale.present differs between reports; comparison rejected "
            "(different measurement class)"
        )


def _cmd_validate(args: argparse.Namespace) -> None:
    payload = _load_json_object(args.input, f"{args.phase} capture")
    normalized = validate_capture(payload, args.phase)
    text = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _cmd_write(args: argparse.Namespace) -> None:
    write_report(
        baseline_path=args.baseline,
        during_path=args.during,
        pressure_end_path=args.pressure_end,
        after_path=args.after,
        output_path=args.output,
        machine_tag=args.machine_tag,
        hostname=args.hostname,
        kind=args.kind,
        level=args.level,
        duration_seconds=args.duration,
        harness_source_sha=args.harness_source_sha,
        capture_implementation=args.capture_implementation,
        library_scale_fixture=args.library_scale_fixture,
    )


def _cmd_compare(args: argparse.Namespace) -> None:
    report_a = json.loads(args.report_a.read_text(encoding="utf-8"))
    report_b = json.loads(args.report_b.read_text(encoding="utf-8"))
    compare_reports(report_a, report_b)


def _cmd_library_scale(args: argparse.Namespace) -> None:
    resolved = resolve_library_scale(args.fixture)
    if not resolved["present"]:
        print(f"[WARN] perf-squeeze: {resolved['reason']}", file=sys.stderr)
    sys.stdout.write(json.dumps(resolved, sort_keys=True) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.perf.squeeze_report")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--phase", required=True)
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--output", type=Path)

    write = subparsers.add_parser("write")
    write.add_argument("--baseline", type=Path, required=True)
    write.add_argument("--during", type=Path, required=True)
    write.add_argument("--pressure-end", type=Path, required=True)
    write.add_argument("--after", type=Path, required=True)
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--machine-tag", required=True)
    write.add_argument("--hostname", required=True)
    write.add_argument("--kind", required=True)
    write.add_argument("--level", required=True)
    write.add_argument("--duration", type=int, required=True)
    write.add_argument("--harness-source-sha", required=True)
    write.add_argument("--capture-implementation", required=True)
    write.add_argument("--library-scale-fixture", type=Path)

    compare = subparsers.add_parser("compare")
    compare.add_argument("report_a", type=Path)
    compare.add_argument("report_b", type=Path)

    library_scale = subparsers.add_parser("library-scale")
    library_scale.add_argument("--fixture", type=Path)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        _cmd_validate(args)
    elif args.command == "write":
        _cmd_write(args)
    elif args.command == "compare":
        _cmd_compare(args)
    elif args.command == "library-scale":
        _cmd_library_scale(args)
    else:
        parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
