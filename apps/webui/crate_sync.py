"""Reconcile a present-library crate between the Mac owner and agentbox.

The owner database is never copied.  Instead the owner writes a stable-ID
ledger from its live database and files, while the replica independently stats
the declared destinations and must reproduce the same digest.  Rsync remains
the transfer engine, so files whose size and mtime already agree are not sent
again.

This module is the CLI facade and plan collector; the model, state.db
readers, owner SSH config, copy lanes and ledger live in sibling
``crate_sync_*`` modules (issue #9687) and are re-exported here.

Usage:
  python -m apps.webui.crate_sync --dry-run --preload1
  python -m apps.webui.crate_sync --live --local --preload1
  python -m apps.webui.crate_sync --live --remote --preload1
  python -m apps.webui.crate_sync --audit --remote
  python -m apps.webui.crate_sync --status
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from apps.shared import fs_residency, library_mode, platform_paths
from apps.shared.platform_paths import PROJECT_ROOT  # noqa: F401
from apps.webui.crate_sync_db import (  # noqa: F401
    _has_membership_order_key,
    _open_ro,
    _playlist_members_sql,
    _playlist_stable_ids,
)
from apps.webui.crate_sync_ledger import (  # noqa: F401
    _manifest_files,
    assert_audit_ok,
    assert_full_allowed,
    assert_push_host,
    audit_payload,
    load_destination_manifest,
    load_manifest,
    manifest_payload,
    owner_manifest,
    reconcile_library_manifest,
    remote_reconcile,
    spike_verified,
)
from apps.webui.crate_sync_model import (  # noqa: F401
    ANLZ_SIBLING_SUFFIXES,
    ARTWORK_SIBLINGS,
    DEFAULT_CRATE_ROOT,
    OWNER_REPO_ENV,
    OWNER_SSH_KEY_ENV,
    OWNER_SSH_TARGETS_ENV,
    PRELOAD1_PRESET,
    REMOTE_REPO,
    STREAMING_PREFIXES,
    CrateFile,
    DestKind,
    FileKind,
    SyncPlan,
    plan_bytes,
    preload1_stable_ids,
    write_json,
)
from apps.webui.crate_sync_owner import (  # noqa: F401
    _owner_repo_default,
    _owner_ssh_default,
    _owner_ssh_targets,
    _require_owner_ssh_key,
    allowed_owner_ssh,
    owner_ssh_key_path,
    ssh_owner_argv,
)

# crate_dest, default_user_maps and path_map_document are re-exported:
# crate_sync_cli and the tests reach them as crate_sync.<name>.
from apps.webui.crate_sync_paths import (  # noqa: F401
    DEFAULT_USER_PREFIXES_ENV,
    _source_group,
    crate_dest,
    default_user_maps,
    parse_map_entry,
    path_map_document,
)
from apps.webui.crate_sync_transfer import (  # noqa: F401
    CrateDestinationEscape,
    _assert_within_crate,
    _changed_file_count,
    _copy_local,
    _rsync_manifest_from_host,
    _rsync_plan_to_host,
    _rsync_to_host,
    apply_plan,
)
from apps.webui.run_agentbox import agentbox_host
from apps.webui.soft_deletes import has_soft_deletes


def _selected_stable_ids(
    state: sqlite3.Connection,
    *,
    playlist: str | None,
    stable_ids: Sequence[str] | None,
    preload1: bool,
) -> tuple[str, ...] | None:
    if preload1:
        return preload1_stable_ids()
    if stable_ids is not None:
        return tuple(stable_ids)
    if playlist is not None:
        return _playlist_stable_ids(state, playlist)
    return None


def _scope_name(
    *, playlist: str | None, stable_ids: Sequence[str] | None, preload1: bool
) -> str:
    if preload1:
        return "preload1"
    if playlist is not None:
        return f"playlist:{playlist}"
    if stable_ids is not None:
        return "stable-ids"
    return "present"


def _is_streaming(path: str | None) -> bool:
    return platform_paths.is_streaming_uri(path)


def _materialised_source(raw: str | None) -> Path | None:
    if not raw or _is_streaming(raw):
        return None
    path = Path(raw)
    if fs_residency.is_materialised(path):
        return path
    if raw.startswith("/PIONEER/"):
        share_path = platform_paths.SHARE_ROOT / raw.lstrip("/")
        if fs_residency.is_materialised(share_path):
            return share_path
    return None


def _anlz_sources(analysis_data_path: str | None) -> list[Path]:
    source = _materialised_source(analysis_data_path)
    if source is None:
        return []
    return sorted(
        (
            sibling
            for sibling in source.parent.iterdir()
            if sibling.stem == source.stem
            and sibling.suffix.upper() in ANLZ_SIBLING_SUFFIXES
            and fs_residency.is_materialised(sibling)
        ),
        key=lambda path: path.name,
    )


def _artwork_sources(image_path: str | None) -> list[Path]:
    source = _materialised_source(image_path)
    if source is None:
        return []
    files = [source]
    for name in ARTWORK_SIBLINGS:
        sibling = source.with_name(name)
        if sibling != source and fs_residency.is_materialised(sibling):
            files.append(sibling)
    return files


def collect_plan(
    *,
    state_db: Path,
    master_db: Path | None,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    playlist: str | None = None,
    stable_ids: Sequence[str] | None = None,
    preload1: bool = False,
) -> SyncPlan:
    """Keep a row only when the owner-machine source file is present."""
    from apps.webui import crate_sync_plan as plan_mod

    state = _open_ro(state_db, "STATE_DB")
    try:
        wanted = _selected_stable_ids(
            state, playlist=playlist, stable_ids=stable_ids, preload1=preload1
        )
        rows = plan_mod.selected_track_rows(state, wanted)
        vendor_ids = {
            str(r["stable_id"]): str(r["vendor_id"])
            for r in state.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = 'rekordbox'"
            )
        }
    finally:
        state.close()

    rb_assets = plan_mod.load_rb_assets(master_db)
    files, skipped_streaming, skipped_absent = plan_mod.accumulate_plan_files(
        rows,
        vendor_ids,
        rb_assets,
        crate_root,
        user_maps,
    )
    ordered = tuple(sorted(files.values(), key=lambda item: item.dest.as_posix()))
    return SyncPlan(
        scope=_scope_name(playlist=playlist, stable_ids=stable_ids, preload1=preload1),
        files=ordered,
        skipped_streaming=skipped_streaming,
        skipped_absent=skipped_absent,
    )


# ----- status / verify ----------------------------------------------------


def status_report(
    *,
    state_db: Path,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    manifest_path: Path,
) -> dict[str, object]:
    mode = library_mode.library_mode()
    manifest = load_manifest(manifest_path)
    if not user_maps:
        raise RuntimeError(
            "status_report requires at least one user_maps entry to probe; "
            f"got none (check {DEFAULT_USER_PREFIXES_ENV} or pass --map)"
        )
    # Probes the FIRST configured prefix rather than a hardcoded placeholder
    # (#910): a literal example path can never equal whatever prefix this
    # deployment actually configured, so "mapped_sample" would report a
    # permanent, misleading non-match even when the real mapping works.
    sample_src = f"{user_maps[0][0]}/Music/track.mp3"
    mapped = platform_paths.resolve_library_path(
        sample_src,
        path_map=platform_paths.PathMap(entries=tuple(user_maps)),
    )
    resolved = 0
    total = 0
    if state_db.is_file():
        state = _open_ro(state_db, "STATE_DB")
        try:
            if has_soft_deletes(state, "tracks"):
                paths_sql = "SELECT file_path FROM tracks WHERE deleted_at IS NULL"
            else:
                paths_sql = "SELECT file_path FROM tracks"
            rows = state.execute(paths_sql).fetchall()
        finally:
            state.close()
        path_map = platform_paths.PathMap(entries=tuple(user_maps))
        for (file_path,) in rows:
            if not file_path or _is_streaming(file_path):
                continue
            total += 1
            result = platform_paths.resolve_asset_path(file_path, path_map=path_map)
            if result.resolved is not None and fs_residency.is_materialised(
                result.resolved
            ):
                resolved += 1
    return {
        "crate_root": str(crate_root),
        "crate_root_exists": crate_root.is_dir(),
        "file_paths_present": total,
        "file_paths_resolved": resolved,
        "library_mode": mode,
        "mapped_sample": None if mapped.resolved is None else str(mapped.resolved),
        "mapped_sample_reason": mapped.reason,
        "manifest_scope": manifest.get("scope"),
        "spike_verified": spike_verified(manifest),
    }


def verify_spike(
    *,
    stable_ids: Sequence[str],
    base_url: str,
) -> dict[str, object]:
    """GET audio + anlz for each spike id. Real HTTP, no mocked status."""
    performance_status = _http_status(f"{base_url.rstrip('/')}/performance/preload1")
    results: list[dict[str, object]] = []
    for stable_id in stable_ids:
        audio_url = f"{base_url.rstrip('/')}/api/v1/tracks/{stable_id}/audio"
        anlz_url = f"{base_url.rstrip('/')}/api/v1/tracks/{stable_id}/anlz?points=100"
        audio_status = _http_status(audio_url)
        anlz_status, anlz_body = _http_json(anlz_url)
        anlz_ok = anlz_status == 200 and "ANALYSIS_NOT_FOUND" not in anlz_body
        results.append(
            {
                "stable_id": stable_id,
                "audio_status": audio_status,
                "anlz_status": anlz_status,
                "anlz_ok": anlz_ok,
            }
        )
    all_ok = performance_status == 200 and all(
        item["audio_status"] == 200 and item["anlz_ok"] is True for item in results
    )
    return {
        "ok": all_ok,
        "performance_status": performance_status,
        "tracks": results,
    }


def _http_status(url: str) -> int:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"spike verify could not reach {url}: {exc}") from exc


def _http_json(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except urllib.error.URLError as exc:
        raise RuntimeError(f"spike verify could not reach {url}: {exc}") from exc


# ----- CLI ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    host = agentbox_host()
    parser = argparse.ArgumentParser(prog="python -m apps.webui.crate_sync")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="print count + bytes, copy nothing"
    )
    mode.add_argument(
        "--live", action="store_true", help="copy the selected present files"
    )
    mode.add_argument(
        "--status", action="store_true", help="box-side mode / crate / resolve counts"
    )
    mode.add_argument(
        "--audit",
        action="store_true",
        help="independently stat the replica and reconcile it with manifest.json",
    )
    mode.add_argument(
        "--verify-spike",
        action="store_true",
        help="GET audio+anlz for the spike set and record spike_verified",
    )
    mode.add_argument(
        "--reconcile-state",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    filt = parser.add_mutually_exclusive_group()
    filt.add_argument("--playlist", default=None, help="spike one playlist by name")
    filt.add_argument(
        "--stable-ids",
        default=None,
        help="comma-separated stable_id list",
    )
    filt.add_argument(
        "--preload1", action="store_true", help="spike the four preload1 deck ids"
    )
    side = parser.add_mutually_exclusive_group()
    side.add_argument(
        "--local",
        action="store_true",
        help=f"run on the owner Mac; live mode pushes to {host}",
    )
    side.add_argument(
        "--remote",
        action="store_true",
        help=f"run on {host}; dry/live modes obtain the owner plan over SSH and pull",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="select every currently-present row (refused until spike_verified)",
    )
    parser.add_argument(
        "--to", default=host, help="rsync destination host"
    )
    parser.add_argument(
        "--owner",
        default=None,
        help=(
            "fixed SSH owner used only by --remote pull mode; defaults to "
            f"the first entry of {OWNER_SSH_TARGETS_ENV}"
        ),
    )
    parser.add_argument(
        "--owner-repo",
        default=None,
        help=(
            "owner checkout used only by --remote pull mode; defaults to "
            f"{OWNER_REPO_ENV}"
        ),
    )
    parser.add_argument(
        "--dest",
        choices=("ssh", "local"),
        default=None,
        help=f"ssh (owner Mac -> {host}) or local copy into --crate-root (tests)",
    )
    parser.add_argument("--crate-root", default=None, help="replica crate root")
    parser.add_argument(
        "--data-dir", default=None, help="owner data dir (state.db + path-map.json)"
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        type=parse_map_entry,
        help=(
            "FROM=TO user prefix (repeatable). Default: derived from "
            f"{DEFAULT_USER_PREFIXES_ENV}"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:9400",
        help="Vite/Serve origin for --verify-spike",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable object instead of the human plan",
    )
    return parser


def _stable_ids_arg(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    ids = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not ids:
        raise RuntimeError("--stable-ids was set but named no id")
    return ids


def _owner_library_snapshot(
    state_db: Path,
    *,
    playlist: str | None,
    stable_ids: Sequence[str] | None,
    preload1: bool,
) -> dict[str, object]:
    from apps.shared.crate_state import build_snapshot

    selected = preload1_stable_ids() if preload1 else stable_ids
    return build_snapshot(
        state_db,
        playlist_name=playlist,
        stable_ids=selected,
    )


def _filtered(args: argparse.Namespace) -> bool:
    return bool(args.playlist or args.stable_ids or args.preload1)


def _operation_side(args: argparse.Namespace) -> Literal["local", "remote"]:
    if args.local:
        return "local"
    if args.remote:
        return "remote"
    return "remote" if library_mode.library_mode() == "remote" else "local"


def _forwarded_plan_args(
    args: argparse.Namespace,
    *,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
) -> list[str]:
    forwarded = ["--crate-root", str(crate_root)]
    for source, destination in user_maps:
        forwarded.extend(["--map", f"{source}={destination}"])
    if args.playlist:
        forwarded.extend(["--playlist", args.playlist])
    elif args.stable_ids:
        forwarded.extend(["--stable-ids", args.stable_ids])
    elif args.preload1:
        forwarded.append("--preload1")
    if args.full:
        forwarded.append("--full")
    return forwarded


def _print_manifest_plan(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    summary = {
        key: payload.get(key)
        for key in (
            "bytes",
            "count",
            "crate_root",
            "ledger_digest",
            "scope",
            "skipped_absent",
            "skipped_streaming",
        )
    }
    summary["track_count"] = len(payload.get("track_files", {}))
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    for item in _manifest_files(payload):
        sys.stdout.write(
            f"{item['kind']}\t{item['bytes']}\t{item['source']} -> {item['dest']}\n"
        )


def _run(argv: Sequence[str] | None = None) -> int:
    from apps.webui.crate_sync_cli import run_crate_sync

    return run_crate_sync(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(argv)
    except (RuntimeError, TypeError, ValueError) as exc:
        sys.stderr.write(f"crate-sync: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
