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

Remote sync is three-way, not timestamp-based. Each side's sha256 is compared
against the baseline recorded at the end of the last successful sync with that
host (`.git/sync_untracked_state.json`), so "who actually changed this?" is
answerable. rsync's own -u and --ignore-existing cannot answer it: -u compares
mtimes, which say nothing about whether the far side also moved, and
--ignore-existing refuses every existing file whether or not it is stale. Both
could report a clean sync having lost an edit.

Requirements (mini-PRD)
  1. Provision a new worktree                                   [OK] done
     - if a listed path is missing in the worktree then copy it
     - if it exists and differs then report, do not clobber silently
  2. Mirror to another machine                                  [OK] done
     - if the remote lacks a listed file then send it
     - if both sides changed then STOP and name the file, never auto-resolve
     - if only the far side changed then skip it and name the direction that
       would carry it, rather than reporting a sync that moved nothing
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
import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------

INCLUDE_FILE = ".worktreeinclude"
# Per-host record of the content hash both sides agreed on at the end of the
# last successful transfer. Lives in the git common dir so every worktree of
# this checkout shares one baseline, and so it is never itself carried.
STATE_FILE = "sync_untracked_state.json"

# classify() outcomes.
IN_SYNC = "in-sync"
SEND = "send"
BLOCKED = "blocked"
DIVERGED = "diverged"
# Hard refusal list. Enforced regardless of what the carry-list says, because a
# well-meaning edit to .worktreeinclude must not be able to start a 25G rsync.
FORBIDDEN_ROOTS: tuple[str, ...] = (
    "data/", ".tmp/", ".venv/", "node_modules/", ".claude/", ".git/",
    ".mypy_cache/", "blog/", "downloads/",
)
REMOTE_PATHS: dict[str, str] = {
    "agentbox": "~/code/music-dj-tools",
}
#: Env override for Silver's checkout path, resolved fresh (not cached at
#: import time) so a test can set/unset it per case. Resolved on the REMOTE
#: host, not locally -- "~/..." is portable there the same way it already is
#: for agentbox above. #910: no personal absolute path is hardcoded here;
#: unset falls back to the same portable form agentbox uses.
SILVER_CHECKOUT_ENV: str = "MDT_SILVER_CHECKOUT"


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
# _divergence: hash both sides, compare against the recorded baseline
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Hashes:
    """One carried file's sha256 on each side, plus the last agreed baseline.

    `None` means the file is absent there (local absence is impossible, since
    carry_list only yields files that exist locally). A `None` baseline means
    the two machines have never completed a sync of this path.
    """

    local: str | None
    remote: str | None
    baseline: str | None


@dataclass(frozen=True)
class Decision:
    rel: str
    action: str
    reason: str


@dataclass
class Plan:
    in_sync: list[Decision] = field(default_factory=list)
    send: list[Decision] = field(default_factory=list)
    blocked: list[Decision] = field(default_factory=list)
    diverged: list[Decision] = field(default_factory=list)


def _carried_by(direction: str, carrier: str, rel: str, send: str, block: str) -> Decision:
    """SEND when `direction` is the one that carries this change, else BLOCKED.

    Naming the direction that WOULD carry it is the point: a silent skip is how
    the old flags left a machine stale while reporting success.
    """
    if direction == carrier:
        return Decision(rel, SEND, send)
    return Decision(rel, BLOCKED, block)


