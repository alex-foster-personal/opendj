"""Turn an Open DJ process ROLE into the PID the profilers should attach to.

Why this exists rather than a `pgrep` in each shell script: `pgrep -x
com.apple.WebKit.WebContent` returned TEN pids on the machine this was built on,
and most belong to something other than Open DJ -- while Open DJ's own cluster
can itself hold more than one WebContent pid (#705, a second WebContent launched
within the cluster window is kept, not refused). A profiler pointed at the wrong
WebContent produces a real, plausible, completely unrelated sample. So role
resolution reuses the diagnostics probe's own family association (shell by
app-bundle marker, engine by PPID ancestry, WebKit helpers by the first
WebContent launched within 45s of the shell) instead of re-deriving a weaker
rule here.

FILE REQUIREMENTS (mini-PRD)

* R1 a known role resolves to exactly the pid the probe would attribute to it.
  Status: OK, run, works as expected.
  - if two Open DJ builds are running then `--shell-pid` picks between them for
    the shell/engine roles unconditionally, and for the WebKit roles whenever
    their association is unambiguous
  - if two Open DJ builds started within the 45s WebKit association window
    then a WebKit role can come back "no live process fills role" even with
    `--shell-pid` pinned, because the pin disambiguates the shell but not
    which build's WebContent cluster a helper launched in that overlap
    belongs to; refusing beats guessing (Codex P1/BLOCKING, #705)
  - if two Open DJ builds are running and `--shell-pid` is NOT given then this
    exits non-zero naming both shell pids, rather than silently profiling
    whichever one has the lowest pid
  - if the app is not running then this exits non-zero saying so, and prints no
    pid, because a profiler must not be handed a stale number
  - if the role exists but no process currently fills it then it exits non-zero
* R2 an unknown role fails loudly and lists the roles that do exist.
  Status: OK, run, works as expected.
  - if a caller typos `python-engin` then nothing is sampled and the exit is 2

INTERPRETER FLOOR: runs under `/usr/bin/python3` (3.9 on current macOS) like the
diagnostics package it imports, so it works with no venv. Launch it with `-m`
from the repo root: `python3 -m scripts.perf.resolve_role python-engine`.
"""

from __future__ import annotations

import argparse
import sys

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.diagnostics.probe_process_family import (
    ProcessRow,
    associate_process_family,
    find_shell,
    process_table,
    shell_candidates,
)

# Roles a profiler can usefully attach to, in the order `all` walks them.
# These are the probe's own role names (probe_process_family.process_role), not
# a parallel vocabulary: one spelling across the probe, the register and here.
PROFILABLE_ROLES = (
    "desktop-shell",
    "python-engine",
    "engine-worker",
    "webkit-webcontent",
    "webkit-gpu",
    "webkit-networking",
)


class RoleUnresolved(RuntimeError):
    """No live process fills the requested role."""


def _reject_ambiguous_shell(rows: list[ProcessRow], shell_pid: int | None) -> None:
    """Refuse to silently pick between multiple running Open DJ shells.

    `find_shell(rows, None)` breaks ties by lowest pid, which is exactly how a
    profiler ends up attached to the WRONG build when two are running: the
    per-role pid COUNT downstream always reads as "1", regardless of which
    shell it came from, so nothing else catches this. Ambiguity must be
    rejected here, before a role is ever resolved.
    """

    if shell_pid is not None:
        return
    candidates = shell_candidates(rows)
    if len(candidates) > 1:
        pids = ", ".join(str(row.pid) for row in sorted(candidates, key=lambda row: row.pid))
        raise RoleUnresolved(
            f"{len(candidates)} Open DJ desktop shells are running (pids {pids}). "
            "Pass --shell-pid to pick one; resolving without a pin would "
            "silently profile whichever shell happens to have the lowest pid."
        )


def resolve_roles(shell_pid: int | None = None) -> dict[str, list[int]]:
    """Every profilable role that is live right now, with its pids."""

    rows = process_table()
    _reject_ambiguous_shell(rows, shell_pid)
    shell = find_shell(rows, shell_pid)
    if shell is None:
        raise RoleUnresolved(
            "no Open DJ desktop shell is running"
            + ("" if shell_pid is None else f" with pid {shell_pid}")
            + ". Start the app before profiling; a role cannot be resolved from "
            "a process that does not exist."
        )
    family, _association = associate_process_family(rows, shell, DarwinProcessMetrics())
    found: dict[str, list[int]] = {}
    for row, role in family:
        if role in PROFILABLE_ROLES:
            found.setdefault(role, []).append(row.pid)
    return found


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role",
        help=f"one of {', '.join(PROFILABLE_ROLES)}, or 'all' for every live role",
    )
    parser.add_argument(
        "--shell-pid",
        type=int,
        default=None,
        help="pin association to this Open DJ shell pid when two builds run",
    )
    parser.add_argument(
        "--format",
        choices=("pids", "table"),
        default="pids",
        help="'pids' prints one pid per line; 'table' prints 'role<TAB>pid'",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.role != "all" and args.role not in PROFILABLE_ROLES:
        print(
            f"[ERROR] unknown role {args.role!r}. Known roles: "
            f"{', '.join(PROFILABLE_ROLES)}, all",
            file=sys.stderr,
        )
        return 2
    try:
        live = resolve_roles(args.shell_pid)
    except RoleUnresolved as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    wanted = PROFILABLE_ROLES if args.role == "all" else (args.role,)
    emitted = 0
    for role in wanted:
        for pid in live.get(role, []):
            print(f"{role}\t{pid}" if args.format == "table" else pid)
            emitted += 1
    if emitted == 0:
        present = ", ".join(sorted(live)) or "none"
        print(
            f"[ERROR] no live process fills role {args.role!r}. "
            f"Roles present right now: {present}",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
