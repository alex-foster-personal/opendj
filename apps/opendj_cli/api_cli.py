"""``opendj api METHOD PATH``: raw HTTP escape hatch to the library daemon.

Resolves the backend from worktree ports when configured (``MUSIC_DJ_BACKEND_PORT``
or the root ``.env``, same as ``just webui-ports``), otherwise from the installed
app's ``.engine.lock`` (same discovery as ``opendj play``, ``opendj state``, and
``opendj mcp``). Exit codes: see :mod:`apps.opendj_cli` (canonical table).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx

from apps.opendj_cli import (
    EXIT_CONFLICT,
    EXIT_FAILED,
    EXIT_NO_ENGINE,
    EXIT_OK,
    EXIT_PRECONDITION,
)
from apps.opendj_cli.mcp_safety import SafetyRefusal, validate_api_path
from apps.opendj_cli.origin import EngineNotRunning, EngineOrigin, resolve_origin, unreachable
from apps.webui.port_config import PortConfigError, resolve_ports

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)

REQUEST_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class _BackendTarget:
    base_url: str
    origin: EngineOrigin | None = None


def resolve_backend_base_url(
    environ: Mapping[str, str] | None = None,
    lock_path: Path | None = None,
) -> _BackendTarget:
    """Worktree ports when configured, else the engine lock file's origin."""
    try:
        return _BackendTarget(
            base_url=resolve_ports(environ=environ).api_proxy_target,
        )
    except PortConfigError:
        origin = resolve_origin(lock_path, environ)
        return _BackendTarget(base_url=origin.base_url, origin=origin)


class UsageError(ValueError):
    """Argv or port configuration the caller can fix."""


@dataclass(frozen=True)
class _ParsedRequest:
    method: str
    path: str
    json_body: str | None
    fields: tuple[tuple[str, Any], ...]
    extra_headers: tuple[tuple[str, str], ...]


def exit_for_status(status_code: int) -> int:
    if 200 <= status_code < 300:
        return EXIT_OK
    if status_code == 412:
        return EXIT_PRECONDITION
    if status_code == 409:
        return EXIT_CONFLICT
    return EXIT_FAILED


def _parse_field(raw: str) -> tuple[str, Any]:
    name, separator, value = raw.partition("=")
    if separator == "":
        raise UsageError(f"-f expects key=value, got {raw!r}")
    key = name.strip()
    if key == "":
        raise UsageError(f"-f expects a non-empty key, got {raw!r}")
    text = value.strip()
    if text == "":
        return key, ""
    try:
        return key, json.loads(text)
    except json.JSONDecodeError:
        return key, text


