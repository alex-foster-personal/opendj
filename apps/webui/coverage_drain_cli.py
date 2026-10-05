"""CLI twin of the coverage auto-drain HTTP surface.

    python -m apps.webui.coverage_drain_cli status
    python -m apps.webui.coverage_drain_cli start | stop
    python -m apps.webui.coverage_drain_cli enable | disable
    python -m apps.webui.coverage_drain_cli step-on | step-off --step analysis
    python -m apps.webui.coverage_drain_cli transient-cap --value 2
    python -m apps.webui.coverage_drain_cli retry
    python -m apps.webui.coverage_drain_cli no-source-list
    python -m apps.webui.coverage_drain_cli no-source-mark --stable-id S --reason "why"
    python -m apps.webui.coverage_drain_cli no-source-clear --stable-id S

Talks to the running engine (worktree ports when configured, else the engine
lock file's origin). Prints the JSON response; exits non-zero on any non-2xx.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import quote

import httpx

from apps.shared.engine_origin import resolve_origin
from apps.webui.port_config import PortConfigError, resolve_ports
from apps.webui.server.coverage_drain_state import SWITCHABLE_STEPS

PREFIX: str = "/api/v1/coverage-drain"
NO_SOURCE: str = "/api/v1/coverage-outcomes/stems/no-source"
REQUEST_TIMEOUT_S: float = 30.0
Request = tuple[str, str, dict[str, Any] | None]
VERBS: dict[str, Request] = {
    "status": ("GET", f"{PREFIX}/status", None),
    "start": ("POST", f"{PREFIX}/start", None),
    "stop": ("POST", f"{PREFIX}/stop", None),
    "enable": ("PUT", f"{PREFIX}/config", {"enabled": True}),
    "disable": ("PUT", f"{PREFIX}/config", {"enabled": False}),
    "retry": ("POST", f"{PREFIX}/retry", None),
    "no-source-list": ("GET", NO_SOURCE, None),
}
#: Verbs whose request is built from arguments, with the arguments they need.
ARGUMENT_VERBS: dict[str, tuple[str, ...]] = {
    "step-on": ("step",),
    "step-off": ("step",),
    "transient-cap": ("value",),
    "no-source-mark": ("stable_id", "reason"),
    "no-source-clear": ("stable_id",),
}


def request_for(verb: str, **arguments: Any) -> Request:
    if verb in VERBS:
        return VERBS[verb]
    missing = [name for name in ARGUMENT_VERBS[verb] if arguments.get(name) is None]
    if missing:
        flags = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise ValueError(f"{verb} needs {flags}")
    request: Request
    if verb in {"step-on", "step-off"}:
        request = "PUT", f"{PREFIX}/config", {"steps": {arguments["step"]: verb == "step-on"}}
    elif verb == "transient-cap":
        request = "PUT", f"{PREFIX}/config", {"transient_bundle_cap": arguments["value"]}
    elif verb == "no-source-mark":
        body = {"stable_id": arguments["stable_id"], "reason": arguments["reason"]}
        request = "POST", NO_SOURCE, body
    elif verb == "no-source-clear":
        request = "DELETE", f"{NO_SOURCE}/{quote(arguments['stable_id'], safe='')}", None
    else:
        raise ValueError(f"unhandled verb {verb!r}")
    return request


def _backend_base_url() -> str:
    """Worktree ports when configured, else the engine lock file's origin."""
    try:
        return resolve_ports().api_proxy_target
    except PortConfigError:
        return resolve_origin().base_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coverage_drain_cli", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=sorted({*VERBS, *ARGUMENT_VERBS}))
    parser.add_argument("--step", choices=SWITCHABLE_STEPS)
    parser.add_argument("--value", type=int)
    parser.add_argument("--stable-id")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        method, path, body = request_for(
            args.verb, step=args.step, value=args.value,
            stable_id=args.stable_id, reason=args.reason,
        )
    except ValueError as error:
        parser.error(str(error))
    base_url = _backend_base_url()
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, f"{base_url}{path}", json=body)
    sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
