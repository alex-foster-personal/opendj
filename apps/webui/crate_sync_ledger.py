"""Owner manifest, ledger audit, reconcile and push guards for crate sync.

Split out of :mod:`apps.webui.crate_sync` (issue #9687); ``crate_sync``
re-exports every name here.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from apps.shared import library_mode
from apps.shared.crate_index import audit_manifest, ledger_digest
from apps.webui.crate_sync_model import REMOTE_REPO, DestKind, SyncPlan, plan_bytes
from apps.webui.crate_sync_owner import _owner_repo_default, ssh_owner_argv
from apps.webui.crate_sync_paths import _source_group
from apps.webui.run_agentbox import allowed_ssh_host, allowed_ssh_hosts_description, ssh_agentbox_argv


def _manifest_files(manifest: dict[str, object]) -> list[dict[str, object]]:
    raw = manifest.get("files")
    if not isinstance(raw, list):
        raise TypeError("owner manifest files must be a list")
    entries: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("owner manifest file entry must be an object")
        required = {
            "bytes",
            "dest",
            "dest_root",
            "kind",
            "mtime_s",
            "relative",
            "source",
            "source_root",
            "stable_ids",
        }
        missing = required - item.keys()
        if missing:
            raise RuntimeError("owner manifest file entry lacks " + ", ".join(sorted(missing)))
        entries.append(item)
    return entries


def owner_manifest(*, owner: str, owner_repo: Path, forwarded_args: Sequence[str]) -> dict[str, object]:
    expected = _owner_repo_default()
    if owner_repo != expected:
        raise RuntimeError(f"refusing owner repo {owner_repo}; expected {expected}")
    command = shlex.join(["mdt-crate-plan", *forwarded_args])
    result = subprocess.run(
        ssh_owner_argv(command, owner=owner),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"owner plan failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"owner plan returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("owner plan JSON must be an object")
    _manifest_files(payload)
    if "library" in payload:
        from apps.shared.crate_state import normalise_manifest

        normalise_manifest(payload)
    return payload


def audit_payload(manifest: dict[str, object], *, crate_root: Path, state_db: Path | None = None) -> dict[str, object]:
    audit = audit_manifest(manifest, crate_root=crate_root)
    payload = audit.as_dict()
    payload["source_host"] = manifest.get("source_host")
    payload["scope"] = manifest.get("scope")
    payload["track_count"] = len(manifest.get("track_files", {}))
    if "library" in manifest:
        if state_db is None:
            raise RuntimeError("library ledger audit requires state.db")
        from apps.shared.crate_state import audit_snapshot, snapshot_from_manifest

        library = audit_snapshot(state_db, snapshot_from_manifest(manifest))
        payload["library"] = library
        payload["ok"] = payload["ok"] is True and library["ok"] is True
    return payload


def assert_audit_ok(payload: dict[str, object], *, label: str) -> None:
    if payload.get("ok") is True:
        return
    raise RuntimeError(
        f"{label} ledger mismatch: "
        f"missing={len(payload.get('missing', []))}, "
        f"size={len(payload.get('size_mismatches', []))}, "
        f"mtime={len(payload.get('mtime_mismatches', []))}"
        + (
            ", library=false"
            if isinstance(payload.get("library"), dict) and payload["library"].get("ok") is not True
            else ""
        )
    )


def reconcile_library_manifest(manifest: dict[str, object], *, state_db: Path, crate_root: Path) -> dict[str, object]:
    """Apply owner records only on the explicit remote replica."""
    if library_mode.library_mode() != "remote":
        raise RuntimeError("crate library reconciliation requires MDT_LIBRARY_MODE=remote")
    from apps.shared.crate_state import (
        normalise_manifest,
        reconcile_snapshot,
        snapshot_from_manifest,
    )

    normalise_manifest(manifest)
    snapshot = snapshot_from_manifest(manifest)
    return reconcile_snapshot(
        state_db,
        snapshot,
        backup_dir=crate_root / "state-backups",
    )


def remote_reconcile(*, dest_host: str, crate_root: Path) -> dict[str, object]:
    command = f"cd {shlex.quote(str(REMOTE_REPO))} && exec " + shlex.join(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.webui.crate_sync",
            "--reconcile-state",
            "--remote",
            "--json",
            "--crate-root",
            str(crate_root),
        ]
    )
    result = subprocess.run(
        ssh_agentbox_argv(command, ssh_host=dest_host),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"replica state reconciliation failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"replica state reconciliation returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("replica state reconciliation JSON must be an object")
    return payload


def manifest_payload(
    plan: SyncPlan,
    *,
    source_host: str,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
    spike_verified: bool,
    library_snapshot: dict[str, object],
) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for item in plan.files:
        source_root, dest_root, relative = _source_group(
            item,
            crate_root=crate_root,
            user_maps=user_maps,
        )
        files.append(
            {
                "bytes": item.size_bytes,
                "dest": str(item.dest),
                "dest_root": str(dest_root),
                "kind": item.kind,
                "mtime_s": item.mtime_s,
                "relative": relative.as_posix(),
                "source": str(item.source),
                "source_root": str(source_root),
                "stable_ids": list(item.stable_ids),
            }
        )
    track_files: dict[str, str] = {}
    for item in plan.files:
        if item.kind != "audio":
            continue
        for stable_id in item.stable_ids:
            prior = track_files.setdefault(stable_id, str(item.dest))
            if prior != str(item.dest):
                raise RuntimeError(f"stable_id {stable_id} maps to two crate audio files: {prior} and {item.dest}")
    from apps.shared.crate_state import snapshot_digest

    return {
        "bytes": plan_bytes(plan),
        "count": len(plan.files),
        "crate_root": str(crate_root),
        "files": files,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ledger_digest": ledger_digest(files),
        "library": library_snapshot,
        "library_digest": snapshot_digest(library_snapshot),
        "schema_version": 3,
        "scope": plan.scope,
        "skipped_absent": plan.skipped_absent,
        "skipped_streaming": plan.skipped_streaming,
        "source_host": source_host,
        "spike_verified": spike_verified,
        "track_files": track_files,
    }


def load_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"manifest {path} must be a JSON object")
    return raw


def load_destination_manifest(*, dest_kind: DestKind, dest_host: str, manifest_path: Path) -> dict[str, object]:
    if dest_kind == "local":
        return load_manifest(manifest_path)
    quoted = shlex.quote(manifest_path.as_posix())
    command = f"test ! -f {quoted} || cat -- {quoted}"
    completed = subprocess.run(
        ssh_agentbox_argv(command, ssh_host=dest_host),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"could not read destination manifest: {detail}")
    if not completed.stdout.strip():
        return {}
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"destination manifest {manifest_path} is malformed: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"destination manifest {manifest_path} must be a JSON object")
    return raw


def spike_verified(manifest: dict[str, object]) -> bool:
    return bool(manifest.get("spike_verified"))


def assert_full_allowed(*, filtered: bool, spike_ok: bool) -> None:
    if filtered or spike_ok:
        return
    raise RuntimeError(
        "full crate push refused until a spike verify is recorded in "
        "manifest.json (run --preload1 --live, then --verify-spike)"
    )


def assert_push_host(*, dest_kind: DestKind, dest_host: str, to_explicit: bool) -> None:
    """Live copy runs on the Air. The box is the replica, never the source."""
    if dest_kind == "local":
        return
    if sys.platform == "darwin":
        # Exact membership of the configured allowlist, and nothing else. This
        # destination goes to mkdir/rsync directly, without the remote-hostname
        # check run_via_ssh performs, so the allowlist is the only thing between
        # a typo (or a hostile argument) and a library copied off this machine.
        if not allowed_ssh_host(dest_host):
            raise RuntimeError(f"refusing dest host {dest_host!r}; allowed {allowed_ssh_hosts_description()}")
        return
    if to_explicit:
        raise RuntimeError(
            f"crate push copies FROM the Air (darwin). This host is {socket.gethostname()} / {sys.platform}."
        )
    raise RuntimeError(
        "crate push copies FROM the Air (darwin). This host is "
        f"{socket.gethostname()} / {sys.platform}. Status is the box-side command."
    )
