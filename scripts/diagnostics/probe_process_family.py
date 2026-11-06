"""Which live processes belong to one OpenDJ app, and by what stated rule.

The association is explicit and inspectable rather than guessed: the Python
engine and its workers are PPID descendants of the Rust shell, and the WebKit
helpers are the first helper cluster launched shortly after that shell. Every
sample records the rule it used and the PIDs it accepted.

The rule implementation lives in ``process_family_rule`` (shared with the live
walk in ``apps.shared.machine_pressure``); this module adds ``ps`` process
tables and the Darwin native-metrics wrapper.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import process_family_rule as _pfr
from .probe_native_metrics import DarwinProcessMetrics
from .probe_types import ProcessRow, run_text

ANALYSIS_LANE_ROLE = _pfr.ANALYSIS_LANE_ROLE
ELECTRON_GPU_ROLE = _pfr.ELECTRON_GPU_ROLE
ELECTRON_RENDERER_ROLE = _pfr.ELECTRON_RENDERER_ROLE
ELECTRON_UTILITY_ROLE = _pfr.ELECTRON_UTILITY_ROLE
FFMPEG_ROLE = _pfr.FFMPEG_ROLE
NAMED_CHILD_ROLES = _pfr.NAMED_CHILD_ROLES
PROCESS_ROLES = _pfr.PROCESS_ROLES
WEBKIT_AND_SHELL_ROLES = _pfr.WEBKIT_AND_SHELL_ROLES
SHELL_MARKER = _pfr.SHELL_MARKER
associate_family = _pfr.associate_family
descendants = _pfr.descendants
find_shell = _pfr.find_shell
named_child_role = _pfr.named_child_role
opendj_process_name = _pfr.opendj_process_name
process_role = _pfr.process_role
shell_candidates = _pfr.shell_candidates
webkit_role = _pfr.webkit_role

BUILD_IDENTITY_FIELDS = frozenset(
    {
        "app_version",
        "built_at_utc",
        "bundle_identifier",
        "engine_version",
        "git_branch",
        "git_dirty",
        "git_sha",
        "git_sha_full",
        "lane_label",
        "product_name",
    }
)


def process_table() -> list[ProcessRow]:
    output = run_text(["ps", "-axo", "pid=,ppid=,pgid=,command="], timeout=5.0)
    rows: list[ProcessRow] = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", line)
        if not match:
            continue
        rows.append(
            ProcessRow(
                pid=int(match.group(1)),
                ppid=int(match.group(2)),
                pgid=int(match.group(3)),
                command=match.group(4),
            )
        )
    return rows


def _open_dj_bundle_root(command: str) -> str | None:
    start = command.find("/Applications/Open DJ")
    if start < 0:
        return None
    end = command.find(".app/", start)
    if end < 0:
        return None
    return command[start : end + len(".app/")]


def bundle_build_identity(shell: ProcessRow) -> dict[str, Any]:
    root = _open_dj_bundle_root(shell.command)
    if root is None:
        return {"available": False, "reason": "app bundle root not found"}
    manifest = Path(root) / "Contents/Resources/payload/manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"available": False, "reason": "payload manifest unavailable"}
    identity = value.get("identity") if isinstance(value, dict) else None
    if not isinstance(identity, dict):
        return {"available": False, "reason": "payload manifest has no identity"}
    return {
        "available": True,
        **{key: identity[key] for key in sorted(BUILD_IDENTITY_FIELDS) if key in identity},
    }


def suspected_orphans(
    rows: list[ProcessRow],
    selected_pids: set[int],
    known_descendants: dict[int, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Find same-bundle or formerly-associated children outside the family."""

    by_pid = {row.pid: row for row in rows}
    live_shells = [row for row in rows if _pfr._is_shell(row)]
    live_roots = {
        root: descendants(rows, shell.pid)
        for shell in live_shells
        if (root := _open_dj_bundle_root(shell.command)) is not None
    }
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.pid in selected_pids:
            continue
        root = _open_dj_bundle_root(row.command)
        if root is None or "/Contents/Resources/payload/" not in row.command:
            continue
        if row.pid not in live_roots.get(root, set()):
            found[row.pid] = {
                "pid": row.pid,
                "ppid": row.ppid,
                "reason": "same OpenDJ app payload is not a descendant of its live shell",
                "command": row.command,
            }
    for pid, (command, role) in known_descendants.items():
        known_row = by_pid.get(pid)
        if known_row is None or pid in selected_pids or known_row.command != command:
            continue
        found[pid] = {
            "pid": pid,
            "ppid": known_row.ppid,
            "reason": f"previously associated {role} is still alive outside the current family",
            "command": known_row.command,
        }
    return [found[pid] for pid in sorted(found)]


def associate_process_family(
    rows: list[ProcessRow],
    shell: ProcessRow,
    native: DarwinProcessMetrics,
) -> tuple[list[tuple[ProcessRow, str]], dict[str, Any]]:
    """Shell plus PPID descendants plus WebKit cluster; records the rule used."""

    native.read(shell.pid)

    def start_seconds(pid: int) -> float | None:
        try:
            return native.ticks_to_seconds(native.read(pid).proc_start_abstime)
        except ProcessLookupError:
            return None

    return associate_family(rows, shell, start_seconds)
