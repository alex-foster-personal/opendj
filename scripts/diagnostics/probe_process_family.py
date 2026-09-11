"""Which live processes belong to one OpenDJ app, and by what stated rule.

The association is explicit and inspectable rather than guessed: the Python
engine and its workers are PPID descendants of the Rust shell, and the WebKit
helpers are the first helper cluster launched shortly after that shell. Every
sample records the rule it used and the PIDs it accepted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .probe_native_metrics import DarwinProcessMetrics
from .probe_types import ProcessRow, run_text

WEBKIT_ASSOCIATION_WINDOW_SECONDS = 45.0
WEBKIT_CLUSTER_WINDOW_SECONDS = 3.0

SHELL_MARKER = "/MacOS/opendj-desktop"
"""The shell EXECUTABLE's path within the bundle, and only that.

A bare bundle-root marker like `/Applications/Open DJ` matches every process
launched from inside the bundle, not just the shell -- the packaged engine's
own command is `.../Open DJ.app/Contents/Resources/payload/runtime/bin/
python3 -m apps.engine_core serve`, which contains that root as a plain
substring. `shell_candidates()` (added in 5fd736cd to reject ambiguity when
two builds run) then counted the engine as a second candidate shell on every
SINGLE build, permanently tripping the ambiguity guard it was supposed to
gate on rarity (PR #705 review, resolve_role.py:76, fresh evidence after the
guard's own introduction). `Contents/MacOS/<executable>` is the one place in
a macOS app bundle only the main binary itself lives.
"""
WEBKIT_MARKER = "/WebKit.framework/"
WEBKIT_ROLES = {
    "com.apple.WebKit.WebContent": "webkit-webcontent",
    "com.apple.WebKit.GPU": "webkit-gpu",
    "com.apple.WebKit.Networking": "webkit-networking",
}
# Roles that are re-derived from scratch every sample. Only the roles OUTSIDE
# this set are remembered across samples, because those are the ones that can
# outlive their shell and turn into the orphans the probe is looking for.
WEBKIT_AND_SHELL_ROLES = frozenset(
    {"desktop-shell", "webkit-other", *WEBKIT_ROLES.values()}
)

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


def _is_shell(row: ProcessRow) -> bool:
    return SHELL_MARKER in row.command


def shell_candidates(rows: Iterable[ProcessRow]) -> list[ProcessRow]:
    """Every live process that looks like an Open DJ desktop shell.

    Exposed so a caller can detect AMBIGUITY (more than one candidate) before
    picking one, rather than only ever seeing whichever single row `find_shell`
    happened to choose.
    """

    return [row for row in rows if _is_shell(row)]


def find_shell(rows: Iterable[ProcessRow], requested_pid: int | None) -> ProcessRow | None:
    candidates = shell_candidates(rows)
    if requested_pid is not None:
        return next((row for row in candidates if row.pid == requested_pid), None)
    return min(candidates, key=lambda row: row.pid) if candidates else None


def descendants(rows: Iterable[ProcessRow], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    found: set[int] = set()
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, []))
    return found


def webkit_role(command: str) -> str | None:
    if WEBKIT_MARKER not in command:
        return None
    for marker, role in WEBKIT_ROLES.items():
        if marker in command:
            return role
    return "webkit-other"


def process_role(row: ProcessRow, shell_pid: int, descendant_pids: set[int]) -> str:
    if row.pid == shell_pid:
        return "desktop-shell"
    role = webkit_role(row.command)
    if role is not None:
        return role
    if row.pid in descendant_pids:
        if "apps.engine_core serve" in row.command:
            return "python-engine"
        if "worker" in row.command or "agent" in row.command:
            return "engine-worker"
        return "shell-descendant"
    return "other"


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
    live_shells = [row for row in rows if _is_shell(row)]
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
        row = by_pid.get(pid)
        if row is None or pid in selected_pids or row.command != command:
            continue
        found[pid] = {
            "pid": pid,
            "ppid": row.ppid,
            "reason": f"previously associated {role} is still alive outside the current family",
            "command": row.command,
        }
    return [found[pid] for pid in sorted(found)]


def _other_shell_starts(
    rows: list[ProcessRow], native: DarwinProcessMetrics, shell: ProcessRow
) -> list[float]:
    """Start times of every OTHER live Open DJ shell, for the contest check below."""

    starts: list[float] = []
    for other in shell_candidates(rows):
        if other.pid == shell.pid:
            continue
        try:
            usage = native.read(other.pid)
        except ProcessLookupError:
            continue
        starts.append(native.ticks_to_seconds(usage.proc_start_abstime))
    return starts


def _webkit_candidates(
    rows: list[ProcessRow], native: DarwinProcessMetrics, shell_start: float
) -> list[tuple[ProcessRow, str, float]]:
    """WebKit helpers whose start time falls inside the association window."""

    candidates: list[tuple[ProcessRow, str, float]] = []
    for row in rows:
        role = webkit_role(row.command)
        if role is None:
            continue
        try:
            usage = native.read(row.pid)
        except ProcessLookupError:
            continue
        start = native.ticks_to_seconds(usage.proc_start_abstime)
        if 0 <= start - shell_start <= WEBKIT_ASSOCIATION_WINDOW_SECONDS:
            candidates.append((row, role, start))
    return candidates


def _is_contested(start: float, other_shell_starts: list[float]) -> bool:
    """Would a DIFFERENT live shell's own association window also claim a
    helper that started at `start`?

    The window alone cannot tell which shell a helper belongs to: with two
    Open DJ builds running within `WEBKIT_ASSOCIATION_WINDOW_SECONDS` of each
    other, the same WebContent can sit inside BOTH shells' windows, and
    `_webkit_cluster`'s "first WebContent" rule then hands whichever shell
    asked the OTHER shell's cluster -- or merges both when their WebContents
    start within `WEBKIT_CLUSTER_WINDOW_SECONDS` of each other. `ProcessRow`
    carries no ancestry that ties a WebKit helper to its requesting shell (its
    ppid is launchd, and its command is the shared framework binary with no
    bundle reference -- the reason this module uses timing at all), so a
    contested candidate cannot be constrained to the right build; it can only
    be refused (Codex P1/BLOCKING, #705, resolve_role.py:98).
    """

    return any(
        0 <= start - other_start <= WEBKIT_ASSOCIATION_WINDOW_SECONDS
        for other_start in other_shell_starts
    )


def _webkit_cluster(
    candidates: list[tuple[ProcessRow, str, float]],
) -> tuple[float | None, dict[int, tuple[ProcessRow, str, float]]]:
    """The first WebContent plus every helper launched within the cluster window.

    Keeps each member's OWN start time rather than just `cluster_start`: a
    member can join the cluster window (`WEBKIT_CLUSTER_WINDOW_SECONDS` of the
    first WebContent) while itself falling inside a DIFFERENT shell's
    association window, and only checking `cluster_start` for a contest missed
    that member entirely (Codex P1/BLOCKING, #705, probe_process_family.py:306).
    """

    webcontent = [item for item in candidates if item[1] == "webkit-webcontent"]
    if not webcontent:
        return None, {}
    cluster_start = min(webcontent, key=lambda item: item[2])[2]
    return cluster_start, {
        row.pid: (row, role, start)
        for row, role, start in candidates
        if abs(start - cluster_start) <= WEBKIT_CLUSTER_WINDOW_SECONDS
    }


def associate_process_family(
    rows: list[ProcessRow],
    shell: ProcessRow,
    native: DarwinProcessMetrics,
) -> tuple[list[tuple[ProcessRow, str]], dict[str, Any]]:
    descendant_pids = descendants(rows, shell.pid)
    selected: dict[int, tuple[ProcessRow, str]] = {shell.pid: (shell, "desktop-shell")}
    for row in rows:
        if row.pid in descendant_pids:
            selected[row.pid] = (row, process_role(row, shell.pid, descendant_pids))

    shell_start = native.ticks_to_seconds(native.read(shell.pid).proc_start_abstime)
    other_starts = _other_shell_starts(rows, native, shell)
    candidates = _webkit_candidates(rows, native, shell_start)
    cluster_start, cluster = _webkit_cluster(candidates)
    contested = cluster_start is not None and any(
        _is_contested(start, other_starts) for _row, _role, start in cluster.values()
    )
    # A candidate WebContent outside the cluster window is a SECOND, distinct
    # cluster in the same 45s association window -- an unrelated WKWebView
    # (Safari, another Electron app), not just another Open DJ shell. Nothing
    # ties a WebKit helper back to its launching app, so a second cluster
    # makes "first WebContent" a guess between two apps, not one shell's
    # timing ambiguity (Codex P1/BLOCKING, #705, resolve_role.py:105).
    multiple_clusters = cluster_start is not None and any(
        role == "webkit-webcontent" and abs(start - cluster_start) > WEBKIT_CLUSTER_WINDOW_SECONDS
        for _row, role, start in candidates
    )
    # A SECOND webkit-webcontent candidate INSIDE the cluster window is not,
    # by itself, proof of a second app: the locked production capture at
    # tests/fixtures/perf/MANIFEST.json ("probe-samples.jsonl") shows one real
    # Open DJ family legitimately holding two webkit-webcontent PIDs in the
    # same cluster at once, so "one WKWebView" does not mean "one WebContent
    # process" and counting webcontent PIDs cannot stand in for counting
    # WKWebViews. ProcessRow carries no field that ties a WebKit helper back
    # to the app that launched it (ppid is launchd, command is the shared
    # framework binary -- see `_is_contested`), so there is no stronger
    # identity to distinguish a same-app second process from an unrelated
    # app's; only `contested` (another live shell could equally claim it) and
    # `multiple_clusters` (a genuinely separate cluster outside this window)
    # are signals this data can actually support (Codex P1/BLOCKING, #705,
    # probe_process_family.py:334).
    ambiguous = contested or multiple_clusters
    if ambiguous:
        cluster_start, cluster = None, {}
    selected.update({pid: (row, role) for pid, (row, role, _start) in cluster.items()})

    association = {
        "shell_rule": "command contains an Open DJ app-bundle marker",
        "descendant_rule": "recursive PPID ancestry from desktop shell",
        "webkit_rule": (
            "first WebContent launched 0..45s after shell; include WebKit helpers "
            "within 3s of that WebContent; refused instead when another live Open "
            "DJ shell's own window could equally claim that WebContent, or when a "
            "second, distinct WebContent cluster sits in the same window"
        ),
        "webkit_cluster_found": cluster_start is not None,
        "webkit_cluster_ambiguous": ambiguous,
        "included_pids": sorted(selected),
    }
    return [selected[pid] for pid in sorted(selected)], association
