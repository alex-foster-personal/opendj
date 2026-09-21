"""Fleet state-file duplicate-writer check (DEVOPS-10, issue #1580).

Enumerates the committed inventory of fleet state files (path, writer,
readers) and fails on more than one writer, a reader of a path no writer
produces, or near-duplicate names (dash vs underscore, old vs new prefix).
Tombstones are retired names: they must have no readers, and a live path
that replaces them carries a ``Supersedes:`` line.

    python -m scripts.duplicate_writer_check
    python -m scripts.duplicate_writer_check --manifest ops/fleet/state-files.yaml
    python -m scripts.duplicate_writer_check --json

Exit codes: 0 clean, 1 findings, 2 UNKNOWN (never pass on an unreadable
manifest).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:
    if exc.name == "yaml":
        raise SystemExit("UNKNOWN: PyYAML is required to read the state-file inventory") from None
    raise

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "ops" / "fleet" / "state-files.yaml"
DEFAULT_SCAN_DIR = REPO_ROOT / "ops" / "fleet"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNKNOWN = 2

_STATE_REF = re.compile(r"(?:\$JOBS/state/|\$STATE_DIR/|jobs/state/)([A-Za-z0-9_.<${}-]+)")
_VAR_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$\{?_na\}?"), "<account>"),
    (re.compile(r"\$\{?_a\}?"), "<account>"),
    (re.compile(r"\$\{?acct\}?"), "<account>"),
    (re.compile(r"\$1"), "<account>"),
)
_ACCT_PREFIX = re.compile(r"acct(?=\d)")
_FIVE_H = re.compile(r"(?<![a-z])5h(?![a-z])")


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True)
class Report:
    findings: tuple[Finding, ...]
    files: int
    exit_code: int


def normalize_name(path: str) -> str:
    """Fold dash/underscore and known prefix abbreviations to one key."""
    name = Path(path).name.lower().replace("_", "-")
    name = _ACCT_PREFIX.sub("account", name)
    name = _FIVE_H.sub("five-hour", name)
    return re.sub(r"-+", "-", name)


def near_duplicate_kind(left: str, right: str) -> str | None:
    if left == right:
        return None
    if normalize_name(left) != normalize_name(right):
        return None
    folded_left = Path(left).name.lower().replace("_", "-")
    folded_right = Path(right).name.lower().replace("_", "-")
    if folded_left == folded_right:
        return "dash vs underscore"
    return "old vs new prefix"


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [] if value in {"none", ""} else [value]
    if isinstance(value, list):
        return [str(item) for item in value if item and item != "none"]
    return [str(value)]


def _is_tombstone(entry: dict) -> bool:
    return str(entry.get("status", "live")).lower() == "tombstone"


def canonicalize_extracted(token: str) -> str:
    cleaned = token.strip().strip("\"'")
    # `_STATE_REF` admits `{` and `}` so `state/sink-${x}.json` keeps its variable, but
    # the same class swallows the brace that CLOSES an enclosing expansion:
    # `${SINK_TRIAGE_KPI:-$JOBS/state/sink-triage-kpi.json}` yielded the name
    # `sink-triage-kpi.json}`, an undeclared file that exists nowhere, and the check
    # went red on trunk (ops/fleet/sink-triage.sh:30, Wed 16 Sep 2026). A brace with no
    # opener inside the token belongs to the caller's expansion, never to the name.
    while cleaned.endswith("}") and cleaned.count("}") > cleaned.count("{"):
        cleaned = cleaned[:-1]
    for pattern, repl in _VAR_SUBS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


def scan_state_paths(scan_dir: Path) -> set[str]:
    """State-file names ops/fleet scripts actually reference (not comments)."""
    found: set[str] = set()
    if not scan_dir.is_dir():
        return found
    for path in sorted(scan_dir.iterdir()):
        if path.suffix not in {".sh", ".py"} or not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("#"):
                continue
            for match in _STATE_REF.findall(raw_line):
                name = canonicalize_extracted(match)
                if name:
                    found.add(name)
    return found


def _partition(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    live: list[dict] = []
    tombs: list[dict] = []
    for entry in entries:
        (tombs if _is_tombstone(entry) else live).append(entry)
    return live, tombs


def _multi_writer_findings(live: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    writers_by_path: dict[str, list[str]] = {}
    for entry in live:
        path = str(entry.get("path", "")).strip()
        if not path:
            findings.append(Finding("invalid", "", "live entry is missing path"))
            continue
        writers_by_path.setdefault(path, [])
        writers_by_path[path].extend(_as_list(entry.get("writer")))
    for path, writers in writers_by_path.items():
        unique = list(dict.fromkeys(writers))
        if len(unique) > 1:
            findings.append(Finding("multi_writer", path, "writers=" + ",".join(unique)))
    return findings


def _orphan_reader_findings(live_by_path: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for path, entry in live_by_path.items():
        writers = list(dict.fromkeys(_as_list(entry.get("writer"))))
        readers = _as_list(entry.get("readers"))
        if readers and not writers:
            findings.append(
                Finding(
                    "orphan_reader",
                    path,
                    "readers=" + ",".join(readers) + " (no writer produces this path)",
                )
            )
    return findings


def _tombstone_findings(
    tomb_by_path: dict[str, dict], live_by_path: dict[str, dict]
) -> list[Finding]:
    findings: list[Finding] = []
    for path, entry in tomb_by_path.items():
        readers = _as_list(entry.get("readers"))
        if readers:
            findings.append(
                Finding(
                    "tombstone_read",
                    path,
                    "tombstone has readers="
                    + ",".join(readers)
                    + "; a tombstone must fail loud, not be read",
                )
            )
        if path in live_by_path:
            findings.append(
                Finding(
                    "tombstone_live",
                    path,
                    "tombstone is also listed as live; add-beside of a retired name",
                )
            )
    return findings


def _near_duplicate_findings(live_paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for index, left in enumerate(live_paths):
        for right in live_paths[index + 1 :]:
            kind = near_duplicate_kind(left, right)
            if kind is None:
                continue
            findings.append(Finding("near_duplicate", f"{left} ~ {right}", kind))
    return findings


def _undeclared_findings(declared: set[str], scan_dir: Path) -> list[Finding]:
    return [
        Finding(
            "undeclared",
            name,
            f"ops/fleet references {name} but inventory does not declare it",
        )
        for name in sorted(scan_state_paths(scan_dir))
        if name not in declared
    ]


def evaluate(payload: dict, *, scan_dir: Path | None) -> Report:
    entries = list(payload.get("files") or [])
    live, tombs = _partition(entries)
    live_by_path = {str(entry.get("path", "")).strip(): entry for entry in live}
    tomb_by_path = {str(entry.get("path", "")).strip(): entry for entry in tombs}
    findings = (
        _multi_writer_findings(live)
        + _orphan_reader_findings(live_by_path)
        + _tombstone_findings(tomb_by_path, live_by_path)
        + _near_duplicate_findings([path for path in live_by_path if path])
    )
    if scan_dir is not None:
        findings.extend(_undeclared_findings(set(live_by_path) | set(tomb_by_path), scan_dir))
    exit_code = EXIT_FINDINGS if findings else EXIT_OK
    return Report(tuple(findings), files=len(entries), exit_code=exit_code)


def render(report: Report) -> str:
    if report.exit_code == EXIT_OK:
        return f"[duplicate-writer] PASS files={report.files} findings=0"
    lines = [f"[duplicate-writer] FAIL files={report.files} findings={len(report.findings)}"]
    for item in report.findings:
        extra = f" ({item.detail})" if item.detail else ""
        lines.append(f"  {item.kind}: {item.path}{extra}")
    return "\n".join(lines)


def load_manifest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise TypeError(f"manifest {path} is not a mapping")
    return payload


def run_check(
    manifest: Path,
    *,
    scan_dir: Path | None,
    as_json: bool = False,
) -> tuple[int, str]:
    if not manifest.is_file():
        return EXIT_UNKNOWN, f"[duplicate-writer] UNKNOWN: missing manifest {manifest}"
    try:
        payload = load_manifest(manifest)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        return EXIT_UNKNOWN, f"[duplicate-writer] UNKNOWN: cannot read {manifest}: {exc}"
    report = evaluate(payload, scan_dir=scan_dir)
    if as_json:
        body = {
            "verdict": "OK" if report.exit_code == EXIT_OK else "FINDINGS",
            "files": report.files,
            "findings": [asdict(item) for item in report.findings],
        }
        return report.exit_code, json.dumps(body, indent=2, sort_keys=True)
    return report.exit_code, render(report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="committed state-file inventory (default: ops/fleet/state-files.yaml)",
    )
    parser.add_argument(
        "--scan-dir",
        type=Path,
        default=DEFAULT_SCAN_DIR,
        help="ops/fleet directory to enumerate for undeclared state paths",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="validate the manifest only; do not scan ops/fleet sources",
    )
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    scan_dir: Path | None = None if args.no_scan else args.scan_dir
    code, text = run_check(args.manifest, scan_dir=scan_dir, as_json=args.as_json)
    stream = sys.stdout if code == EXIT_OK else sys.stderr
    if args.as_json:
        stream = sys.stdout
    print(text, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
