"""Worktree lifecycle: a registry, a last-touch reaper, and a creation guard.

Usage::

    python -m scripts.worktree_lifecycle register        # scan + write the registry
    python -m scripts.worktree_lifecycle status          # table of every worktree
    python -m scripts.worktree_lifecycle reap --dry-run  # what WOULD be reaped
    python -m scripts.worktree_lifecycle reap --apply    # back up, then remove
    python -m scripts.worktree_lifecycle restore <branch>
    python -m scripts.worktree_lifecycle guard           # before `git worktree add`

Why this exists: 144 live worktrees at roughly 0.3-0.9 GB each filled the Air's
disk to 3 GB free twice in one weekend, and ENOSPC stops threads rather than
failing one command. The expensive half of a worktree is regenerable (`.venv`,
`node_modules`); the irreplaceable half is whatever is uncommitted in it. So the
reaper's whole job is to separate those two and only ever delete the first.

REQUIREMENTS (OPS-21, OPS-22)
  R-1 Never lose uncommitted or untracked work.                     [done, run]
      [if a stale worktree holds modified, untracked or non-regenerable
       gitignored files then every one of those paths is inside the archive
       BEFORE the worktree is removed]
      [if the archive does not list every path the manifest claims then the
       reap aborts and the worktree is left standing]
      [if a restored worktree is compared to the manifest then every file
       matches its recorded sha256]
  R-2 Never touch something in use.                                 [done, run]
      [if a worktree is the primary checkout then no command ever removes it]
      [if a process holds a file under a worktree then that worktree is KEEP
       (live), whatever its age]
      [if liveness cannot be measured (lsof times out) then the verdict is
       UNKNOWN and the worktree is kept, never reaped]
  R-3 Stale is age AND finished, never age alone.                   [done, run]
      [if last touch is older than the stale window but the branch has an open
       PR then the verdict is KEEP (pr-open)]
      [if last touch is inside the stale window then the verdict is KEEP
       (active) even with a merged PR]
      [if last touch is older than the window and the PR is merged or closed or
       absent then the verdict is REAP]
  R-4 Refuse to create past the cap or the disk floor.              [done, run]
      [if live worktrees are at or above the cap then guard exits nonzero and
       prints the reap command]
      [if free disk is below the floor then guard exits nonzero and names the
       floor, the measured free space and the reap command]
      [if both are satisfied then guard exits 0 and prints nothing fatal]

Non-goals, deliberately: this never runs `git stash`, `git reset --hard` or
`git add -A`. Every one of those is repo-banned because this checkout is shared
by many concurrent agents and all three reach outside the worktree they are run
in. The reaper's destructive verb is `git worktree remove`, nothing else.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from scripts.worktree_census import (
    Worktree,
    _git,
    _git_ok,
    _parse_worktree_list,
    _primary_checkout,
    collect,
)

# ------------------------------------------------------------------ CFG

# Live-worktree cap. Measured Sun 6 Sep 2026 on the Air, not guessed: 141 linked
# worktrees, 74.8G. Reconstructing each worktree's active span from its directory
# birth time and its newest non-regenerable mtime gives, per hourly sample:
#
#   strictly worked (birth..last touch)   30d: p50 11  p90 12  max 23
#   active + the 72h grace this reaper    30d: p50 11  p90 69  max 78
#   same, last 7d (current fleet cadence) 7d:  p50 68  p90 76  max 78
#
# The cap has to hold the SECOND shape, because a worktree stays on disk for the
# whole grace window, so 90 = p90 at current cadence (76) plus ~20% headroom.
# the maintainer's instinct that 6 was far too few is right by an order of magnitude: even
# the strictly-worked p90 is 12 and its max is 23.
#
# This number is a POLICY BACKSTOP, not the real gate. The disk floor below is
# what actually binds, because worktree size varies 7x (p50 0.41G, p90 0.91G,
# max 2.89G) and a count cannot see that.
LIVE_CAP: int = 90

# Disk policy, the maintainer's numbers (Sun 6 Sep 2026): stop creating below the floor,
# and when reaping, keep going until the target is clear.
DISK_CREATE_FLOOR_GB: int = 15
DISK_REAP_TARGET_GB: int = 25

# A worktree untouched for longer than this is a reap CANDIDATE. It is only a
# reap TARGET once its PR is finished too (R-3).
STALE_HOURS: int = 72

BACKUP_ROOT: Path = Path.home() / ".cache" / "mdt-worktree-backups"

# Archives are written with the stdlib (tarfile + gzip) and nothing else. An
# earlier revision shelled out to the `zstd` binary, which is not a host tool
# this fleet declares: the pytest lane on the nucbox runners has no zstd, so
# three cases died on FileNotFoundError and took trunk red with them (same
# class as the `just` incident, #1370). Compression ratio was never the
# constraint here -- these archives hold a few uncommitted files -- so the
# dependency bought nothing and cost a red main. Reading a LEGACY .tar.zst
# still needs the binary; that path says so by name instead of guessing.
ARCHIVE_SUFFIX: str = ".tar.gz"
LEGACY_ZSTD_SUFFIX: str = ".tar.zst"

REMOTE_HOST: str = "nucbox-wsl"
REMOTE_DIR: str = "wt-backups"
REMOTE_PROBE_TIMEOUT_S: int = 10

REAP_COMMAND: str = "just wt-reap        # then: just wt-reap-apply"

# ------------------------------------------------------------------ verdicts


@dataclasses.dataclass(frozen=True)
class Verdict:
    """A keep-or-reap decision plus the reason a human needs to trust it."""

    action: str   # "KEEP" | "REAP"
    reason: str


def classify(worktree: Worktree, stale_hours: int) -> Verdict:
    if worktree.is_primary:
        return Verdict("KEEP", "primary checkout")
    if not worktree.exists:
        return Verdict("KEEP", "path missing; run `git worktree prune`")
    if worktree.liveness == "live":
        return Verdict("KEEP", f"live ({worktree.live_detail})")
    if worktree.liveness == "unknown":
        return Verdict("KEEP", f"liveness UNKNOWN ({worktree.live_detail})")
    if worktree.age_hours < stale_hours:
        return Verdict("KEEP", f"active ({worktree.age_hours:.0f}h < {stale_hours}h)")
    if worktree.pr is not None and not worktree.pr.is_finished:
        return Verdict("KEEP", f"pr-open (#{worktree.pr.number})")
    pr_note = f"PR #{worktree.pr.number} {worktree.pr.state.lower()}" if worktree.pr else "no PR"
    return Verdict("REAP", f"stale {worktree.age_hours:.0f}h, {pr_note}")


# ------------------------------------------------------------------ backup


def _backup_paths(worktree: Worktree) -> list[str]:
    return sorted(set(worktree.dirty_paths) | set(worktree.kept_ignored))


def _archive_name(worktree: Worktree) -> str:
    label = (worktree.branch or worktree.head[:12] or "detached").replace("/", "--")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{label}-{stamp}"


def back_up(worktree: Worktree, backup_root: Path, *, remote: str | None) -> dict:
    """Tar every uncommitted path, verify the tar lists them, then land it.

    The verification is the point of the function. A backup step that ran is not
    a backup that holds the files, and the only moment that difference is cheap
    to catch is BEFORE the worktree is deleted.
    """
    paths = _backup_paths(worktree)
    if not paths:
        raise RuntimeError(f"back_up called for {worktree.path} which has no uncommitted work")
    backup_root.mkdir(parents=True, exist_ok=True)
    name = _archive_name(worktree)
    compressed = backup_root / f"{name}{ARCHIVE_SUFFIX}"
    with tarfile.open(compressed, "w:gz") as tar:
        for rel in paths:
            tar.add(worktree.path / rel, arcname=rel, recursive=False)

    with tarfile.open(compressed, "r:gz") as tar:
        packed = sorted(m.name for m in tar.getmembers())
    missing = sorted(set(paths) - set(packed))
    if missing:
        compressed.unlink(missing_ok=True)
        raise RuntimeError(
            f"archive for {worktree.path} is missing {len(missing)} path(s), "
            f"first: {missing[:3]}; the worktree has NOT been touched"
        )

    digests = {
        rel: hashlib.sha256((worktree.path / rel).read_bytes()).hexdigest()
        for rel in paths if (worktree.path / rel).is_file()
    }

    manifest = {
        "branch": worktree.branch,
        "head": worktree.head,
        "worktree": str(worktree.path),
        "archive": compressed.name,
        "created_utc": dt.datetime.now(dt.UTC).isoformat(),
        "paths": paths,
        "sha256": digests,
        "archive_bytes": compressed.stat().st_size,
    }
    (backup_root / f"{name}.manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")

    manifest["remote"] = _push_remote(backup_root, compressed.name, remote)
    return manifest


def _push_remote(backup_root: Path, archive_name: str, remote: str | None) -> str:
    if not remote:
        return "skipped (--no-remote)"
    probe = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={REMOTE_PROBE_TIMEOUT_S}",
         remote, f"mkdir -p ~/{REMOTE_DIR} && echo ok"],
        capture_output=True, text=True, check=False, timeout=REMOTE_PROBE_TIMEOUT_S + 5,
    )
    if probe.returncode != 0 or "ok" not in probe.stdout:
        return f"unreachable ({probe.stderr.strip()[:80] or 'no answer'}); local copy only"
    stem = archive_name[: -len(ARCHIVE_SUFFIX)]
    files = [str(backup_root / archive_name), str(backup_root / f"{stem}.manifest.json")]
    rsync = subprocess.run(
        ["rsync", "-a", *files, f"{remote}:{REMOTE_DIR}/"],
        capture_output=True, text=True, check=False,
    )
    if rsync.returncode != 0:
        return f"rsync failed ({rsync.stderr.strip()[:80]}); local copy only"
    return f"{remote}:{REMOTE_DIR}/{archive_name}"


# ------------------------------------------------------------------ registry


def registry_path(repo: Path) -> Path:
    common = _git_ok("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)
    return Path(common.strip()) / "mdt-worktree-registry.json"


def _owner_of(worktree: Worktree) -> str:
    """Who made this. Branch prefix is the only signal a scan actually has."""
    branch = worktree.branch or ""
    if branch.startswith(("codex/", "codex--")):
        return "codex"
    if branch.startswith("cc/"):
        return "claude-cli"
    if branch.startswith("af--"):
        return "claude"
    if branch.startswith(("rebase-", "ml")):
        return "rebase-lane"
    return "unknown"


def write_registry(repo: Path, worktrees: list[Worktree], stale_hours: int) -> Path:
    target = registry_path(repo)
    previous: dict[str, dict] = {}
    if target.exists():
        previous = {e["path"]: e for e in json.loads(target.read_text())["worktrees"]}
    now = dt.datetime.now(dt.UTC).isoformat()
    rows = []
    for worktree in worktrees:
        key = str(worktree.path)
        rows.append({
            "path": key,
            "branch": worktree.branch,
            "owner": _owner_of(worktree),
            "pr": worktree.pr.number if worktree.pr else None,
            "pr_state": worktree.pr.state if worktree.pr else None,
            "first_seen": previous.get(key, {}).get("first_seen", now),
            "last_scan": now,
            "last_touch": worktree.last_touch,
            "size_kb": worktree.size_kb,
            "dirty": len(worktree.dirty_paths),
            "kept_ignored": len(worktree.kept_ignored),
            "verdict": dataclasses.asdict(classify(worktree, stale_hours)),
        })
    target.write_text(json.dumps(
        {"generated_utc": now, "stale_hours": stale_hours, "worktrees": rows}, indent=1) + "\n")
    return target


# ------------------------------------------------------------------ disk


def free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / 1024**3


# ------------------------------------------------------------------ rendering


def _fmt_age(hours: float) -> str:
    if hours == float("inf"):
        return "never"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def render_table(worktrees: list[Worktree], stale_hours: int) -> str:
    header = ("PATH", "BRANCH", "PR", "SIZE", "TOUCHED", "DIRTY", "VERDICT")
    rows = [header, tuple("-" * len(h) for h in header)]
    for worktree in sorted(worktrees, key=lambda w: -w.age_hours):
        verdict = classify(worktree, stale_hours)
        size = "-" if worktree.size_kb is None else f"{worktree.size_kb / 1024**2:.2f}G"
        rows.append((
            str(worktree.path).replace(str(Path.home()), "~"),
            worktree.branch or f"({worktree.head[:8]} detached)",
            f"#{worktree.pr.number} {worktree.pr.state[:4].lower()}" if worktree.pr else "-",
            size,
            _fmt_age(worktree.age_hours),
            (f"{len(worktree.dirty_paths)}+{len(worktree.kept_ignored)}i"
             if worktree.has_work else "clean"),
            f"{verdict.action}: {verdict.reason}",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
                     for row in rows)


# ------------------------------------------------------------------ commands


def cmd_register(args: argparse.Namespace) -> int:
    worktrees = collect(args.repo, use_github=args.github, with_size=args.size)
    target = write_registry(args.repo, worktrees, args.stale_hours)
    reapable = sum(1 for w in worktrees if classify(w, args.stale_hours).action == "REAP")
    print(f"registered {len(worktrees)} worktree(s) -> {target}")
    print(f"{reapable} currently classify as REAP; run `{REAP_COMMAND}` to see them")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    worktrees = collect(args.repo, use_github=args.github, with_size=args.size)
    if args.json:
        print(json.dumps([{**dataclasses.asdict(w), "path": str(w.path),
                           "verdict": dataclasses.asdict(classify(w, args.stale_hours))}
                          for w in worktrees], indent=1, default=str))
        return 0
    print(render_table(worktrees, args.stale_hours))
    sized = [w.size_kb for w in worktrees if w.size_kb]
    live = [w for w in worktrees if w.exists and not w.is_primary]
    active = [w for w in live if w.age_hours < args.stale_hours]
    print(f"\n{len(live)} linked worktree(s), {len(active)} active "
          f"(touched < {args.stale_hours}h), cap {args.cap}")
    if sized:
        print(f"{sum(sized) / 1024**2:.1f}G across measured worktrees; "
              f"{free_gb(args.repo):.1f}G free on disk (floor {args.floor_gb}G)")
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    worktrees = collect(args.repo, use_github=args.github, with_size=args.size)
    targets = [w for w in worktrees if classify(w, args.stale_hours).action == "REAP"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[reap {mode}] {len(targets)} of {len(worktrees)} worktree(s) are stale and finished")
    if not targets:
        return 0
    recovered_kb = 0
    log_path = args.backup_root / "reap-log.jsonl"
    for worktree in targets:
        verdict = classify(worktree, args.stale_hours)
        paths = _backup_paths(worktree)
        size = worktree.size_kb or 0
        recovered_kb += size
        print(f"\n  {worktree.path}")
        print(f"    branch   {worktree.branch or '(detached ' + worktree.head[:8] + ')'}")
        print(f"    why      {verdict.reason}")
        print(f"    size     {size / 1024**2:.2f}G")
        print(f"    backup   {len(paths)} path(s)"
              + (f", e.g. {paths[:3]}" if paths else " (nothing uncommitted)"))
        if not args.apply:
            continue
        record: dict = {"worktree": str(worktree.path), "branch": worktree.branch,
                        "reason": verdict.reason, "size_kb": size,
                        "utc": dt.datetime.now(dt.UTC).isoformat()}
        if paths:
            record["backup"] = back_up(worktree, args.backup_root,
                                       remote=None if args.no_remote else args.remote)
            print(f"    saved    {record['backup']['archive']} "
                  f"({record['backup']['archive_bytes'] / 1024:.0f} KiB) "
                  f"-> {record['backup']['remote']}")
        removal = _git("worktree", "remove", "--force", str(worktree.path), cwd=args.repo)
        if removal.returncode != 0:
            record["removed"] = False
            record["error"] = removal.stderr.strip()
            print(f"    [ERROR] git worktree remove refused: {removal.stderr.strip()}")
        else:
            record["removed"] = True
            print("    removed  git worktree remove --force")
        args.backup_root.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    print(f"\n[reap {mode}] {recovered_kb / 1024**2:.1f}G "
          f"{'recovered' if args.apply else 'would be recovered'}; "
          f"free now {free_gb(args.repo):.1f}G, target {args.reap_target_gb}G")
    if not args.apply:
        print("nothing was changed. Re-run with --apply to back up and remove.")
    return 0


def _extract_archive(archive: Path, target: Path) -> None:
    """Unpack a backup archive into `target`, whichever format it is in.

    Two formats, deliberately asymmetric. `.tar.gz` is what this module WRITES
    and it needs nothing but the stdlib. `.tar.zst` is what the first revision
    wrote, and roughly 130 of them are already sitting in
    ~/.cache/mdt-worktree-backups and on nucbox-wsl:~/wt-backups, so dropping
    the reader would strand real uncommitted work. That reader needs the `zstd`
    binary and there is no stdlib substitute, so when it is absent this raises
    and NAMES it rather than falling back to something that would silently hand
    back an empty or partial tree. A restore that half-succeeds is worse than
    one that refuses.
    """
    if archive.name.endswith(ARCHIVE_SUFFIX):
        with tarfile.open(archive, "r:gz") as tar:
            # Our own archive, written by back_up(); "data" would drop the mode
            # bits a byte-for-byte restore is supposed to keep.
            tar.extractall(target, filter="fully_trusted")
        return
    if archive.name.endswith(LEGACY_ZSTD_SUFFIX):
        if shutil.which("zstd") is None:
            raise RuntimeError(
                f"{archive.name} is a legacy zstd archive and the 'zstd' binary is not on "
                f"PATH, so it cannot be read here. Install zstd (brew install zstd / "
                f"apt-get install zstd) and re-run, or restore this archive on a host that "
                f"has it. New backups are written as {ARCHIVE_SUFFIX} and need no binary."
            )
        # The intermediate tar goes to a tempdir, never beside the archive: the
        # backup root is a SHARED cache that concurrent reaps write into, and
        # two restores of the same branch would otherwise race on one path.
        with tempfile.TemporaryDirectory(prefix="mdt-restore-") as scratch:
            plain = Path(scratch) / archive.with_suffix("").name   # <n>.tar.zst -> <n>.tar
            unzstd = subprocess.run(["zstd", "-d", "-q", "-f", str(archive), "-o", str(plain)],
                                    capture_output=True, text=True, check=False)
            if unzstd.returncode != 0:
                raise RuntimeError(f"zstd -d failed for {archive.name}: {unzstd.stderr.strip()}")
            with tarfile.open(plain, "r") as tar:
                tar.extractall(target, filter="fully_trusted")
        return
    raise RuntimeError(
        f"{archive.name} is in no format this restores; expected {ARCHIVE_SUFFIX} "
        f"or the legacy {LEGACY_ZSTD_SUFFIX}"
    )


def cmd_restore(args: argparse.Namespace) -> int:
    label = args.branch.replace("/", "--")
    archives = sorted(args.backup_root.glob(f"{label}-*.manifest.json"))
    if not archives:
        print(f"[ERROR] no backup for branch '{args.branch}' under {args.backup_root}",
              file=sys.stderr)
        return 1
    manifest = json.loads(archives[-1].read_text())
    target = Path(args.into) if args.into else Path(manifest["worktree"])
    if target.exists():
        print(f"[ERROR] {target} already exists; pass --into <new-path>", file=sys.stderr)
        return 1
    add = _git("worktree", "add", str(target), args.branch, cwd=args.repo)
    if add.returncode != 0:
        print(f"[ERROR] git worktree add failed: {add.stderr.strip()}", file=sys.stderr)
        return 1
    archive = args.backup_root / manifest["archive"]
    try:
        _extract_archive(archive, target)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    mismatched = [rel for rel, digest in manifest["sha256"].items()
                  if not (target / rel).is_file()
                  or hashlib.sha256((target / rel).read_bytes()).hexdigest() != digest]
    if mismatched:
        print(f"[ERROR] {len(mismatched)} restored file(s) do not match the manifest sha256: "
              f"{mismatched[:5]}", file=sys.stderr)
        return 1
    print(f"restored {len(manifest['paths'])} path(s) into {target}; "
          f"all {len(manifest['sha256'])} file digests match {archives[-1].name}")
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    """Cheap by construction: this runs before EVERY `git worktree add`.

    A full collect() walks every file of every worktree, which is minutes. The
    guard only needs a count and a statvfs, so it reads neither.
    """
    primary = _primary_checkout(args.repo).resolve()
    live = [p for p, _, _ in _parse_worktree_list(args.repo)
            if p.is_dir() and p.resolve() != primary]
    free = free_gb(args.repo)
    failures: list[str] = []
    if len(live) >= args.cap:
        failures.append(
            f"worktree cap reached: {len(live)} live, cap {args.cap}. "
            f"The cap is p90 of simultaneously ACTIVE worktrees plus headroom "
            f"(.planning/FANOUT-CONVENTIONS.md)."
        )
    if free < args.floor_gb:
        failures.append(
            f"disk floor breached: {free:.1f}G free, floor {args.floor_gb:.0f}G. "
            f"Reap until {args.reap_target_gb:.0f}G is free."
        )
    if failures:
        print("[ERROR] refusing to create another worktree:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(f"\nFree one up first:\n  {REAP_COMMAND}", file=sys.stderr)
        return 1
    print(f"[wt-guard] OK: {len(live)} live worktree(s) of {args.cap}, {free:.1f}G free "
          f"(floor {args.floor_gb:.0f}G)")
    return 0


# ------------------------------------------------------------------ cli


def build_parser() -> argparse.ArgumentParser:
    """Shared flags live on a `parents=` parser so they work on EITHER side of
    the subcommand. argparse otherwise accepts `... --repo X status` and rejects
    `... status --repo X`, which is the order everybody types."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="any checkout of the repo; the worktree set is shared")
    common.add_argument("--stale-hours", type=int, default=STALE_HOURS)
    common.add_argument("--cap", type=int, default=LIVE_CAP)
    common.add_argument("--floor-gb", type=float, default=DISK_CREATE_FLOOR_GB)
    common.add_argument("--reap-target-gb", type=float, default=DISK_REAP_TARGET_GB)
    common.add_argument("--backup-root", type=Path, default=BACKUP_ROOT)
    common.add_argument("--remote", default=REMOTE_HOST)
    common.add_argument("--no-remote", action="store_true", help="never rsync off-machine")
    common.add_argument("--no-github", dest="github", action="store_false",
                        help="skip PR lookup; every branch then counts as 'no PR'")
    common.add_argument("--no-size", dest="size", action="store_false",
                        help="skip du, which is the slow half of a scan")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("register", parents=[common]).set_defaults(func=cmd_register)
    status = subparsers.add_parser("status", parents=[common])
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    reap = subparsers.add_parser("reap", parents=[common])
    group = reap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    reap.set_defaults(func=cmd_reap)
    restore = subparsers.add_parser("restore", parents=[common])
    restore.add_argument("branch")
    restore.add_argument("--into", default=None)
    restore.set_defaults(func=cmd_restore)
    subparsers.add_parser("guard", parents=[common]).set_defaults(func=cmd_guard)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
