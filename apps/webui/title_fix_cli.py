"""CLI parity for the title-fix admin pane (TFIX-05): every button is one subcommand.

All commands call the same local HTTP endpoints as the UI and print the JSON response.

    python -m apps.webui.title_fix_cli config show
    python -m apps.webui.title_fix_cli config set --enabled true --policy off --cap 0.5 --families version,cover
    printf %s "$KEY" | python -m apps.webui.title_fix_cli key set     # read from stdin, never argv
    python -m apps.webui.title_fix_cli key clear
    python -m apps.webui.title_fix_cli scan [--limit N | --ids A B ...] [--wait]
    python -m apps.webui.title_fix_cli scan-status SCAN_ID
    python -m apps.webui.title_fix_cli proposals [--state pending] [--status review] [--tag remix] [--source regex]
    python -m apps.webui.title_fix_cli apply PROPOSAL_ID ... --confirm
    python -m apps.webui.title_fix_cli reject PROPOSAL_ID ...
    python -m apps.webui.title_fix_cli batches
    python -m apps.webui.title_fix_cli undo BATCH_ID --confirm [--skip-conflicts]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from apps.webui.port_config import PortConfigError, resolve_backend_port


class CFG:
    POLL_S = 1.0
    WAIT_MAX_S = 6 * 3600
    REQUEST_TIMEOUT_S = 600.0  # one request, the longest being an apply of a large selection


def _request(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout_s: float | None = None,
) -> Any:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=CFG.REQUEST_TIMEOUT_S if timeout_s is None else timeout_s) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:  # daemon down, DNS failure or a socket timeout: one line, exit 2
        raise RuntimeError(f"cannot reach {base_url}: {exc}") from exc


def _bool(text: str) -> bool:
    if text not in ("true", "false"):
        raise argparse.ArgumentTypeError("expected true or false")
    return text == "true"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.webui.title_fix_cli")
    parser.add_argument("--base-url", default=None, help="daemon API root, default from port config")
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config").add_subparsers(dest="config_command", required=True)
    config.add_parser("show")
    cset = config.add_parser("set")
    cset.add_argument("--enabled", type=_bool, required=True)
    cset.add_argument("--policy", choices=("off", "safe_tier", "gate_approved"), required=True)
    cset.add_argument("--cap", type=float, required=True, help="per-scan spend cap in USD")
    cset.add_argument("--families", required=True, help="comma list of version,context,cover,project")
    key = sub.add_parser("key").add_subparsers(dest="key_command", required=True)
    key.add_parser("set", help="reads the key from stdin")
    key.add_parser("clear")
    scan = sub.add_parser("scan")
    scope = scan.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int)
    scope.add_argument("--ids", nargs="+")
    scan.add_argument("--wait", action="store_true", help="poll until the scan finishes")
    sub.add_parser("scan-status").add_argument("scan_id")
    props = sub.add_parser("proposals")
    props.add_argument("--state", default="pending", help="a proposal state, or any")
    props.add_argument("--status", dest="fixer_status")
    props.add_argument("--source")
    props.add_argument("--tag")
    props.add_argument("--scan-id")
    props.add_argument("--batch-id")
    props.add_argument("--limit", type=int, default=100)
    props.add_argument("--offset", type=int, default=0)
    apply = sub.add_parser("apply")
    apply.add_argument("proposal_ids", nargs="+")
    apply.add_argument("--confirm", action="store_true")
    sub.add_parser("reject").add_argument("proposal_ids", nargs="+")
    sub.add_parser("batches")
    undo = sub.add_parser("undo")
    undo.add_argument("batch_id")
    undo.add_argument("--skip-conflicts", action="store_true")
    undo.add_argument("--confirm", action="store_true")
    return parser


def _wait_for_scan(base_url: str, scan: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + CFG.WAIT_MAX_S
    while scan["state"] == "running":
        if time.monotonic() > deadline:
            raise RuntimeError(f"scan {scan['scan_id']} still running after {CFG.WAIT_MAX_S}s")
        sys.stderr.write(
            f"scan {scan['scan_id'][:8]}: {scan['processed']}/{scan['requested']} "
            f"cost ${scan['cost_usd']:.4f} of ${scan['spend_cap_usd']:.2f}\n"
        )
        time.sleep(CFG.POLL_S)
        remaining_s = deadline - time.monotonic()  # a stalled poll must not outlast the advertised wait
        if remaining_s <= 0:
            raise RuntimeError(f"scan {scan['scan_id']} still running after {CFG.WAIT_MAX_S}s")
        scan = _request(
            base_url, "GET", f"/title-fix/scans/{scan['scan_id']}", timeout_s=min(CFG.REQUEST_TIMEOUT_S, remaining_s)
        )
    if scan["state"] == "failed":  # a waited scan that did not finish is a failed command, never a clean exit
        raise RuntimeError(f"scan {scan['scan_id']} failed: {scan.get('error') or 'no reason recorded'}")
    return scan


Handler = Callable[[argparse.Namespace, str], Any]


def _config_set(args: argparse.Namespace, base_url: str) -> Any:
    families = [f for f in args.families.split(",") if f]
    return _request(
        base_url,
        "PUT",
        "/title-fix/config",
        {
            "enabled": args.enabled,
            "auto_apply_policy": args.policy,
            "spend_cap_usd": args.cap,
            "tag_families": families,
        },
    )


def _key_set(_args: argparse.Namespace, base_url: str) -> Any:
    secret = sys.stdin.read().strip()
    if not secret:
        raise RuntimeError("key set reads the key from stdin; stdin was empty")
    return _request(base_url, "PUT", "/title-fix/key", {"api_key": secret})


def _scan(args: argparse.Namespace, base_url: str) -> Any:
    body: dict[str, Any] = {}
    if args.ids:
        body["stable_ids"] = args.ids
    elif args.limit is not None:
        body["limit"] = args.limit
    scan = _request(base_url, "POST", "/title-fix/scans", body)
    return _wait_for_scan(base_url, scan) if args.wait else scan


def _proposals(args: argparse.Namespace, base_url: str) -> Any:
    query = {
        k: v
        for k, v in {
            "state": args.state,
            "fixer_status": args.fixer_status,
            "source": args.source,
            "tag": args.tag,
            "scan_id": args.scan_id,
            "batch_id": args.batch_id,
            "limit": args.limit,
            "offset": args.offset,
        }.items()
        if v is not None
    }
    return _request(base_url, "GET", f"/title-fix/proposals?{urlencode(query)}")


def _apply(args: argparse.Namespace, base_url: str) -> Any:
    if not args.confirm:
        raise RuntimeError("apply changes track titles/artists/tags; rerun with --confirm")
    return _request(base_url, "POST", "/title-fix/apply", {"proposal_ids": args.proposal_ids})


def _undo(args: argparse.Namespace, base_url: str) -> Any:
    if not args.confirm:
        raise RuntimeError("undo rewrites track titles/artists/tags; rerun with --confirm")
    return _request(
        base_url, "POST", f"/title-fix/batches/{args.batch_id}/undo", {"skip_conflicts": args.skip_conflicts}
    )


HANDLERS: dict[str, Handler] = {
    "config show": lambda _a, url: _request(url, "GET", "/title-fix/config"),
    "config set": _config_set,
    "key set": _key_set,
    "key clear": lambda _a, url: _request(url, "DELETE", "/title-fix/key"),
    "scan": _scan,
    "scan-status": lambda a, url: _request(url, "GET", f"/title-fix/scans/{a.scan_id}"),
    "proposals": _proposals,
    "apply": _apply,
    "reject": lambda a, url: _request(url, "POST", "/title-fix/reject", {"proposal_ids": a.proposal_ids}),
    "batches": lambda _a, url: _request(url, "GET", "/title-fix/batches"),
    "undo": _undo,
}


def run(args: argparse.Namespace, base_url: str) -> Any:
    sub = getattr(args, "config_command", None) or getattr(args, "key_command", None)
    name = f"{args.command} {sub}" if sub else args.command
    if name not in HANDLERS:
        raise RuntimeError(f"unhandled command {name!r}")
    return HANDLERS[name](args, base_url)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base_url = args.base_url or f"http://127.0.0.1:{resolve_backend_port(None)}/api/v1"
    except PortConfigError as exc:
        sys.stderr.write(f"title-fix: port configuration error: {exc}\n")
        return 2
    try:
        result = run(args, base_url)
    except RuntimeError as exc:
        sys.stderr.write(f"title-fix: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
