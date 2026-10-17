"""CLI twin of spoke billing proxy routes (CSPAY-04)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.sync_hub import spoke_billing

EXIT_BILLING_FAILURE: int = 1


def add_billing_parser(subcommands: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    # --data-dir lives on each leaf, so it is accepted after the action
    # (``billing checkout --data-dir DIR``), like every other subcommand.
    billing = subcommands.add_parser(
        "billing",
        help="hosted Polar billing on the configured hub (checkout, portal, refresh)",
    )
    billing_sub = billing.add_subparsers(dest="billing_command", required=True)
    checkout = billing_sub.add_parser("checkout", parents=[common], help="mint a Polar checkout URL")
    checkout.add_argument(
        "--feature-id",
        default=spoke_billing.HOSTED_HUB_FEATURE_ID,
        help=f"feature to buy (default {spoke_billing.HOSTED_HUB_FEATURE_ID})",
    )
    billing_sub.add_parser("portal", parents=[common], help="open Polar's customer portal URL")
    billing_sub.add_parser("refresh", parents=[common], help="reconcile Polar state and print standings")


def run_billing(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    command = args.billing_command
    if command == "checkout":
        payload: dict[str, str] = {"feature_id": args.feature_id}
    elif command in ("portal", "refresh"):
        payload = {}
    else:
        raise AssertionError(f"unknown billing subcommand {command!r}")
    try:
        response = spoke_billing.post_billing(data_dir, command, payload)
    except spoke_billing.SpokeBillingError as exc:
        print(json.dumps({"code": exc.code, "message": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return EXIT_BILLING_FAILURE
    print(json.dumps(response.body, indent=2, sort_keys=True))
    return 0 if response.status_code < 400 else EXIT_BILLING_FAILURE


__all__ = ["EXIT_BILLING_FAILURE", "add_billing_parser", "run_billing"]
