"""Process-family association rules shared by the live walk and probe.

One file, two import paths: ``apps/shared/process_family_rule.py`` and
``scripts/diagnostics/process_family_rule.py`` must stay byte-identical;
edit ``apps/shared/process_family_rule.py`` then run::

    cp apps/shared/process_family_rule.py scripts/diagnostics/process_family_rule.py

without retyping.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Protocol, TypeVar

_OPENDJ_NAME_RE = re.compile(r"opendj-[a-z0-9.-]+")

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
WEBKIT_AND_SHELL_ROLES = frozenset({"desktop-shell", "webkit-other", *WEBKIT_ROLES.values()})

ELECTRON_RENDERER_ROLE = "electron-renderer"
ELECTRON_GPU_ROLE = "electron-gpu"
ELECTRON_UTILITY_ROLE = "electron-utility"
FFMPEG_ROLE = "ffmpeg"
ANALYSIS_LANE_ROLE = "analysis-lane"
NAMED_CHILD_ROLES = (
    ELECTRON_RENDERER_ROLE,
    ELECTRON_GPU_ROLE,
    ELECTRON_UTILITY_ROLE,
    FFMPEG_ROLE,
    ANALYSIS_LANE_ROLE,
)
PROCESS_ROLES = (
    "desktop-shell",
    *WEBKIT_ROLES.values(),
    "webkit-other",
    "python-engine",
    *NAMED_CHILD_ROLES,
    "engine-worker",
    "shell-descendant",
    "other",
)

_ELECTRON_HELPER_RE = re.compile(r"/Open DJ[^/]*? Helper(?: \((Renderer|GPU|Plugin)\))?\.app/Contents/MacOS/")
_ELECTRON_HELPER_ROLES = {
    "Renderer": ELECTRON_RENDERER_ROLE,
    "GPU": ELECTRON_GPU_ROLE,
    "Plugin": ELECTRON_UTILITY_ROLE,
    None: ELECTRON_UTILITY_ROLE,
}
_FFMPEG_EXECUTABLE_RE = re.compile(
    r"^(?:/usr/sbin/taskpolicy -b |(?:/usr/bin/)?nice -n -?\d+ )?(?:(?:(?! -).)*/)?ffmpeg(?= |$)"
)
_ANALYSIS_LANE_RE = re.compile(
    r"^(?:(?:(?! -).)*/)?python[0-9.]*(?: -[A-Za-z]+(?: \S+)?)*? -m "
    r"(?:apps\.analysis\.queue_cli|apps\.analysis\.run|apps\.webui\.server\.backfill_drain_job)(?= |$)"
)


class FamilyRow(Protocol):
    """Duck row: pid, ppid, command (ProcessRow, ProcessInfo, ...)."""

    @property
    def pid(self) -> int:
        ...

    @property
    def ppid(self) -> int:
        ...

    @property
    def command(self) -> str:
        ...


RowT = TypeVar("RowT", bound=FamilyRow)


def named_child_role(command: str) -> str | None:
    """The role a command names by its executable or module, else None."""

    helper = _ELECTRON_HELPER_RE.search(command)
    if helper is not None:
        return _ELECTRON_HELPER_ROLES[helper.group(1)]
    if _FFMPEG_EXECUTABLE_RE.search(command):
        return FFMPEG_ROLE
    if _ANALYSIS_LANE_RE.search(command):
        return ANALYSIS_LANE_ROLE
    return None


def opendj_process_name(command: str) -> str:
    """Return the role-named child, the opendj-* setproctitle token, or ``unnamed``."""

    if "/MacOS/opendj-desktop" in command:
        return "opendj-desktop"
    named = named_child_role(command)
    if named is not None:
        return named
    match = _OPENDJ_NAME_RE.search(command)
    if match is not None:
        return match.group(0)
    return "unnamed"


def _is_shell(row: FamilyRow) -> bool:
    return SHELL_MARKER in row.command


def shell_candidates(rows: Iterable[RowT]) -> list[RowT]:
    """Every live process that looks like an Open DJ desktop shell."""

    return [row for row in rows if _is_shell(row)]


def find_shell(rows: Iterable[RowT], requested_pid: int | None) -> RowT | None:
    candidates = shell_candidates(rows)
    if requested_pid is not None:
        return next((row for row in candidates if row.pid == requested_pid), None)
    return min(candidates, key=lambda row: row.pid) if candidates else None


def descendants(rows: Iterable[RowT], root_pid: int) -> set[int]:
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


def process_role(row: RowT, shell_pid: int, descendant_pids: set[int]) -> str:
    if row.pid == shell_pid:
        return "desktop-shell"
    role = webkit_role(row.command)
    if role is not None:
        return role
    if row.pid in descendant_pids:
        if "apps.engine_core serve" in row.command:
            return "python-engine"
        named = named_child_role(row.command)
        if named is not None:
            return named
        if "worker" in row.command or "agent" in row.command:
            return "engine-worker"
        return "shell-descendant"
    return "other"


def _other_shell_starts(
    rows: list[RowT], start_seconds: Callable[[int], float | None], shell: RowT
) -> list[float]:
    """Start times of every OTHER live Open DJ shell, for the contest check below."""

    starts: list[float] = []
    for other in shell_candidates(rows):
        if other.pid == shell.pid:
            continue
        st = start_seconds(other.pid)
        if st is None:
            continue
        starts.append(st)
    return starts


def _webkit_candidates(
    rows: list[RowT], start_seconds: Callable[[int], float | None], shell_start: float
) -> list[tuple[RowT, str, float]]:
    """WebKit helpers whose start time falls inside the association window."""

    candidates: list[tuple[RowT, str, float]] = []
    for row in rows:
        role = webkit_role(row.command)
        if role is None:
            continue
        start = start_seconds(row.pid)
        if start is None:
            continue
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

    return any(0 <= start - other_start <= WEBKIT_ASSOCIATION_WINDOW_SECONDS for other_start in other_shell_starts)


def _webkit_cluster(
    candidates: list[tuple[RowT, str, float]],
) -> tuple[float | None, dict[int, tuple[RowT, str, float]]]:
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


def associate_family(
    rows: list[RowT],
    shell: RowT,
    start_seconds: Callable[[int], float | None],
) -> tuple[list[tuple[RowT, str]], dict[str, Any]]:
    """Associate shell, PPID descendants, and WebKit cluster (same rule as probe)."""

    descendant_pids = descendants(rows, shell.pid)
    selected: dict[int, tuple[RowT, str]] = {shell.pid: (shell, "desktop-shell")}
    for row in rows:
        if row.pid in descendant_pids:
            selected[row.pid] = (row, process_role(row, shell.pid, descendant_pids))

    shell_start = start_seconds(shell.pid)
    webkit_refusal: str | None = None
    if shell_start is None:
        association = {
            "shell_rule": "command contains an Open DJ app-bundle marker",
            "descendant_rule": "recursive PPID ancestry from desktop shell",
            "webkit_rule": (
                "first WebContent launched 0..45s after shell; include WebKit helpers "
                "within 3s of that WebContent; refused instead when another live Open "
                "DJ shell's own window could equally claim that WebContent, or when a "
                "second, distinct WebContent cluster sits in the same window"
            ),
            "webkit_cluster_found": False,
            "webkit_cluster_ambiguous": False,
            "webkit_refusal": None,
            "included_pids": sorted(selected),
        }
        return [selected[pid] for pid in sorted(selected)], association

    other_starts = _other_shell_starts(rows, start_seconds, shell)
    candidates = _webkit_candidates(rows, start_seconds, shell_start)
    cluster_start, cluster = _webkit_cluster(candidates)
    contested = cluster_start is not None and any(
        _is_contested(start, other_starts) for _row, _role, start in cluster.values()
    )
    multiple_clusters = cluster_start is not None and any(
        role == "webkit-webcontent" and abs(start - cluster_start) > WEBKIT_CLUSTER_WINDOW_SECONDS
        for _row, role, start in candidates
    )
    ambiguous = contested or multiple_clusters
    if ambiguous:
        webkit_refusal = "contested" if contested else "second_cluster"
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
        "webkit_refusal": webkit_refusal,
        "included_pids": sorted(selected),
    }
    return [selected[pid] for pid in sorted(selected)], association
