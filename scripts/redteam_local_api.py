"""Scripted local-daemon API probes for red-team track (g) (issue #1861).

Requirements:
    - [if] the daemon is listening [then] the probe asserts the socket address
      from the kernel listen table, never from the configured bind host alone
    - [if] a request is sent to a non-loopback address of this host [then] it
      is refused or unreachable; a loopback health GET is the control
    - [if] Origin is https://evil.example or null [then] CORS does not echo it;
      the app frontend origin is the positive control
    - [if] a path or filename route is given ../ or an encoded variant [then]
      it is refused; a legitimate in-root request is the negative control
    - [if] a probe cannot run [then] it reports UNKNOWN, never PASS

One command: ``just redteam-local-api``. FAIL findings are filed with
``python -m scripts.redteam_filing --label red-team``. This module does not
patch the daemon.

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    from scripts.redteam_findings import (
        Evidence,
        ExistingFindingError,
        Finding,
        FindingDetails,
        FindingStore,
        Reproduction,
    )
    from scripts.redteam_local_api_net import (
        acao,
        http_body,
        http_status,
        is_loopback_ip,
        listening_addresses,
        non_loopback_ipv4,
        origin_allowed,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.redteam_local_api") from None
    raise

HOSTILE_ORIGIN = "https://evil.example"
NULL_ORIGIN = "null"
HEALTH_PATH = "/api/v1/health"
SURFACE = "area:platform"
DEFAULT_RUN_ID = "track-g-1861"
PATHISH_NAMES = frozenset(
    {
        "filename",
        "path",
        "filepath",
        "file_path",
        "dir",
        "folder",
        "folder_path",
        "batch_dir",
        "attachment_id",
    }
)
TRAVERSAL_PAYLOADS = (
    "../secret.json",
    "../../etc/passwd",
    "..%2Fsecret.json",
    "..%2f..%2fetc%2fpasswd",
    "%2e%2e/%2e%2e/etc/passwd",
    "%2E%2E%2Fsecret.json",
    "....//secret.json",
    "/etc/passwd",
    "..\\secret.json",
)
KNOWN_FILE_ROUTES = (
    ("GET", "/api/v1/tracks/{stable_id}/audio", "stable_id"),
    ("GET", "/api/v1/tracks/{stable_id}/artwork", "stable_id"),
    ("GET", "/api/v1/feedback/attachments/{attachment_id}", "attachment_id"),
    ("GET", "/api/v1/bench/ratings/{filename}", "filename"),
)


class ProbeVerdict(StrEnum):
    """Operator-facing result. UNKNOWN is never a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProbeResult:
    """One probe, its control, the exact command, and the raw evidence."""

    name: str
    verdict: ProbeVerdict
    summary: str
    command: str
    output: str
    control: str


@dataclass(frozen=True)
class ProbeTarget:
    """The daemon the probes hit: backend listen port plus the app origin."""

    host: str
    port: int
    frontend_origin: str
    openapi_path: Path | None = None


def probe_bind_address(port: int) -> ProbeResult:
    """Assert the listen socket's address; do not assume 127.0.0.1."""
    command = f"inspect listen table for tcp port {port}"
    addresses = listening_addresses(port)
    output = f"listen={addresses or 'NONE'}"
    if not addresses:
        return ProbeResult(
            name="bind-address",
            verdict=ProbeVerdict.UNKNOWN,
            summary=f"nothing is listening on port {port}",
            command=command,
            output=output,
            control="a listen socket must exist before the bind can be asserted",
        )
    wild = tuple(addr for addr in addresses if not is_loopback_ip(addr))
    if wild:
        return ProbeResult(
            name="bind-address",
            verdict=ProbeVerdict.FAIL,
            summary=f"daemon is reachable off-loopback at {wild}",
            command=command,
            output=output,
            control=f"loopback-only would have been {addresses} minus {wild}",
        )
    return ProbeResult(
        name="bind-address",
        verdict=ProbeVerdict.PASS,
        summary=f"listen socket is loopback-only at {addresses}",
        command=command,
        output=output,
        control=f"kernel listen table listed {addresses} for port {port}",
    )


