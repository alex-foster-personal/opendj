"""Install-token admin over HTTP. Prints the Worker's JSON; refusals exit nonzero."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

# ----- configuration ------------------------------------------------------
ADMIN_URL_ENV: str = "MDT_AI_GATEWAY_ADMIN_URL"
ADMIN_TOKEN_ENV: str = "MDT_AI_GATEWAY_ADMIN_TOKEN"
TIMEOUT_S: float = 30.0

EXIT_REFUSED: int = 1
EXIT_UNCONFIGURED: int = 2


class AdminUnconfigured(Exception):
    """The admin URL or token env is unset."""


class GatewayRefused(Exception):
    """The Worker answered non-2xx; ``detail`` is its ``{code, message}`` envelope."""

    def __init__(self, status: int, detail: dict[str, Any]) -> None:
        super().__init__(f"{status} {detail.get('code')}: {detail.get('message')}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class AdminClient:
    base_url: str
    token: str

    @classmethod
    def from_env(cls) -> AdminClient:
        url = os.environ.get(ADMIN_URL_ENV, "").strip()
        token = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
        missing = [name for name, value in ((ADMIN_URL_ENV, url), (ADMIN_TOKEN_ENV, token)) if not value]
        if missing:
            raise AdminUnconfigured(f"set {' and '.join(missing)} to reach the gateway admin API")
        return cls(base_url=url.rstrip("/"), token=token)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = httpx.request(
            method,
            f"{self.base_url}/admin{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=TIMEOUT_S,
            **kwargs,
        )
        body = response.json()
        if response.is_error:
            raise GatewayRefused(response.status_code, body.get("detail", body))
        return body

    def mint(self, plan: str | None = None, label: str | None = None) -> dict[str, Any]:
        payload = {k: v for k, v in (("plan", plan), ("label", label)) if v is not None}
        return self._call("POST", "/installs", json=payload)

    def revoke(self, install_id: str) -> dict[str, Any]:
        return self._call("POST", f"/installs/{install_id}/revoke")

    def set_plan(self, install_id: str, plan: str) -> dict[str, Any]:
        return self._call("PUT", f"/installs/{install_id}/plan", json={"plan": plan})

    def usage(self, install_id: str, days: int = 7) -> dict[str, Any]:
        return self._call("GET", f"/installs/{install_id}/usage", params={"days": days})


# ----- CLI ----------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.ai_gateway_admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("mint", help="mint an install token (printed once)")
    mint.add_argument("--plan", help="plan name from the Worker's PLANS (Worker default: free)")
    mint.add_argument("--label", help="free-text note stored with the install")
    revoke = sub.add_parser("revoke", help="revoke an install's token")
    revoke.add_argument("install_id")
    set_plan = sub.add_parser("set-plan", help="move an install to another plan")
    set_plan.add_argument("install_id")
    set_plan.add_argument("plan")
    usage = sub.add_parser("usage", help="per-UTC-day spend for an install")
    usage.add_argument("install_id")
    usage.add_argument("--days", type=int, default=7)
    return parser


def _run_command(client: AdminClient, args: argparse.Namespace) -> dict[str, Any]:
    match args.command:
        case "mint":
            return client.mint(args.plan, args.label)
        case "revoke":
            return client.revoke(args.install_id)
        case "set-plan":
            return client.set_plan(args.install_id, args.plan)
        case "usage":
            return client.usage(args.install_id, args.days)
        case _:
            raise AssertionError(f"unknown command {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        client = AdminClient.from_env()
    except AdminUnconfigured as exc:
        print(exc, file=sys.stderr)
        return EXIT_UNCONFIGURED
    try:
        result = _run_command(client, args)
    except GatewayRefused as exc:
        print(json.dumps({"status": exc.status, "detail": exc.detail}), file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(result, indent=2))
    return 0