def classify(direction: str, rel: str, h: Hashes) -> Decision:
    """Decide one file's fate. The whole conflict stop lives here.

    rsync's own -u / --ignore-existing cannot express this: -u compares mtimes,
    which say nothing about whether the other side also changed, and
    --ignore-existing refuses every existing file whether or not it is stale.
    """
    if h.local is None:
        raise ValueError(f"{rel}: no local copy; carry_list should not have yielded it")

    if h.remote is None:
        if h.baseline is not None:
            return Decision(rel, DIVERGED,
                            "deleted on the remote after the last recorded sync")
        return _carried_by(direction, "push", rel,
                           "remote has no copy yet",
                           "remote has no copy yet; run push instead")

    if h.local == h.remote:
        return Decision(rel, IN_SYNC, "identical on both sides")

    if h.baseline is None:
        return Decision(rel, DIVERGED,
                        "copies differ and no sync was ever recorded for this host")

    local_changed = h.local != h.baseline
    remote_changed = h.remote != h.baseline

    if local_changed and remote_changed:
        return Decision(rel, DIVERGED, "both sides changed since the last sync")
    if local_changed:
        return _carried_by(direction, "push", rel,
                           "only the local copy changed",
                           "only the local copy changed; pulling would lose it, run push")
    if remote_changed:
        return _carried_by(direction, "pull", rel,
                           "only the remote copy changed",
                           "only the remote copy changed; pushing would lose it, run pull")
    # Neither side moved off the baseline yet the hashes differ: unreachable,
    # and a silent guess here is exactly the bug this function exists to stop.
    raise AssertionError(f"{rel}: unclassifiable hashes {h}")


def plan_sync(direction: str, hashes: dict[str, Hashes]) -> Plan:
    plan = Plan()
    bucket = {IN_SYNC: plan.in_sync, SEND: plan.send,
              BLOCKED: plan.blocked, DIVERGED: plan.diverged}
    for rel, h in sorted(hashes.items()):
        d = classify(direction, rel, h)
        bucket[d.action].append(d)
    return plan


def updated_baselines(
    direction: str, plan: Plan, hashes: dict[str, Hashes], old: dict[str, str],
) -> dict[str, str]:
    """Baselines after a successful live transfer of exactly `plan.send`.

    Only files that are now provably identical on both sides get recorded.
    Blocked and diverged files keep whatever baseline they had, so a later run
    still sees the same divergence rather than silently forgetting it.
    """
    new = dict(old)
    for d in plan.in_sync:
        new[d.rel] = hashes[d.rel].local  # type: ignore[assignment]
    for d in plan.send:
        h = hashes[d.rel]
        new[d.rel] = h.local if direction == "push" else h.remote  # type: ignore[assignment]
    return new


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_remote_hashes(out: str, files: list[str]) -> dict[str, str | None]:
    """Parse the one-shot remote hash query. Any surprise is fatal.

    A partial or garbled reply must never be read as 'that file is missing',
    which would turn a transport hiccup into an overwrite.
    """
    parsed: dict[str, str | None] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        head, _, rel = line.partition(" ")
        rel = rel.strip()
        if not rel:
            print(f"[ERROR] unparseable remote hash line: {line!r}", file=sys.stderr)
            raise SystemExit(1)
        if head == "MISSING":
            parsed[rel] = None
        elif len(head) == 64 and all(c in "0123456789abcdef" for c in head):
            parsed[rel] = head
        else:
            print(f"[ERROR] unparseable remote hash line: {line!r}", file=sys.stderr)
            raise SystemExit(1)

    unanswered = [f for f in files if f not in parsed]
    if unanswered:
        print(f"[ERROR] remote answered for {len(parsed)}/{len(files)} files; "
              f"first unanswered: {unanswered[0]}", file=sys.stderr)
        raise SystemExit(1)
    return parsed


def _state_path(repo: Path) -> Path:
    common = _run("git", "-C", str(repo), "rev-parse", "--git-common-dir")
    if common.returncode != 0:
        print(f"[ERROR] {repo} is not a git checkout", file=sys.stderr)
        raise SystemExit(1)
    return (repo / common.stdout.strip()).resolve() / STATE_FILE


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


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
    if host == "silver":
        return os.environ.get(SILVER_CHECKOUT_ENV, "~/code/music-dj-tools")
    return REMOTE_PATHS.get(host, "~/code/music-dj-tools")


def _remote_hash_script(root: str, files: list[str]) -> str:
    """Shell to print '<sha256> <rel>' or 'MISSING <rel>' for every carried path.

    sha256sum is coreutils (Linux, agentbox); macOS ships shasum instead, and
    silver is a Mac, so both are probed.
    """
    quoted = " ".join(shlex.quote(f) for f in files)
    return (
        f"cd {shlex.quote(root)} || exit 1\n"
        f'if command -v sha256sum >/dev/null 2>&1; then H="sha256sum";'
        f' else H="shasum -a 256"; fi\n'
        f"for f in {quoted}; do\n"
        f'  if [ -f "$f" ]; then echo "$($H "$f" | cut -d\' \' -f1) $f";'
        f'  else echo "MISSING $f"; fi\n'
        f"done\n"
    )


