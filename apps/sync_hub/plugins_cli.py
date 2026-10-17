"""``python -m apps.sync_hub plugins index``: CLI twin of ``GET /sync/plugins/index``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.shared.state import machine_identity
from apps.sync_hub import config as sync_config
from apps.sync_hub import spoke_credential
from apps.sync_hub.transport import HttpTransport

INDEX_PATH = "/api/v1/sync/plugins/index"


def add_plugins_parser(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    plugins = subcommands.add_parser("plugins", help="hosted plugin catalog (CSPAY-06)")
    actions = plugins.add_subparsers(dest="plugins_action", required=True)
    actions.add_parser(
        "index",
        parents=[common],
        help="fetch the hub plugin catalog for this machine",
    )


def fetch_plugin_index(data_dir: Path) -> dict[str, object]:
    effective = sync_config.resolve_config(data_dir)
    if not effective.configured or effective.hub_url is None:
        raise sync_config.CloudSyncConfigError(
            "cloudsync is disabled or hub_url is unset; run `config set --enabled --hub URL`"
        )
    # No credential on file is sent as none: the hub's credential mode decides,
    # so a self-hosted observe-mode hub serves the CLI exactly as it serves HTTP.
    bearer = spoke_credential.read_credential(data_dir)
    machine_id = machine_identity.get_or_create_machine_id(data_dir)
    transport = HttpTransport(effective.hub_url, bearer=bearer)
    return transport.get(INDEX_PATH, params={"machine_id": machine_id})


def run_plugins(args: argparse.Namespace) -> None:
    if args.plugins_action != "index":
        raise AssertionError(f"unknown plugins action {args.plugins_action!r}")
    payload = fetch_plugin_index(args.data_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["add_plugins_parser", "fetch_plugin_index", "run_plugins"]
