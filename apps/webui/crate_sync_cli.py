"""CLI dispatch extracted from :mod:`apps.webui.crate_sync`.

``crate_sync._run`` stays the argv entry and returns the same ints. Path
resolution and each status/plan/apply branch live here so ``_run`` stays
under the complexity ceiling.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.shared import library_mode, platform_paths
from apps.webui import crate_sync as cs


@dataclass(frozen=True)
class CratePaths:
    data_dir: Path
    crate_root: Path
    user_maps: Sequence[tuple[str, str]]
    dest_kind: cs.DestKind
    state_db: Path
    master_db: Path
    manifest_path: Path
    path_map_path: Path
    to_explicit: bool
    side: Literal["local", "remote"]


def resolve_crate_paths(
    args: argparse.Namespace, argv: Sequence[str] | None
) -> CratePaths:
    data_dir = Path(args.data_dir) if args.data_dir else platform_paths.DATA_DIR
    crate_root = (
        Path(args.crate_root)
        if args.crate_root
        else (
            library_mode.crate_root()
            if os.environ.get(library_mode.CRATE_ROOT_ENV, "").strip()
            else cs.DEFAULT_CRATE_ROOT
        )
    )
    if args.map:
        user_maps = tuple((str(src), str(dst)) for src, dst in args.map)
    else:
        user_maps = cs.default_user_maps(crate_root)
    dest_kind: cs.DestKind = "local" if args.dest == "local" else "ssh"
    return CratePaths(
        data_dir=data_dir,
        crate_root=crate_root,
        user_maps=user_maps,
        dest_kind=dest_kind,
        state_db=data_dir / "state" / "state.db",
        master_db=data_dir / "master.plain.db",
        manifest_path=crate_root / "manifest.json",
        path_map_path=data_dir / "path-map.json",
        to_explicit=any(
            a == "--to" or a.startswith("--to=") for a in (argv or sys.argv[1:])
        ),
        side=cs._operation_side(args),
    )


def _cmd_status(paths: CratePaths) -> int:
    report = cs.status_report(
        state_db=paths.state_db,
        crate_root=paths.crate_root,
        user_maps=paths.user_maps,
        manifest_path=paths.manifest_path,
    )
    report["operation_side"] = paths.side
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_audit(paths: CratePaths) -> int:
    manifest = cs.load_manifest(paths.manifest_path)
    if not manifest:
        raise RuntimeError(f"no crate manifest at {paths.manifest_path}")
    payload = cs.audit_payload(
        manifest,
        crate_root=paths.crate_root,
        state_db=paths.state_db,
    )
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if payload["ok"] is True else 1


def _cmd_reconcile(paths: CratePaths) -> int:
    if paths.side != "remote":
        raise RuntimeError("--reconcile-state is a remote-only operation")
    manifest = cs.load_manifest(paths.manifest_path)
    if not manifest:
        raise RuntimeError(f"no crate manifest at {paths.manifest_path}")
    state_reconciliation = cs.reconcile_library_manifest(
        manifest,
        state_db=paths.state_db,
        crate_root=paths.crate_root,
    )
    cs.write_json(paths.manifest_path, manifest)
    reconciliation = cs.audit_payload(
        manifest,
        crate_root=paths.crate_root,
        state_db=paths.state_db,
    )
    cs.assert_audit_ok(reconciliation, label="replica")
    result = {
        "state_reconciliation": state_reconciliation,
        "reconciliation": reconciliation,
    }
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_verify_spike(args: argparse.Namespace, paths: CratePaths) -> int:
    ids = cs._stable_ids_arg(args.stable_ids) or cs.preload1_stable_ids()
    result = cs.verify_spike(stable_ids=ids, base_url=args.base_url)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if not result["ok"]:
        return 1
    manifest = cs.load_manifest(paths.manifest_path)
    if not manifest:
        raise RuntimeError(
            f"no crate manifest at {paths.manifest_path}; run --live --preload1 first"
        )
    manifest["spike_verified"] = True
    cs.write_json(paths.manifest_path, manifest)
    return 0


def _cmd_remote(args: argparse.Namespace, paths: CratePaths) -> int:
    if library_mode.library_mode() != "remote":
        raise RuntimeError(
            "--remote pull requires MDT_LIBRARY_MODE=remote; refusing to "
            "write a replica into a local-library process"
        )
    owner = args.owner or cs._owner_ssh_default()
    owner_repo = (
        Path(args.owner_repo) if args.owner_repo else cs._owner_repo_default()
    )
    payload = cs.owner_manifest(
        owner=owner,
        owner_repo=owner_repo,
        forwarded_args=cs._forwarded_plan_args(
            args,
            crate_root=paths.crate_root,
            user_maps=paths.user_maps,
        ),
    )
    if str(payload.get("crate_root")) != str(paths.crate_root):
        raise RuntimeError(
            f"owner planned crate {payload.get('crate_root')}; expected {paths.crate_root}"
        )
    if args.dry_run:
        cs._print_manifest_plan(payload, as_json=args.json)
        return 0
    existing = cs.load_manifest(paths.manifest_path)
    cs.assert_full_allowed(
        filtered=cs._filtered(args),
        spike_ok=cs.spike_verified(existing),
    )
    payload["spike_verified"] = cs.spike_verified(existing)
    transferred = cs._rsync_manifest_from_host(
        payload,
        owner=owner,
        crate_root=paths.crate_root,
    )
    cs.write_json(paths.path_map_path, cs.path_map_document(paths.user_maps))
    state_reconciliation = cs.reconcile_library_manifest(
        payload,
        state_db=paths.state_db,
        crate_root=paths.crate_root,
    )
    cs.write_json(paths.manifest_path, payload)
    reconciliation = cs.audit_payload(
        payload,
        crate_root=paths.crate_root,
        state_db=paths.state_db,
    )
    cs.assert_audit_ok(reconciliation, label="replica")
    result = {
        "direction": "pull",
        "transferred_files": transferred,
        "state_reconciliation": state_reconciliation,
        "reconciliation": reconciliation,
    }
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_push(args: argparse.Namespace, paths: CratePaths) -> int:
    plan = cs.collect_plan(
        state_db=paths.state_db,
        master_db=paths.master_db if paths.master_db.is_file() else None,
        crate_root=paths.crate_root,
        user_maps=paths.user_maps,
        playlist=args.playlist,
        stable_ids=cs._stable_ids_arg(args.stable_ids),
        preload1=args.preload1,
    )
    payload = cs.manifest_payload(
        plan,
        source_host=socket.gethostname(),
        crate_root=paths.crate_root,
        user_maps=paths.user_maps,
        spike_verified=False,
        library_snapshot=cs._owner_library_snapshot(
            paths.state_db,
            playlist=args.playlist,
            stable_ids=cs._stable_ids_arg(args.stable_ids),
            preload1=args.preload1,
        ),
    )

    if args.dry_run:
        cs._print_manifest_plan(payload, as_json=args.json)
        return 0

    cs.assert_push_host(
        dest_kind=paths.dest_kind, dest_host=args.to, to_explicit=paths.to_explicit
    )
    existing = cs.load_destination_manifest(
        dest_kind=paths.dest_kind,
        dest_host=args.to,
        manifest_path=paths.manifest_path,
    )
    cs.assert_full_allowed(
        filtered=cs._filtered(args),
        spike_ok=cs.spike_verified(existing),
    )
    payload["spike_verified"] = cs.spike_verified(existing)
    transferred = cs.apply_plan(
        plan,
        dest_kind=paths.dest_kind,
        dest_host=args.to,
        crate_root=paths.crate_root,
        user_maps=paths.user_maps,
    )
    map_doc = cs.path_map_document(paths.user_maps)
    if paths.dest_kind == "local":
        cs.write_json(paths.path_map_path, map_doc)
        if library_mode.library_mode() == "remote":
            cs.reconcile_library_manifest(
                payload,
                state_db=paths.state_db,
                crate_root=paths.crate_root,
            )
        cs.write_json(paths.manifest_path, payload)
        reconciliation = cs.audit_payload(
            payload,
            crate_root=paths.crate_root,
            state_db=paths.state_db,
        )
        cs.assert_audit_ok(reconciliation, label="replica")
    else:
        with tempfile.TemporaryDirectory(prefix="mdt-crate-") as tmp:
            tmp_root = Path(tmp)
            tmp_map = tmp_root / "path-map.json"
            tmp_manifest = tmp_root / "manifest.json"
            cs.write_json(tmp_map, map_doc)
            cs.write_json(tmp_manifest, payload)
            cs._rsync_to_host(tmp_map, args.to, cs.REMOTE_REPO / "data" / "path-map.json")
            cs._rsync_to_host(tmp_manifest, args.to, paths.crate_root / "manifest.json")
        remote_result = cs.remote_reconcile(
            dest_host=args.to, crate_root=paths.crate_root
        )
        reconciliation = remote_result["reconciliation"]
    result = {
        "direction": "push",
        "transferred_files": transferred,
        "reconciliation": reconciliation,
    }
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def dispatch_crate_command(args: argparse.Namespace, paths: CratePaths) -> int:
    if args.status:
        return _cmd_status(paths)
    if args.audit:
        return _cmd_audit(paths)
    if args.reconcile_state:
        return _cmd_reconcile(paths)
    if args.verify_spike:
        return _cmd_verify_spike(args, paths)
    if args.full and cs._filtered(args):
        raise RuntimeError(
            "--full cannot combine with --playlist / --stable-ids / --preload1"
        )
    if paths.side == "remote":
        return _cmd_remote(args, paths)
    return _cmd_push(args, paths)


def run_crate_sync(argv: Sequence[str] | None = None) -> int:
    args = cs.build_parser().parse_args(argv)
    library_mode.apply_library_env()
    platform_paths.refresh_share_root()
    paths = resolve_crate_paths(args, argv)
    return dispatch_crate_command(args, paths)
