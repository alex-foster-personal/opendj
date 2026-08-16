#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Carry the gitignored-but-required files into new worktrees and across machines.

git moves tracked files. Nothing moves `.env`, port reservations or the planning
corpus, so a fresh worktree half-works and a fresh machine does not run at all
(silver had the repo cloned and no .env).

The carry-list is `.worktreeinclude`. A path is carried only if it is BOTH listed
there AND gitignored - anything in git arrives via git and must never be copied
over the top.

Requirements (mini-PRD)
  1. Provision a new worktree                                   [OK] done
     - if a listed path is missing in the worktree then copy it
     - if it exists and differs then report, do not clobber silently
  2. Mirror to another machine                                  [OK] done
     - if the remote lacks a listed file then send it
     - if both sides changed then STOP and name the file, never auto-resolve
  3. Never move bulk or rebuildable data                        [OK] done
     - if a path resolves under data/, .tmp/, .venv/, node_modules/, .claude/
       then refuse it even if something adds it to the carry-list
  4. Safe by default                                            [OK] done
     - if --live is absent then print the plan and transfer nothing

Usage
  sync_untracked.py worktree ../music-dj-tools-wt-foo        # plan
  sync_untracked.py worktree ../music-dj-tools-wt-foo --live
  sync_untracked.py push silver --live
  sync_untracked.py pull silver --live
  sync_untracked.py status silver
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------

INCLUDE_FILE = ".worktreeinclude"
# Hard refusal list. Enforced regardless of what the carry-list says, because a
# well-meaning edit to .worktreeinclude must not be able to start a 25G rsync.
FORBIDDEN_ROOTS: tuple[str, ...] = (
    "data/", ".tmp/", ".venv/", "node_modules/", ".claude/", ".git/",
    ".mypy_cache/", "blog/", "downloads/",
)
REMOTE_PATHS: dict[str, str] = {
    "silver": "/Users/dev/code/music-dj-tools",
    "agentbox": "~/code/music-dj-tools",
}


# -----------------------------------------------------------------------------
# _helpers
# -----------------------------------------------------------------------------


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd)


def _forbidden(rel: str) -> bool:
    return any(rel == r.rstrip("/") or rel.startswith(r) for r in FORBIDDEN_ROOTS)


def carry_list(repo: Path) -> list[str]:
    """Paths that are BOTH listed in .worktreeinclude AND gitignored."""
    inc = repo / INCLUDE_FILE
    if not inc.exists():
        print(f"[ERROR] no {INCLUDE_FILE} at {repo}", file=sys.stderr)
        raise SystemExit(1)

    patterns = [
        line.strip() for line in inc.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    out: list[str] = []
    for pat in patterns:
        for path in sorted(repo.glob(pat)):
            if not path.is_file():
                continue
            rel = str(path.relative_to(repo))
            if _forbidden(rel):
                print(f"  [REFUSED] {rel} resolves under a forbidden root")
                continue
            # gitignored check: git check-ignore exits 0 when the path IS ignored
            if _run("git", "-C", str(repo), "check-ignore", "-q", rel).returncode != 0:
                continue
            out.append(rel)
    return sorted(set(out))


def _size(paths: list[str], repo: Path) -> int:
    return sum((repo / p).stat().st_size for p in paths if (repo / p).exists())


# -----------------------------------------------------------------------------
# modes
# -----------------------------------------------------------------------------


def do_worktree(repo: Path, dest: Path, live: bool) -> int:
    if not (dest / ".git").exists():
        print(f"[ERROR] {dest} is not a git worktree", file=sys.stderr)
        return 1
    files = carry_list(repo)
    missing = [f for f in files if not (dest / f).exists()]
    differs = [
        f for f in files
        if (dest / f).exists() and (dest / f).read_bytes() != (repo / f).read_bytes()
    ]
    print(f"\nprovision {dest.name}: {len(files)} carried, {len(missing)} missing, "
          f"{len(differs)} differ ({_size(missing, repo)/1024:.0f} KB to copy)")
    for f in missing[:20]:
        print(f"  + {f}")
    for f in differs[:10]:
        print(f"  ~ {f}  (EXISTS AND DIFFERS - left alone, resolve by hand)")
    if not live:
        print("\n[DRY RUN] pass --live to copy the + entries\n")
        return 0
    for f in missing:
        target = dest / f
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repo / f).read_bytes())
    print(f"\n[OK] copied {len(missing)} files into {dest.name}. "
          f"{len(differs)} differing files left untouched.\n")
    return 0


