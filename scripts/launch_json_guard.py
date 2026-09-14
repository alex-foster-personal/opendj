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
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LAUNCH_JSON = REPO_ROOT / ".claude" / "launch.json"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNKNOWN = 2

_SUBSTITUTION = re.compile(r"\$\{(?P<kind>[^:}]+)(?::(?P<name>[^}]+))?\}")
_DECIMAL_INTEGER = re.compile(r"^[0-9]+$")
_SCRIPT_EXTENSIONS = (".py", ".js", ".mjs", ".ts", ".sh")
_MODULE_FLAGS = frozenset({"-m", "--module"})
_PORT_FLAGS = frozenset({"--port", "-p"})
_PORT_FLAG_EQUALS = re.compile(r"^(?:--port|-p)=([0-9]+)$")
_LOCALHOST_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


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


def _script_entrypoint_findings(
    runtime_args: list[str],
    *,
    label: str,
    repo_root: Path,
) -> list[str]:
    """Check every script-like argument exists, not just the interpreter.

    A ``-m``/``--module`` flag's following value is a module name, not a
    file path, and is skipped. Anything else ending in a recognized script
    extension is treated as an entrypoint the interpreter will try to open.
    """
    findings: list[str] = []
    skip_next = False
    for arg in runtime_args:
        if skip_next:
            skip_next = False
            continue
        if arg in _MODULE_FLAGS:
            skip_next = True
            continue
        if arg.endswith(_SCRIPT_EXTENSIONS):
            resolved = _substitute(arg, repo_root=repo_root)
            if not Path(resolved).is_file():
                findings.append(
                    f"{label}: script entrypoint {arg!r} does not exist at {resolved!r}"
                )
    return findings


def _localhost_url_findings(raw_url: str, *, label: str) -> tuple[list[str], int | None]:
    """Validate a ``url`` is an origin-only localhost URL. Returns (findings, port)."""
    parsed = urlparse(raw_url)
    findings: list[str] = []
    if parsed.scheme not in ("http", "https"):
        findings.append(f"{label}: url {raw_url!r} must use http or https")
    if parsed.hostname not in _LOCALHOST_HOSTNAMES:
        findings.append(f"{label}: url {raw_url!r} must point at localhost, not a remote host")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        findings.append(
            f"{label}: url {raw_url!r} must be just the server origin, no path or query"
        )
    if parsed.port is None:
        findings.append(f"{label}: url {raw_url!r} must carry an explicit port")
    return findings, parsed.port


def _port_values_in_args(runtime_args: list[str]) -> list[str]:
    """Return every raw port-value string in ``runtime_args``.

    Covers a bare positional port ("9434"), a "--port"/"-p" flag whose
    value is the next argument, and a "--port=9434"/"-p=9434" single token.
    """
    values: list[str] = []
    take_next_as_port = False
    for arg in runtime_args:
        if take_next_as_port:
            values.append(arg)
            take_next_as_port = False
            continue
        if arg in _PORT_FLAGS:
            take_next_as_port = True
            continue
        equals_match = _PORT_FLAG_EQUALS.match(arg)
        if equals_match:
            values.append(equals_match.group(1))
        elif _DECIMAL_INTEGER.match(arg):
            values.append(arg)
    return values


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


def _validate_command_entry(
    entry: dict,
    runtime_executable: str,
    *,
    label: str,
    repo_root: Path,
) -> tuple[list[str], int | None]:
    """Validate an entry that starts its own server process."""
    findings: list[str] = []
    if not _executable_is_resolvable(runtime_executable, repo_root=repo_root):
        findings.append(
            f"{label}: runtimeExecutable {runtime_executable!r} does not exist and is not on PATH"
        )

    runtime_args = entry.get("runtimeArgs", [])
    if not isinstance(runtime_args, list) or not all(isinstance(arg, str) for arg in runtime_args):
        findings.append(f'{label}: "runtimeArgs" must be a list of strings')
        runtime_args = []
    findings.extend(_script_entrypoint_findings(runtime_args, label=label, repo_root=repo_root))

    port = entry.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        findings.append(f'{label}: missing an integer "port"')
        port = None
    else:
        findings.extend(
            f'{label}: runtimeArgs port {value} does not match "port" field {port}'
            for value in _port_values_in_args(runtime_args)
            if int(value) != port
        )

    raw_url = entry.get("url")
    if isinstance(raw_url, str) and raw_url:
        url_findings, url_port = _localhost_url_findings(raw_url, label=label)
        findings.extend(url_findings)
        if port is not None and url_port is not None and url_port != port:
            findings.append(f'{label}: url port {url_port} does not match "port" field {port}')

    return findings, port


def _validate_attach_only_entry(raw_url: str, *, label: str) -> tuple[list[str], int | None]:
    """Validate an entry that attaches to a server owned by something else
    (a launchd job, a service manager) instead of starting one itself."""
    findings, port = _localhost_url_findings(raw_url, label=label)
    return findings, port


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
    raw_url = entry.get("url")
    if isinstance(runtime_executable, str) and runtime_executable:
        entry_findings, port = _validate_command_entry(
            entry, runtime_executable, label=label, repo_root=repo_root
        )
    elif isinstance(raw_url, str) and raw_url:
        entry_findings, port = _validate_attach_only_entry(raw_url, label=label)
    else:
        entry_findings = [f'{label}: needs a non-empty "runtimeExecutable" or an attach-only "url"']
        port = None

    findings.extend(entry_findings)
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
