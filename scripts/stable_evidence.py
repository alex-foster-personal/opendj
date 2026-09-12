"""OPS-18 stable-channel evidence: schema, writers, and the release gate.

A sha is stable only when this file is complete and green for THAT sha:
full suites, a red-team pass with zero open blocking bugs, and signing.
Nightly publishes without it. A fix for a blocking bug is a new sha.

Evidence JSON is machine-local (gitignored). Writers merge-append; the gate
judges green. No network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUITE_KEYS: tuple[str, ...] = ("fast_lane", "full_ci", "e2e", "macos_packaging")
TOP_LEVEL_KEYS: tuple[str, ...] = (
    "sha",
    "suites",
    "red_team",
    "signing",
    "written_by",
    "written_at_utc",
)
RED_TEAM_KEYS: tuple[str, ...] = (
    "pass_id",
    "agents",
    "sessions",
    "open_blocking_bug_count",
    "report_path",
)
SIGNING_KEYS: tuple[str, ...] = ("identity", "notarization_submission_id")
GREEN_CONCLUSION = "success"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ID_LINE_RE = re.compile(r"^\s*id:\s*([0-9a-fA-F-]{36})\s*$", re.MULTILINE)
_WORKFLOW_TO_SUITE: dict[str, str] = {
    "CI": "fast_lane",
    "Full CI (on-demand)": "full_ci",
    "E2E": "e2e",
    "macOS Packaging": "macos_packaging",
}


class StableEvidenceError(ValueError):
    """Schema or gate failure with an operator-readable message."""


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_evidence_dir() -> Path:
    env = os.environ.get("OPENDJ_STABLE_EVIDENCE_DIR", "").strip()
    if env:
        return Path(env)
    return Path("ops/stable")


def require_sha(sha: str) -> str:
    candidate = sha.strip().lower()
    if _SHA_RE.fullmatch(candidate) is None:
        raise StableEvidenceError(f"sha {sha!r} is not a full 40-character lowercase hex git sha")
    return candidate


def evidence_path(evidence_dir: Path, sha: str) -> Path:
    return evidence_dir / f"{require_sha(sha)}.json"


def load_evidence(path: Path) -> dict[str, Any]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StableEvidenceError(f"evidence file {path} is missing") from exc
    except json.JSONDecodeError as exc:
        raise StableEvidenceError(f"evidence file {path} is not valid JSON") from exc
    if not isinstance(body, dict):
        raise StableEvidenceError(f"evidence file {path} is not a JSON object")
    return body


def write_evidence(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(_nonempty_str(item) for item in value)


def validate_evidence(body: dict[str, Any], *, expected_sha: str | None = None) -> None:
    sha = body.get("sha")
    if not isinstance(sha, str) or _SHA_RE.fullmatch(sha.lower()) is None:
        raise StableEvidenceError("sha must be a full 40-character hex git object name")
    if expected_sha is not None and sha.lower() != expected_sha.lower():
        raise StableEvidenceError(f"evidence sha is {sha}, expected {expected_sha}")
    missing = gate_missing_items(body, expected_sha=expected_sha or sha)
    if missing:
        raise StableEvidenceError("stable evidence is incomplete: " + ", ".join(missing))


def _missing_sha(body: dict[str, Any], expected_sha: str) -> list[str]:
    if "sha" not in body:
        return ["sha"]
    sha = body.get("sha")
    if not isinstance(sha, str) or _SHA_RE.fullmatch(sha.lower()) is None:
        return ["sha is not a full 40-character hex git object name"]
    if sha.lower() != expected_sha.lower():
        return [f"sha is {sha} (expected {expected_sha})"]
    return []


def _missing_writers(body: dict[str, Any]) -> list[str]:
    missing = [key for key in ("written_by", "written_at_utc") if key not in body]
    if "written_by" in body and not _nonempty_str(body.get("written_by")):
        missing.append("written_by")
    if "written_at_utc" in body and not _nonempty_str(body.get("written_at_utc")):
        missing.append("written_at_utc")
    return missing


def _missing_suites(body: dict[str, Any]) -> list[str]:
    if "suites" not in body:
        return ["suites"]
    suites = body.get("suites")
    if not isinstance(suites, dict):
        return ["suites"]
    missing: list[str] = []
    for suite in SUITE_KEYS:
        record = suites.get(suite)
        if suite not in suites or not isinstance(record, dict):
            missing.append(f"suites.{suite}")
            continue
        if not _nonempty_str(record.get("run_id")):
            missing.append(f"suites.{suite}.run_id")
        conclusion = record.get("conclusion")
        if conclusion != GREEN_CONCLUSION:
            missing.append(
                f"suites.{suite} conclusion is {conclusion!r} (must be {GREEN_CONCLUSION})"
            )
    return missing


def _missing_red_team(body: dict[str, Any]) -> list[str]:
    if "red_team" not in body:
        return ["red_team"]
    red = body.get("red_team")
    if not isinstance(red, dict):
        return ["red_team"]
    missing = [f"red_team.{key}" for key in RED_TEAM_KEYS if key not in red]
    count = red.get("open_blocking_bug_count")
    if "open_blocking_bug_count" in red:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            missing.append("red_team.open_blocking_bug_count")
        elif count != 0:
            missing.append(
                f"red_team.open_blocking_bug_count is {count} (must be 0); "
                "a fix needs a new sha and a new pass"
            )
    if "pass_id" in red and not _nonempty_str(red.get("pass_id")):
        missing.append("red_team.pass_id")
    if "report_path" in red and not _nonempty_str(red.get("report_path")):
        missing.append("red_team.report_path")
    if "agents" in red and not _str_list(red.get("agents")):
        missing.append("red_team.agents")
    if "sessions" in red and not _str_list(red.get("sessions")):
        missing.append("red_team.sessions")
    return missing


def _missing_signing(body: dict[str, Any]) -> list[str]:
    if "signing" not in body:
        return ["signing"]
    signing = body.get("signing")
    if not isinstance(signing, dict):
        return ["signing"]
    return [
        f"signing.{key}" if key not in signing else key
        for key in SIGNING_KEYS
        if key not in signing or not _nonempty_str(signing.get(key))
    ]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def gate_missing_items(body: dict[str, Any], *, expected_sha: str) -> list[str]:
    missing = (
        _missing_sha(body, expected_sha)
        + _missing_writers(body)
        + _missing_suites(body)
        + _missing_red_team(body)
        + _missing_signing(body)
    )
    return _dedupe(missing)


def gate_stable(evidence_dir: Path, sha: str) -> list[str]:
    sha = require_sha(sha)
    path = evidence_path(evidence_dir, sha)
    if not path.is_file():
        return [f"evidence file {path}"]
    try:
        body = load_evidence(path)
    except StableEvidenceError as exc:
        return [str(exc)]
    return gate_missing_items(body, expected_sha=sha)


def _load_or_create(path: Path, sha: str) -> dict[str, Any]:
    body = load_evidence(path) if path.is_file() else {}
    body["sha"] = sha
    if not isinstance(body.get("suites"), dict):
        body["suites"] = {}
    return body


def _stamp_writer(body: dict[str, Any], written_by: str) -> None:
    body["written_by"] = written_by
    body["written_at_utc"] = now_utc()


def append_suite(
    evidence_dir: Path,
    sha: str,
    *,
    suite: str,
    run_id: str,
    conclusion: str,
    written_by: str,
) -> Path:
    sha = require_sha(sha)
    if suite not in SUITE_KEYS:
        raise StableEvidenceError(
            f"unknown suite {suite!r}; expected one of {', '.join(SUITE_KEYS)}"
        )
    if not _nonempty_str(run_id) or not _nonempty_str(conclusion) or not _nonempty_str(written_by):
        raise StableEvidenceError("suite writer needs run_id, conclusion, and written_by")
    path = evidence_path(evidence_dir, sha)
    body = _load_or_create(path, sha)
    suites = body["suites"]
    assert isinstance(suites, dict)
    suites[suite] = {"run_id": str(run_id), "conclusion": str(conclusion)}
    _stamp_writer(body, written_by)
    write_evidence(path, body)
    return path


def append_red_team(
    evidence_dir: Path,
    sha: str,
    *,
    pass_id: str,
    agents: list[str],
    sessions: list[str],
    open_blocking_bug_count: int,
    report_path: str,
    written_by: str,
) -> Path:
    sha = require_sha(sha)
    if not isinstance(open_blocking_bug_count, int) or isinstance(open_blocking_bug_count, bool):
        raise StableEvidenceError("open_blocking_bug_count must be an integer")
    if open_blocking_bug_count < 0:
        raise StableEvidenceError("open_blocking_bug_count cannot be negative")
    if not (_nonempty_str(pass_id) and _nonempty_str(report_path) and _nonempty_str(written_by)):
        raise StableEvidenceError("red-team writer needs pass_id, report_path, and written_by")
    if not _str_list(agents) or not _str_list(sessions):
        raise StableEvidenceError("red-team writer needs non-empty agents and sessions lists")
    path = evidence_path(evidence_dir, sha)
    body = _load_or_create(path, sha)
    body["red_team"] = {
        "pass_id": pass_id,
        "agents": list(agents),
        "sessions": list(sessions),
        "open_blocking_bug_count": open_blocking_bug_count,
        "report_path": report_path,
    }
    _stamp_writer(body, written_by)
    write_evidence(path, body)
    return path


def append_signing(
    evidence_dir: Path,
    sha: str,
    *,
    identity: str,
    notarization_submission_id: str,
    written_by: str,
) -> Path:
    sha = require_sha(sha)
    if (
        not _nonempty_str(identity)
        or not _nonempty_str(notarization_submission_id)
        or not _nonempty_str(written_by)
    ):
        raise StableEvidenceError(
            "signing writer needs identity, notarization_submission_id, and written_by"
        )
    path = evidence_path(evidence_dir, sha)
    body = _load_or_create(path, sha)
    body["signing"] = {
        "identity": identity,
        "notarization_submission_id": notarization_submission_id,
    }
    _stamp_writer(body, written_by)
    write_evidence(path, body)
    return path


def parse_notary_submission_id(log: str) -> str:
    matches = _ID_LINE_RE.findall(log)
    if not matches:
        raise StableEvidenceError("notarytool output has no id: line to parse")
    return matches[-1]


def stamp_payload_identity(
    manifest: Path,
    *,
    channel: str,
    evidence_written_at_utc: str,
) -> None:
    body = json.loads(manifest.read_text(encoding="utf-8"))
    identity = body.get("identity")
    if not isinstance(identity, dict):
        identity = {}
        body["identity"] = identity
    identity["release_channel"] = channel if isinstance(channel, str) else ""
    identity["evidence_written_at_utc"] = (
        evidence_written_at_utc if isinstance(evidence_written_at_utc, str) else ""
    )
    manifest.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def suite_for_workflow(name: str) -> str:
    suite = _WORKFLOW_TO_SUITE.get(name)
    if suite is None:
        raise StableEvidenceError(
            f"unknown workflow {name!r}; expected one of " + ", ".join(_WORKFLOW_TO_SUITE)
        )
    return suite


def _add_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help="directory of per-sha JSON files (default: OPENDJ_STABLE_EVIDENCE_DIR or ops/stable)",
    )


def _dir_from(args: argparse.Namespace) -> Path:
    if args.evidence_dir:
        return Path(args.evidence_dir)
    return default_evidence_dir()


def _cmd_gate(args: argparse.Namespace) -> int:
    sha = args.sha
    evidence_dir = _dir_from(args)
    try:
        missing = gate_stable(evidence_dir, sha)
    except StableEvidenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if missing:
        print(
            f"[ERROR] sha {sha} cannot be marked stable; missing: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    print("[OK] stable evidence is complete", file=sys.stderr)
    return 0


def _cmd_append_suite(args: argparse.Namespace) -> int:
    suite = args.suite or suite_for_workflow(args.workflow_name)
    try:
        path = append_suite(
            _dir_from(args),
            args.sha,
            suite=suite,
            run_id=str(args.run_id),
            conclusion=args.conclusion,
            written_by=args.written_by,
        )
    except StableEvidenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] wrote suite {suite} into {path}", file=sys.stderr)
    return 0


def _cmd_append_red_team(args: argparse.Namespace) -> int:
    try:
        path = append_red_team(
            _dir_from(args),
            args.sha,
            pass_id=args.pass_id,
            agents=list(args.agents),
            sessions=list(args.sessions),
            open_blocking_bug_count=args.open_blocking_bug_count,
            report_path=args.report_path,
            written_by=args.written_by,
        )
    except StableEvidenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] wrote red-team pass into {path}", file=sys.stderr)
    return 0


def _cmd_append_signing(args: argparse.Namespace) -> int:
    try:
        path = append_signing(
            _dir_from(args),
            args.sha,
            identity=args.identity,
            notarization_submission_id=args.notarization_submission_id,
            written_by=args.written_by,
        )
    except StableEvidenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] wrote signing into {path}", file=sys.stderr)
    return 0


def _cmd_stamp_payload(args: argparse.Namespace) -> int:
    try:
        stamp_payload_identity(
            Path(args.manifest),
            channel=args.channel,
            evidence_written_at_utc=args.evidence_at,
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_parse_notary_id(_args: argparse.Namespace) -> int:
    try:
        print(parse_notary_submission_id(sys.stdin.read()))
    except StableEvidenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_written_at(args: argparse.Namespace) -> int:
    try:
        path = evidence_path(_dir_from(args), args.sha)
        body = load_evidence(path)
    except StableEvidenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    stamp = body.get("written_at_utc")
    if not _nonempty_str(stamp):
        print("[ERROR] evidence has no written_at_utc", file=sys.stderr)
        return 1
    print(stamp)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.stable_evidence",
        description="OPS-18 stable-channel evidence writers and gate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gate = sub.add_parser("gate", help="refuse unless every evidence key is green")
    gate.add_argument("--sha", required=True)
    _add_dir(gate)
    gate.set_defaults(func=_cmd_gate)

    suite = sub.add_parser("append-suite", help="merge one CI suite result for a sha")
    suite.add_argument("--sha", required=True)
    suite.add_argument("--suite", choices=SUITE_KEYS, default=None)
    suite.add_argument("--workflow-name", default=None)
    suite.add_argument("--run-id", required=True)
    suite.add_argument("--conclusion", required=True)
    suite.add_argument("--written-by", default="github-actions")
    _add_dir(suite)
    suite.set_defaults(func=_cmd_append_suite)

    red = sub.add_parser("append-red-team", help="merge a red-team pass for a sha")
    red.add_argument("--sha", required=True)
    red.add_argument("--pass-id", required=True)
    red.add_argument("--agents", nargs="+", required=True)
    red.add_argument("--sessions", nargs="+", required=True)
    red.add_argument("--open-blocking-bug-count", type=int, required=True)
    red.add_argument("--report-path", required=True)
    red.add_argument("--written-by", default="red-team")
    _add_dir(red)
    red.set_defaults(func=_cmd_append_red_team)

    signing = sub.add_parser("append-signing", help="merge signing facts after just dmg")
    signing.add_argument("--sha", required=True)
    signing.add_argument("--identity", required=True)
    signing.add_argument("--notarization-submission-id", required=True)
    signing.add_argument("--written-by", default="just dmg")
    _add_dir(signing)
    signing.set_defaults(func=_cmd_append_signing)

    stamp = sub.add_parser("stamp-payload", help="write channel onto payload identity")
    stamp.add_argument("--manifest", required=True)
    stamp.add_argument("--channel", default="")
    stamp.add_argument("--evidence-at", default="")
    stamp.set_defaults(func=_cmd_stamp_payload)

    parse_id = sub.add_parser("parse-notary-id", help="read the last id: line from stdin")
    parse_id.set_defaults(func=_cmd_parse_notary_id)

    written = sub.add_parser("written-at", help="print written_at_utc for a sha")
    written.add_argument("--sha", required=True)
    _add_dir(written)
    written.set_defaults(func=_cmd_written_at)

    args = parser.parse_args(argv)
    if args.command == "append-suite" and not args.suite and not args.workflow_name:
        print("[ERROR] append-suite needs --suite or --workflow-name", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