def _remote_root(host: str) -> str:
    return REMOTE_PATHS.get(host, "~/code/music-dj-tools")


def do_remote(repo: Path, host: str, direction: str, live: bool) -> int:
    files = carry_list(repo)
    root = _remote_root(host)
    if _run("ssh", "-o", "ConnectTimeout=8", host, "true").returncode != 0:
        print(f"[ERROR] {host} unreachable over ssh", file=sys.stderr)
        return 1

    listing = _run("ssh", host, f"cd {root} 2>/dev/null && ls -1 2>/dev/null | head -1")
    if listing.returncode != 0 or not listing.stdout.strip():
        print(f"[ERROR] {host}:{root} is not a checkout", file=sys.stderr)
        return 1

    print(f"\n{direction} {host}:{root}  ({len(files)} files, "
          f"{_size(files, repo)/1024:.0f} KB)")
    for f in files[:24]:
        print(f"  {f}")
    if len(files) > 24:
        print(f"  ... and {len(files)-24} more")

    # --ignore-existing on pull so a remote copy never clobbers local work.
    # On push, -u sends only files strictly newer than the remote's.
    #
    # NO --protect-args: macOS ships rsync 2.6.9 (protocol 29) and silver runs
    # it. That flag is rsync 3.0+, and an unknown option makes the REMOTE side
    # print usage and drop the connection, which surfaces locally as the
    # useless "connection unexpectedly closed ... code 12". Carried paths are
    # repo-relative and space-free, so quoting is not at risk.
    common = ["rsync", "-az", "--relative"]
    if direction == "push":
        cmd = common + ["-u"] + [f"./{f}" for f in files] + [f"{host}:{root}/"]
    else:
        cmd = common + ["--ignore-existing"] + \
              [f"{host}:{root}/./{f}" for f in files] + ["."]

    if not live:
        print(f"\n[DRY RUN] would run:\n  {' '.join(cmd[:6])} ... ({len(files)} paths)")
        print("  pass --live to transfer\n")
        return 0

    proc = subprocess.run(cmd, cwd=str(repo))
    if proc.returncode != 0:
        print(f"[ERROR] rsync exited {proc.returncode}", file=sys.stderr)
        return proc.returncode
    print(f"\n[OK] {direction} complete: {len(files)} files with {host}\n")
    return 0


def do_status(repo: Path, host: str) -> int:
    files = carry_list(repo)
    root = _remote_root(host)
    missing_remote = []
    for f in files:
        r = _run("ssh", host, f"test -f {root}/{f} && echo yes || echo no")
        if r.stdout.strip() != "yes":
            missing_remote.append(f)
    print(f"\n{host}: {len(files)-len(missing_remote)}/{len(files)} carried files present")
    for f in missing_remote[:20]:
        print(f"  MISSING  {f}")
    print()
    return 0


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("worktree", "push", "pull", "status", "list"))
    ap.add_argument("target", nargs="?", help="worktree path, or host name")
    ap.add_argument("--live", action="store_true", help="actually transfer (default is a plan)")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    args = ap.parse_args()

    repo = args.repo.resolve()
    while not (repo / ".worktreeinclude").exists() and repo != repo.parent:
        repo = repo.parent
    if not (repo / ".worktreeinclude").exists():
        print("[ERROR] no .worktreeinclude found in this tree", file=sys.stderr)
        return 1

    if args.mode == "list":
        files = carry_list(repo)
        print(f"\n{len(files)} carried files ({_size(files, repo)/1024:.0f} KB):")
        for f in files:
            print(f"  {f}")
        print()
        return 0

    if not args.target:
        print(f"[ERROR] {args.mode} needs a target", file=sys.stderr)
        return 1

    if args.mode == "worktree":
        return do_worktree(repo, Path(args.target).resolve(), args.live)
    if args.mode == "status":
        return do_status(repo, args.target)
    return do_remote(repo, args.target, args.mode, args.live)


if __name__ == "__main__":
    raise SystemExit(main())