def _request_body(
    method: str,
    json_body: str | None,
    fields: Sequence[tuple[str, Any]],
) -> bytes | None:
    if method in {"GET", "HEAD", "DELETE", "OPTIONS"}:
        if json_body is not None or fields:
            raise UsageError(f"{method} does not accept a request body")
        return None
    if json_body is not None and fields:
        try:
            payload = json.loads(json_body)
        except json.JSONDecodeError as error:
            raise UsageError(f"--json is not valid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise UsageError("--json with -f requires a JSON object body")
        merged = dict(payload)
        merged.update(fields)
        return json.dumps(merged).encode("utf-8")
    if json_body is not None:
        try:
            json.loads(json_body)
        except json.JSONDecodeError as error:
            raise UsageError(f"--json is not valid JSON: {error}") from error
        return json_body.encode("utf-8")
    if fields:
        return json.dumps(dict(fields)).encode("utf-8")
    return None


def _emit_body(stream: TextIO, body: bytes) -> None:
    text = body.decode("utf-8", errors="replace")
    parsed = json.loads(text)
    print(json.dumps(parsed, indent=2, sort_keys=True), file=stream)


def _emit_body_or_text(stream: TextIO, body: bytes) -> None:
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(text, file=stream, end="" if text.endswith("\n") else None)
        if text and not text.endswith("\n"):
            print(file=stream)
        return
    print(json.dumps(parsed, indent=2, sort_keys=True), file=stream)


def _fail_usage(message: str) -> int:
    print(f"opendj api: {message}", file=sys.stderr)
    return EXIT_FAILED


def _fail_structured(
    as_json: bool,
    *,
    code: str,
    message: str,
    exit_code: int,
) -> int:
    if as_json:
        print(
            json.dumps(
                {
                    "error": {"code": code, "message": message},
                    "exit_code": exit_code,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj api: {message}", file=sys.stderr)
    return exit_code


def _fail_unreachable(base_url: str, port: int, error: BaseException) -> int:
    print(
        f"opendj api: daemon not reachable at port {port} ({base_url}): "
        f"{type(error).__name__}: {error}",
        file=sys.stderr,
    )
    return EXIT_FAILED


def _parse_header(raw: str) -> tuple[str, str]:
    name, separator, value = raw.partition(":")
    if separator == "":
        raise UsageError(f"-H expects name:value, got {raw!r}")
    key = name.strip()
    if key == "":
        raise UsageError(f"-H expects a non-empty header name, got {raw!r}")
    return key, value.strip()


def _parse_request(rest: Sequence[str]) -> _ParsedRequest:
    if not rest:
        raise UsageError("METHOD and PATH are required")
    method = rest[0].upper()
    if method not in HTTP_METHODS:
        raise UsageError(f"unknown HTTP method {rest[0]!r}")
    if len(rest) < 2:
        raise UsageError("PATH is required")
    path = rest[1]
    if not path.startswith("/"):
        raise UsageError(f"PATH must start with '/', got {path!r}")

    json_body: str | None = None
    fields: list[tuple[str, Any]] = []
    extra_headers: list[tuple[str, str]] = []
    position = 2
    while position < len(rest):
        token = rest[position]
        if token == "--json":
            if position + 1 >= len(rest):
                raise UsageError("--json requires a value")
            json_body = rest[position + 1]
            position += 2
            continue
        if token == "-f":
            if position + 1 >= len(rest):
                raise UsageError("-f requires key=value")
            fields.append(_parse_field(rest[position + 1]))
            position += 2
            continue
        if token == "-H":
            if position + 1 >= len(rest):
                raise UsageError("-H requires name:value")
            extra_headers.append(_parse_header(rest[position + 1]))
            position += 2
            continue
        raise UsageError(f"unexpected argument {token!r}")
    return _ParsedRequest(
        method, path, json_body, tuple(fields), tuple(extra_headers),
    )


def _write_response(
    response: httpx.Response,
    *,
    url: str,
    as_json: bool,
) -> int:
    exit_code = exit_for_status(response.status_code)
    payload = response.content
    if exit_code == EXIT_OK:
        if not payload:
            return EXIT_OK
        try:
            json.loads(payload)
        except json.JSONDecodeError:
            return _fail_structured(
                as_json,
                code="not_json",
                message="response is not JSON",
                exit_code=EXIT_FAILED,
            )
        _emit_body(sys.stdout, payload)
        return EXIT_OK
    if payload:
        _emit_body_or_text(sys.stderr, payload)
    elif not as_json:
        print(f"opendj api: {response.status_code} from {url}", file=sys.stderr)
    return exit_code


def run(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    environ: Mapping[str, str] | None = None,
    lock: Path | None = None,
) -> int:
    try:
        parsed = _parse_request(rest)
        validate_api_path(parsed.path)
        target = resolve_backend_base_url(environ=environ, lock_path=lock)
        body = _request_body(parsed.method, parsed.json_body, parsed.fields)
    except SafetyRefusal as error:
        return _fail_structured(
            as_json,
            code=error.code,
            message=str(error),
            exit_code=EXIT_FAILED,
        )
    except EngineNotRunning as error:
        return _fail_structured(
            as_json,
            code="engine_not_running",
            message=str(error),
            exit_code=EXIT_NO_ENGINE,
        )
    except UsageError as error:
        return _fail_usage(str(error))

    base_url = target.base_url
    url = f"{base_url}{parsed.path}"
    port = int(base_url.rsplit(":", 1)[-1])
    headers = {"Accept": "application/json"}
    for name, value in parsed.extra_headers:
        headers[name] = value
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.request(parsed.method, url, headers=headers, content=body)
    except (httpx.TransportError, httpx.TimeoutException) as error:
        if target.origin is not None:
            print(f"opendj api: {unreachable(target.origin, error)}", file=sys.stderr)
            return EXIT_NO_ENGINE
        return _fail_unreachable(base_url, port, error)
    return _write_response(response, url=url, as_json=as_json)