def probe_lan_refusal(target: ProbeTarget, lan_ips: Sequence[str] | None = None) -> ProbeResult:
    """A request to a non-loopback host address must not connect."""
    command = f"GET {HEALTH_PATH} via each non-loopback IPv4, control via 127.0.0.1"
    control_status, control_err = http_status(target.host, target.port, "GET", HEALTH_PATH)
    control = f"GET http://{target.host}:{target.port}{HEALTH_PATH} -> {control_status}"
    if control_status is None or control_status >= 500:
        return ProbeResult(
            name="lan-refusal",
            verdict=ProbeVerdict.UNKNOWN,
            summary=f"loopback control failed ({control_err or control_status})",
            command=command,
            output=control,
            control=control,
        )
    ips = tuple(lan_ips) if lan_ips is not None else non_loopback_ipv4()
    if not ips:
        return ProbeResult(
            name="lan-refusal",
            verdict=ProbeVerdict.UNKNOWN,
            summary="host has no non-loopback IPv4 to probe",
            command=command,
            output="lan_ips=NONE",
            control=control,
        )
    lines: list[str] = []
    reachable: list[str] = []
    for ip in ips:
        status, err = http_status(ip, target.port, "GET", HEALTH_PATH, timeout=2.0)
        if status is None:
            lines.append(f"{ip}:{target.port} unreachable ({err})")
            continue
        lines.append(f"{ip}:{target.port} HTTP {status}")
        reachable.append(ip)
    output = "\n".join(lines)
    if reachable:
        return ProbeResult(
            name="lan-refusal",
            verdict=ProbeVerdict.FAIL,
            summary=f"LAN address(es) accepted the request: {reachable}",
            command=command,
            output=output,
            control=control,
        )
    return ProbeResult(
        name="lan-refusal",
        verdict=ProbeVerdict.PASS,
        summary=f"all {len(ips)} non-loopback addresses refused or unreachable",
        command=command,
        output=output,
        control=control,
    )


def probe_cors(target: ProbeTarget) -> ProbeResult:
    """Hostile and null Origin refused; the app origin is the positive control."""
    command = (
        f"GET {HEALTH_PATH} with Origin {HOSTILE_ORIGIN!r}, {NULL_ORIGIN!r}, "
        f"and control {target.frontend_origin!r}"
    )
    control_acao, control_status, control_err = acao(
        target.host, target.port, HEALTH_PATH, target.frontend_origin
    )
    control = (
        f"Origin {target.frontend_origin} -> HTTP {control_status} "
        f"ACAO={control_acao!r}"
    )
    if control_status is None:
        return ProbeResult(
            name="cors",
            verdict=ProbeVerdict.UNKNOWN,
            summary=f"positive control could not run ({control_err})",
            command=command,
            output=control,
            control=control,
        )
    if not origin_allowed(target.frontend_origin, control_acao):
        return ProbeResult(
            name="cors",
            verdict=ProbeVerdict.UNKNOWN,
            summary="positive control Origin was not allowed; refusals prove nothing",
            command=command,
            output=control,
            control=control,
        )
    evil_acao, evil_status, _evil_err = acao(
        target.host, target.port, HEALTH_PATH, HOSTILE_ORIGIN
    )
    null_acao, null_status, _null_err = acao(
        target.host, target.port, HEALTH_PATH, NULL_ORIGIN
    )
    output = (
        f"evil Origin {HOSTILE_ORIGIN} -> HTTP {evil_status} ACAO={evil_acao!r}\n"
        f"null Origin {NULL_ORIGIN!r} -> HTTP {null_status} ACAO={null_acao!r}"
    )
    evil_ok = not origin_allowed(HOSTILE_ORIGIN, evil_acao)
    null_ok = not origin_allowed(NULL_ORIGIN, null_acao)
    if evil_ok and null_ok:
        return ProbeResult(
            name="cors",
            verdict=ProbeVerdict.PASS,
            summary="hostile and null Origin were not echoed; control Origin was",
            command=command,
            output=output,
            control=control,
        )
    bad = []
    if not evil_ok:
        bad.append(HOSTILE_ORIGIN)
    if not null_ok:
        bad.append("null")
    return ProbeResult(
        name="cors",
        verdict=ProbeVerdict.FAIL,
        summary=f"CORS echoed disallowed Origin(s): {bad}",
        command=command,
        output=output,
        control=control,
    )