def _remote_hashes(host: str, root: str, files: list[str]) -> dict[str, str | None]:
    proc = _run("ssh", host, _remote_hash_script(root, files))
    if proc.returncode != 0:
        print(f"[ERROR] could not hash {host}:{root} (ssh exit {proc.returncode})\n"
              f"{proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    return _parse_remote_hashes(proc.stdout, files)


def _print_plan(direction: str, host: str, root: str, plan: Plan) -> None:
    print(f"\n{direction} {host}:{root}")
    print(f"  {len(plan.in_sync)} already in sync")
    for d in plan.send:
        print(f"  -> {d.rel}  ({d.reason})")
    for d in plan.blocked:
        print(f"  [SKIP] {d.rel}  ({d.reason})")
    for d in plan.diverged:
        print(f"  [DIVERGED] {d.rel}  ({d.reason})")


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

    state_path = _state_path(repo)
    state = _load_state(state_path)
    baselines: dict[str, str] = state.get("hosts", {}).get(host, {})
    remote = _remote_hashes(host, root, files)
    hashes = {
        f: Hashes(local=_sha256(repo / f), remote=remote[f], baseline=baselines.get(f))
        for f in files
    }

    plan = plan_sync(direction, hashes)
    _print_plan(direction, host, root, plan)
    sys.stdout.flush()  # keep the plan above the stderr stop when piped

    # A real conflict is a stop, not a warning: transferring the agreed files
    # while one is diverged would report success over a half-done sync.
    if plan.diverged:
        print(f"\n[ERROR] {len(plan.diverged)} file(s) changed on BOTH sides since the "
              f"last sync with {host}. Nothing transferred.\n"
              f"        Resolve by hand, then re-run.", file=sys.stderr)
        for d in plan.diverged:
            print(f"        {d.rel}: {d.reason}", file=sys.stderr)
        return 2

    if not plan.send:
        print(f"\n[OK] nothing to {direction}: all {len(files)} files agree "
              f"with {host}\n")
        _save_state(state_path, _merge_state(state, host,
                    updated_baselines(direction, plan, hashes, baselines)))
        return 0

    moving = [d.rel for d in plan.send]
    # Plain rsync: the decision of WHAT moves was already made from content
    # hashes above, so -u / --ignore-existing must not second-guess it.
    #
    # NO --protect-args: macOS ships rsync 2.6.9 (protocol 29) and silver runs
    # it. That flag is rsync 3.0+, and an unknown option makes the REMOTE side
    # print usage and drop the connection, which surfaces locally as the
    # useless "connection unexpectedly closed ... code 12". Carried paths are
    # repo-relative and space-free, so quoting is not at risk.
    common = ["rsync", "-az", "--relative"]
    if direction == "push":
        cmd = common + [f"./{f}" for f in moving] + [f"{host}:{root}/"]
    else:
        cmd = common + [f"{host}:{root}/./{f}" for f in moving] + ["."]

    if not live:
        print(f"\n[DRY RUN] would {direction} {len(moving)} file(s) "
              f"({_size(moving, repo)/1024:.0f} KB). pass --live to transfer\n")
        return 0

    proc = subprocess.run(cmd, cwd=str(repo))
    if proc.returncode != 0:
        print(f"[ERROR] rsync exited {proc.returncode}", file=sys.stderr)
        return proc.returncode

    _save_state(state_path, _merge_state(state, host,
                updated_baselines(direction, plan, hashes, baselines)))
    print(f"\n[OK] {direction} complete: {len(moving)} transferred, "
          f"{len(plan.in_sync)} already in sync, {len(plan.blocked)} skipped "
          f"(run the opposite direction for those)\n")
    return 0


def _merge_state(state: dict, host: str, baselines: dict[str, str]) -> dict:
    merged = dict(state)
    hosts = dict(merged.get("hosts", {}))
    hosts[host] = baselines
    merged["hosts"] = hosts
    return merged


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
