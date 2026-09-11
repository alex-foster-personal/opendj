"""``python -m apps.sync_hub config show|set``: the CLI twin of ``/cloudsync/config``.

    uv run python -m apps.sync_hub config show --data-dir DIR
    uv run python -m apps.sync_hub config set  --data-dir DIR
                                               [--enabled | --disabled]
                                               [--hub URL] [--name NAME]

Both print the same JSON object ``GET /api/v1/cloudsync/config`` returns:
``{path, file, effective}``, where ``effective`` names the winning source
per field (an env override wins over the file). ``set`` MERGES the flags
given onto the current file (a missing file starts from disabled, no hub, no
name) and validates the result exactly as ``PUT`` does, so ``--enabled``
without a hub on file or on the command line is refused.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from apps.sync_hub import config as sync_config


def add_config_parser(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    """Register ``config show`` and ``config set`` on the operator CLI."""
    config_command = subcommands.add_parser(
        "config", help="show or set this machine's persisted CloudSync config"
    )
    actions = config_command.add_subparsers(dest="config_action", required=True)
    actions.add_parser("show", parents=[common], help="print the config as JSON")
    set_command = actions.add_parser(
        "set", parents=[common], help="merge the given fields into the config file"
    )
    toggle = set_command.add_mutually_exclusive_group()
    toggle.add_argument("--enabled", dest="enabled", action="store_const", const=True)
    toggle.add_argument("--disabled", dest="enabled", action="store_const", const=False)
    set_command.add_argument("--hub", default=None, help="the hub base URL")
    set_command.add_argument("--name", default=None, help="this machine's display name")


def set_config(
    data_dir: Path,
    *,
    enabled: bool | None,
    hub_url: str | None,
    machine_name: str | None,
) -> sync_config.CloudSyncConfig:
    """Merge the given fields onto the stored file, validate, and persist."""
    if enabled is None and hub_url is None and machine_name is None:
        raise sync_config.CloudSyncConfigError(
            "config set needs at least one of --enabled/--disabled, --hub, --name"
        )
    stored = sync_config.read_config(data_dir)
    base: dict[str, object] = {"enabled": False, "hub_url": None, "machine_name": None}
    merged: dict[str, object] = base if stored is None else stored.model_dump()
    updates: dict[str, object] = {
        "enabled": enabled,
        "hub_url": hub_url,
        "machine_name": machine_name,
    }
    merged.update({key: value for key, value in updates.items() if value is not None})
    try:
        config = sync_config.CloudSyncConfig.model_validate(merged)
    except ValidationError as exc:
        raise sync_config.CloudSyncConfigError(f"refused: {exc}") from exc
    sync_config.write_config(data_dir, config)
    return config


def run_config(args: argparse.Namespace) -> None:
    """Handle ``config show`` / ``config set``; print the config payload."""
    if args.config_action == "set":
        set_config(args.data_dir, enabled=args.enabled, hub_url=args.hub, machine_name=args.name)
    elif args.config_action != "show":
        raise AssertionError(f"unhandled config action {args.config_action!r}")
    print(json.dumps(sync_config.config_payload(args.data_dir), indent=2, sort_keys=True))


__all__ = ["add_config_parser", "run_config", "set_config"]