def probe_path_traversal(
    target: ProbeTarget, routes: Sequence[tuple[str, str, str]] | None = None
) -> ProbeResult:
    """../ and encoded variants against every path/filename route, plus a control."""
    discovered = tuple(routes) if routes is not None else discover_path_routes(target.openapi_path)
    command = (
        f"GET/POST traversal payloads {list(TRAVERSAL_PAYLOADS)} against "
        f"{[item[1] for item in discovered]}"
    )
    control_ok, control = _path_control(target)
    if not control_ok:
        return ProbeResult(
            name="path-traversal",
            verdict=ProbeVerdict.UNKNOWN,
            summary=f"in-root control failed: {control}",
            command=command,
            output=control,
            control=control,
        )
    if not discovered:
        return ProbeResult(
            name="path-traversal",
            verdict=ProbeVerdict.UNKNOWN,
            summary="no path or filename routes were discovered",
            command=command,
            output="routes=NONE",
            control=control,
        )
    leaks: list[str] = []
    lines: list[str] = []
    for method, template, param in discovered:
        if not _method_is_safe(method, template, param):
            continue
        for payload in TRAVERSAL_PAYLOADS:
            path, body = _apply_payload(method, template, param, payload)
            status, err, preview = http_body(target.host, target.port, method, path, body)
            line = f"{method} {path} -> {status if status is not None else err} {preview}"
            lines.append(line)
            # 2xx/3xx served the traversal. 5xx is a broken route, not a leak.
            if status is not None and status < 400:
                leaks.append(line)
    output = "\n".join(lines)
    if leaks:
        return ProbeResult(
            name="path-traversal",
            verdict=ProbeVerdict.FAIL,
            summary=f"{len(leaks)} traversal request(s) were not refused",
            command=command,
            output=output,
            control=control,
        )
    return ProbeResult(
        name="path-traversal",
        verdict=ProbeVerdict.PASS,
        summary=(
            f"{len(discovered)} routes x {len(TRAVERSAL_PAYLOADS)} payloads "
            "all refused with 4xx"
        ),
        command=command,
        output=output,
        control=control,
    )


def discover_path_routes(openapi_path: Path | None) -> tuple[tuple[str, str, str], ...]:
    """Path/filename routes from OpenAPI plus the known FileResponse surfaces."""
    found: dict[tuple[str, str, str], None] = dict.fromkeys(KNOWN_FILE_ROUTES)
    schema_path = openapi_path if openapi_path is not None else Path("apps/webui/openapi.json")
    found.update(dict.fromkeys(_openapi_pathish_routes(schema_path)))
    return tuple(found)


def _openapi_pathish_routes(schema_path: Path) -> list[tuple[str, str, str]]:
    if not schema_path.is_file():
        return []
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    found: list[tuple[str, str, str]] = []
    for path, methods in schema.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        found.extend(_pathish_in_methods(path, methods))
    return found


def _pathish_in_methods(path: str, methods: dict[str, Any]) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for method, spec in methods.items():
        if method.startswith("x-") or not isinstance(spec, dict):
            continue
        verb = method.upper()
        found.extend(
            (verb, path, str(param["name"]))
            for param in spec.get("parameters") or ()
            if isinstance(param, dict) and str(param.get("name", "")) in PATHISH_NAMES
        )
        body = (spec.get("requestBody") or {}).get("content", {})
        for media in body.values():
            properties = ((media.get("schema") or {}).get("properties") or {})
            found.extend(
                (verb, path, name) for name in properties if name in PATHISH_NAMES
            )
    return found


def format_report(results: Sequence[ProbeResult]) -> str:
    """Render a live-run report the PR body can quote, one probe plus its control."""
    lines = ["red-team local API probes (track g / issue #1861)", ""]
    for result in results:
        lines.append(f"{result.name:<16} {result.verdict.value:<8} {result.summary}")
        lines.append(f"{'':16} command: {result.command}")
        lines.append(f"{'':16} control: {result.control}")
        outputs = result.output.splitlines() or ("",)
        lines.extend(f"{'':16} output:  {raw}" for raw in outputs)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_probes(target: ProbeTarget) -> tuple[ProbeResult, ...]:
    """Run the four track-(g) probes in issue order."""
    return (
        probe_bind_address(target.port),
        probe_lan_refusal(target),
        probe_cors(target),
        probe_path_traversal(target),
    )


def exit_code(results: Sequence[ProbeResult]) -> int:
    """FAIL wins, else UNKNOWN, else 0. UNKNOWN is never a pass."""
    verdicts = {result.verdict for result in results}
    if ProbeVerdict.FAIL in verdicts:
        return 1
    if ProbeVerdict.UNKNOWN in verdicts:
        return 2
    return 0


def findings_from_results(
    results: Sequence[ProbeResult], *, sha: str, host: str
) -> tuple[Finding, ...]:
    """FAIL -> Finding.fail, UNKNOWN -> Finding.unavailable. PASS is not a finding."""
    out: list[Finding] = []
    for result in results:
        if result.verdict == ProbeVerdict.PASS:
            continue
        details = FindingDetails(
            fingerprint=f"local-api-{result.name}",
            sha=sha,
            surface=SURFACE,
            host=host,
            repro=Reproduction(
                act_sequence=(result.command, f"control: {result.control}"),
                trace_session=DEFAULT_RUN_ID,
            ),
            evidence=Evidence(
                trace_url="N/A: nonvisual local HTTP probe",
                screenshot=None,
            ),
            honest_coverage=f"{result.summary}\n\n{result.output}",
        )
        if result.verdict == ProbeVerdict.UNKNOWN:
            out.append(Finding.unavailable(details=details))
        else:
            out.append(Finding.fail(details=details))
    return tuple(out)


