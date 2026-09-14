"""``.claude/launch.json`` hygiene guard (OPS-31, issue #2558).

Verifies the presence of the good thing (a launch.json that will actually
start the server it names, on the port it claims) rather than the absence
of one known-bad pattern. Every check here is a positive assertion about
the parsed configuration, not a search for a specific past incident.

    python -m scripts.launch_json_guard
    python -m scripts.launch_json_guard --json

Exit codes: 0 clean, 1 findings, 2 UNKNOWN (unreadable or malformed file).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LAUNCH_JSON = REPO_ROOT / ".claude" / "launch.json"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNKNOWN = 2

_SUBSTITUTION = re.compile(r"\$\{(?P<kind>[^:}]+)(?::(?P<name>[^}]+))?\}")
_DECIMAL_INTEGER = re.compile(r"^[0-9]+$")


class LaunchJsonGuardError(ValueError):
    """The launch.json config is missing, malformed, or unreadable."""


def _substitute(raw_value: str, *, repo_root: Path) -> str:
    def _replace(match: re.Match[str]) -> str:
        kind, name = match.group("kind"), match.group("name")
        if kind == "workspaceFolder":
            return str(repo_root)
        if kind == "env" and name is not None:
            return os.environ.get(name, "")
        raise LaunchJsonGuardError(f"unsupported substitution token {match.group(0)!r}")

    return _SUBSTITUTION.sub(_replace, raw_value)


def _executable_is_resolvable(raw_executable: str, *, repo_root: Path) -> bool:
    resolved = _substitute(raw_executable, repo_root=repo_root)
    if "/" in resolved:
        candidate = Path(resolved)
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(resolved) is not None


def load_config(launch_json_path: Path) -> dict:
    """Parse ``launch_json_path``. Raises LaunchJsonGuardError, never a verdict."""
    try:
        raw_text = launch_json_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LaunchJsonGuardError(f"cannot read {launch_json_path}: {exc}") from exc
    try:
        config = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise LaunchJsonGuardError(f"{launch_json_path} is not valid JSON: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("configurations"), list):
        raise LaunchJsonGuardError(f"{launch_json_path} has no configurations array")
    return config


def _validate_entry(
    entry: dict,
    *,
    label: str,
    repo_root: Path,
) -> tuple[list[str], int | None, str]:
    """Validate one configuration entry. Returns (findings, port, owner_name)."""
    findings: list[str] = []
    name = entry.get("name")
    if isinstance(name, str) and name:
        label = f"configuration {name!r}"
    else:
        findings.append(f'{label}: missing a non-empty "name"')

    runtime_executable = entry.get("runtimeExecutable")
    if not isinstance(runtime_executable, str) or not runtime_executable:
        findings.append(f'{label}: missing a non-empty "runtimeExecutable"')
    elif not _executable_is_resolvable(runtime_executable, repo_root=repo_root):
        findings.append(
            f"{label}: runtimeExecutable {runtime_executable!r} does not exist and is not on PATH"
        )

    runtime_args = entry.get("runtimeArgs", [])
    if not isinstance(runtime_args, list) or not all(isinstance(arg, str) for arg in runtime_args):
        findings.append(f'{label}: "runtimeArgs" must be a list of strings')
        runtime_args = []

    port = entry.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        findings.append(f'{label}: missing an integer "port"')
        return findings, None, label

    findings.extend(
        f'{label}: runtimeArgs port {arg} does not match "port" field {port}'
        for arg in runtime_args
        if _DECIMAL_INTEGER.match(arg) and int(arg) != port
    )
    return findings, port, label


def validate(config: dict, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Return every finding against the parsed config. Empty means clean."""
    configurations = config["configurations"]
    if not configurations:
        return ["configurations array is empty"]

    findings: list[str] = []
    port_owners: dict[int, list[str]] = {}
    for index, entry in enumerate(configurations):
        label = f"configurations[{index}]"
        if not isinstance(entry, dict):
            findings.append(f"{label} is not an object")
            continue
        entry_findings, port, owner_label = _validate_entry(entry, label=label, repo_root=repo_root)
        findings.extend(entry_findings)
        if port is not None:
            port_owners.setdefault(port, []).append(owner_label)

    findings.extend(
        f"port {port} is claimed by more than one configuration: {owners}"
        for port, owners in port_owners.items()
        if len(owners) > 1
    )
    return findings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.launch_json_guard",
        description="verify .claude/launch.json entries are internally consistent",
    )
    parser.add_argument("--launch-json", type=Path, default=DEFAULT_LAUNCH_JSON)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = load_config(args.launch_json)
        findings = validate(config, repo_root=REPO_ROOT)
    except LaunchJsonGuardError as exc:
        if args.json:
            print(json.dumps({"status": "UNKNOWN", "error": str(exc)}))
        else:
            print(f"[UNKNOWN] {exc}", file=sys.stderr)
        return EXIT_UNKNOWN

    if args.json:
        print(json.dumps({"status": "OK" if not findings else "FINDINGS", "findings": findings}))
    elif findings:
        for finding in findings:
            print(f"[FAIL] {finding}", file=sys.stderr)
    else:
        print(f"[OK] {args.launch_json} is internally consistent")
    return EXIT_OK if not findings else EXIT_FINDINGS


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LaunchJsonGuardError", "load_config", "validate"]
