"""Run fleet-af's agent worktree tools against THIS checkout.

The worktree registry, reaper and creation guard (`just wt-*`) and the worker
`create` guard (`just worker-worktree`) moved to fleet-af `agents_worktree_tools/`
on Fri 2 Oct 2026: they are agent tooling, needed whatever repository the agents
work in. This module is the caller side, the way `scripts/dispatch_mcp.sh` is for
the dispatch MCP server.

    python -m scripts.worktree_tools lifecycle <guard|register|status|reap|restore> [args]
    python -m scripts.worktree_tools worker_guard create --repo R --target T --branch B --base O

fleet-af is found at $FLEET_AF_HOME, else the pinned, pull-only live clone
$HOME/.local/share/fleet-af-live. Never the shared dev checkout ~/code/fleet-af,
whose branch agents switch. When the tool is not there this exits 2 and names
the path; there is no fallback to another copy, because a reaper that ran some
other revision is worse than one that refused.

`lifecycle` always gets `--repo <this checkout>` right after its subcommand (the
tool used to default to its own checkout, and from fleet-af that would be the
wrong repository); a `--repo` the caller passes later still wins.
"""

from __future__ import annotations

import importlib
import os
import runpy
import sys
from pathlib import Path

CHECKOUT: Path = Path(__file__).resolve().parents[1]
PACKAGE: str = "agents_worktree_tools"
TOOLS: tuple[str, ...] = ("lifecycle", "worker_guard")
EXIT_UNAVAILABLE: int = 2


def fleet_af_home() -> Path:
    override = os.environ.get("FLEET_AF_HOME")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "fleet-af-live"


def tool_argv(tool: str, rest: list[str]) -> list[str]:
    """The argv the fleet-af tool sees: `lifecycle` gets this checkout as `--repo`."""
    if tool == "lifecycle" and rest:
        return [rest[0], "--repo", str(CHECKOUT), *rest[1:]]
    return list(rest)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in TOOLS:
        print(f"usage: python -m scripts.worktree_tools {{{'|'.join(TOOLS)}}} <args>",
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    tool, rest = args[0], args[1:]
    home = fleet_af_home()
    source = home / PACKAGE / f"{tool}.py"
    if not source.is_file():
        print(
            f"[ERROR] worktree tools: no fleet-af copy at {source}. Create the fleet-af live "
            "clone (bash <fleet-af>/deploy--live-clone/install.sh, or "
            "'just live-clone::install' in fleet-af) or set FLEET_AF_HOME.",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    # Appended, not prepended: fleet-af's root also holds a `scripts/` directory,
    # and this repo's `scripts` package must keep winning that name.
    sys.path.append(str(home))
    spec_origin = getattr(importlib.import_module(PACKAGE), "__file__", None) or ""
    if not Path(spec_origin).resolve().is_relative_to(home.resolve()):
        print(f"[ERROR] worktree tools: {PACKAGE} resolved to {spec_origin}, not under {home}",
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    sys.argv = [str(source), *tool_argv(tool, rest)]
    runpy.run_module(f"{PACKAGE}.{tool}", run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