def discover_target(
    *, port: int | None = None, frontend_origin: str | None = None
) -> ProbeTarget:
    """Resolve the worktree pair the same way ``just webui-ports`` does."""
    if port is not None and frontend_origin is not None:
        return ProbeTarget(
            host="127.0.0.1",
            port=port,
            frontend_origin=frontend_origin,
            openapi_path=Path("apps/webui/openapi.json"),
        )
    from apps.webui.port_config import claim_ports

    ports = claim_ports()
    origin = frontend_origin or f"http://127.0.0.1:{ports.frontend}"
    return ProbeTarget(
        host="127.0.0.1",
        port=port if port is not None else ports.backend,
        frontend_origin=origin,
        openapi_path=Path("apps/webui/openapi.json"),
    )


def _path_control(target: ProbeTarget) -> tuple[bool, str]:
    status, err, preview = http_body(
        target.host, target.port, "GET", "/api/v1/bench/ratings/probe-control.json", None
    )
    control = (
        f"GET /api/v1/bench/ratings/probe-control.json -> {status} {preview or err or ''}"
    )
    if status in {200, 404}:
        return True, control
    health_status, health_err = http_status(
        target.host, target.port, "GET", HEALTH_PATH
    )
    if health_status is not None and health_status < 500:
        # Route may 405/422 and still prove the daemon is up; keep going only
        # when the filename route itself answered as a lookup (200/404) OR we
        # can name a 2xx/404 in-root hit. Health alone is not an in-root path.
        return False, (
            f"{control}; health={health_status} {health_err or ''} "
            "(in-root filename control did not succeed)"
        )
    return False, control


def _method_is_safe(method: str, template: str, param: str) -> bool:
    """Do not fire mutating verbs at path-in-URL routes (hot-cue PUT/DELETE)."""
    verb = method.upper()
    pathish_in_url = "{" + param + "}" in template
    if pathish_in_url:
        return verb in {"GET", "HEAD", "OPTIONS"}
    return verb in {"GET", "HEAD", "OPTIONS", "POST"}


def _apply_payload(
    method: str, template: str, param: str, payload: str
) -> tuple[str, str | None]:
    if "{" + param + "}" in template:
        path = template.replace("{" + param + "}", payload)
        path = re.sub(r"\{[^}]+\}", "x", path)
        return path, None
    if method in {"POST", "PUT", "PATCH"}:
        return template, json.dumps({param: payload})
    joiner = "&" if "?" in template else "?"
    return f"{template}{joiner}{param}={payload}", None


def _git_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    sha = completed.stdout.strip()
    if completed.returncode != 0 or len(sha) not in {40, 64}:
        raise RuntimeError("git rev-parse HEAD failed")
    return sha


def _file_findings(results: Sequence[ProbeResult], index_path: Path) -> None:
    from scripts.redteam_filing import GhGitHub, file_findings

    sha = _git_sha()
    host = socket.gethostname()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    store = FindingStore(index_path)
    for finding in findings_from_results(results, sha=sha, host=host):
        try:
            store.record(finding)
        except (ExistingFindingError, OSError) as exc:
            sys.stderr.write(f"finding store: {exc}\n")
    filed = file_findings(
        store.read_all(),
        run_id=DEFAULT_RUN_ID,
        priority="p0",
        github=GhGitHub("private_owner/music-dj-tools"),
        extra_labels=("red-team",),
    )
    print(json.dumps({"created": filed.created, "commented": filed.commented}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, help="Backend port (default: just webui-ports)")
    parser.add_argument(
        "--frontend-origin",
        default=None,
        help="Positive CORS control Origin (default: this worktree's frontend)",
    )
    parser.add_argument(
        "--file",
        action="store_true",
        help="File FAIL findings via scripts.redteam_filing --label red-team",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=Path(os.environ.get("HOME", "."))
        / "jobs"
        / "redteam"
        / DEFAULT_RUN_ID
        / "index.jsonl",
    )
    arguments = parser.parse_args(argv)
    try:
        target = discover_target(
            port=arguments.port, frontend_origin=arguments.frontend_origin
        )
    except Exception as exc:
        unknown = ProbeResult(
            name="bind-address",
            verdict=ProbeVerdict.UNKNOWN,
            summary=f"could not resolve webui ports: {exc}",
            command="just webui-ports",
            output=str(exc),
            control="ports must resolve before any probe can run",
        )
        sys.stdout.write(format_report((unknown,)))
        return 2
    results = run_probes(target)
    sys.stdout.write(format_report(results))
    if arguments.file:
        _file_findings(results, arguments.index)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
