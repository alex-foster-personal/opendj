"""Measure the worktree set: what exists, how big, how idle, and who is using it.

Split out of `worktree_lifecycle` so each module stays under the 600-line
quality-gate limit, along the seam that matters: this module MEASURES and never
decides or deletes, so it is safe to import from anywhere and safe to run while
other agents work in the trees it is looking at.

Two measurements here are deliberately conservative rather than convenient:

* `_last_touch` skips REGENERABLE directories, so a `uv sync` or a `pnpm install`
  does not read as a human touching the worktree. Without that every tree looks
  active and nothing is ever reapable.
* `_liveness` returns "unknown" rather than "idle" when it cannot answer. An
  `lsof +D` that timed out and one that found nothing both come back empty, and
  only one of them is safe to delete on.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_SLUG: str = "maintainer/music-dj-tools"

# Directories that are build output, not work. They dominate a worktree's size
# and are excluded from BOTH the last-touch scan and the backup archive.
REGENERABLE: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".grimp_cache", ".svelte-kit", "dist", "build",
    "target", ".turbo", ".vite", "playwright-report", "test-results", "htmlcov",
    ".coverage", ".DS_Store",
})

# Build output whose directory NAME is too generic to blocklist. `gen` and
# `payload` mean nothing on their own; at these paths they are Tauri's generated
# schemas and its bundled payload. Measured Sun 6 Sep 2026: these two prefixes
# alone were 10,436 of the 11,564 paths a whole-machine reap would have archived.
REGENERABLE_PREFIXES: tuple[str, ...] = (
    "apps/desktop/src-tauri/gen/",
    "apps/desktop/src-tauri/payload/",
)


def _is_regenerable(rel: str) -> bool:
    """Whether a worktree-relative path is build output rather than work.

    Matches the DIRECTORY ITSELF as well as paths under it. `git ls-files
    --others --ignored --directory` collapses a wholly-ignored tree to its
    directory name, so a bare `startswith(prefix)` -- where the prefix carries a
    trailing slash -- silently misses the exact form this is fed most often.
    """
    stripped = rel.rstrip("/")
    for prefix in REGENERABLE_PREFIXES:
        if stripped == prefix.rstrip("/") or stripped.startswith(prefix):
            return True
    return any(part in REGENERABLE or part.endswith(".egg-info")
               for part in Path(stripped).parts)

LSOF_TIMEOUT_S: int = 30


# ------------------------------------------------------------------ types


@dataclasses.dataclass(frozen=True)
class PullRequest:
    number: int
    state: str  # OPEN | MERGED | CLOSED

    @property
    def is_finished(self) -> bool:
        return self.state in {"MERGED", "CLOSED"}


@dataclasses.dataclass
class Worktree:
    path: Path
    branch: str | None
    head: str
    is_primary: bool
    exists: bool
    last_touch: float          # epoch seconds, 0.0 when nothing was scanned
    dirty_paths: list[str]     # tracked-but-modified + untracked, repo-relative
    kept_ignored: list[str]    # gitignored, present, NOT regenerable
    size_kb: int | None        # None when --no-size
    pr: PullRequest | None
    liveness: str              # "idle" | "live" | "unknown"
    live_detail: str

    @property
    def age_hours(self) -> float:
        if not self.last_touch:
            return float("inf")
        return (dt.datetime.now(dt.UTC).timestamp() - self.last_touch) / 3600.0

    @property
    def has_work(self) -> bool:
        return bool(self.dirty_paths or self.kept_ignored)


# ------------------------------------------------------------------ git


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _git_ok(*args: str, cwd: Path) -> str:
    proc = _git(*args, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout


def _primary_checkout(repo: Path) -> Path:
    """The non-linked checkout, i.e. the parent of the shared git common dir."""
    common = _git_ok("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)
    return Path(common.strip()).parent


def _parse_worktree_list(repo: Path) -> list[tuple[Path, str | None, str]]:
    out = _git_ok("worktree", "list", "--porcelain", cwd=repo)
    entries: list[tuple[Path, str | None, str]] = []
    path: Path | None = None
    branch: str | None = None
    head = ""
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            branch, head = None, ""
        elif line.startswith("HEAD "):
            head = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
        elif line == "" and path is not None:
            entries.append((path, branch, head))
            path, branch, head = None, None, ""
    return entries


# ------------------------------------------------------------------ measurement


def _walk_paths(root: Path):
    """Every file under root, skipping REGENERABLE directories."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.name in REGENERABLE:
                        # `.git` is a FILE in a linked worktree, not a directory,
                        # so a directory-only filter lets its mtime count as a
                        # human touch and no worktree ever looks stale.
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        yield entry
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue


def _last_touch(root: Path) -> float:
    newest = 0.0
    for entry in _walk_paths(root):
        try:
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
        except (FileNotFoundError, PermissionError):
            continue
    return newest


def _size_kb(root: Path) -> int:
    proc = subprocess.run(["du", "-sk", str(root)], capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    return int(proc.stdout.split()[0])


def _dirty_paths(worktree: Path) -> list[str]:
    """Modified-tracked plus untracked paths, from porcelain, renames unpacked.

    `-uall` matters: the collapsed form reports a directory, and a directory
    name is not a thing tar can be told to preserve exactly.
    """
    out = _git_ok("status", "--porcelain=v1", "-uall", "-z", cwd=worktree)
    fields = [f for f in out.split("\0") if f]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        status, name = field[:2], field[3:]
        index += 1
        if "R" in status and index < len(fields):
            # rename: the NEXT NUL field is the source path
            paths.append(fields[index])
            index += 1
        paths.append(name)
    return sorted({
        p for p in paths
        if (worktree / p).exists()
        # An UNTRACKED .venv or node_modules is still regenerable build output.
        # Filtering only the gitignored half missed it: a worktree whose
        # .gitignore has not landed yet reports node_modules as untracked, and
        # the reaper would have tarred a whole dependency tree as "work".
        and not _is_regenerable(p)
    })


def _kept_ignored(worktree: Path) -> list[str]:
    """Gitignored files that are present and are NOT regenerable build output.

    `--directory` collapses a wholly-ignored directory into one entry, which is
    what keeps `.venv/` from being enumerated file by file. Anything that
    survives the REGENERABLE filter is then expanded, because those are real
    local files (a `.env`, a captured fixture, a scratch note) that no clone can
    reproduce.
    """
    out = _git_ok(
        "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
        cwd=worktree,
    )
    kept: list[str] = []
    for raw in out.split("\0"):
        if not raw:
            continue
        rel = raw.rstrip("/")
        if _is_regenerable(rel):
            continue
        target = worktree / rel
        if target.is_dir():
            kept.extend(str(Path(e.path).relative_to(worktree)) for e in _walk_paths(target))
        elif target.exists():
            kept.append(rel)
    return sorted(set(kept))


# Git starts one of these per worktree, detached, and it holds a DIR handle on
# the worktree root. It is git's own watcher, not a user of the tree, so it is
# the one process whose presence must NOT read as "somebody is working here".
# Confirmed by hand Sun 6 Sep 2026: pid 35043,
# `git fsmonitor--daemon run --detach`, holding three worktree roots open.
FSMONITOR_MARKER: str = "fsmonitor--daemon"


def _real_users(pids: list[str], self_marker: str) -> list[str]:
    """Drop this process, its parent, git's fsmonitor daemon, and dead pids."""
    own = {str(os.getpid()), str(os.getppid())}
    users: list[str] = []
    for pid in pids:
        if pid in own:
            continue
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", pid], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not command:                       # exited between the probe and here
            continue
        if FSMONITOR_MARKER in command:
            continue
        if self_marker and self_marker in command:
            continue                          # our own pattern matching our own argv
        users.append(pid)
    return users


def _liveness(worktree: Path, self_marker: str) -> tuple[str, str]:
    """Is any process using this worktree?

    Cheap probe first (`pgrep -f`), expensive one second (`lsof +D`). A probe
    that cannot answer returns "unknown", never "idle": an lsof that times out on
    a big tree looks exactly like an lsof that found nothing, and only one of
    those is safe to delete on.

    This errs toward KEEP on purpose. It is a point-in-time probe on a machine
    running dozens of agents, so its answer for any one worktree is noisy; a
    worktree that is genuinely finished gets caught by a later scan, whereas a
    worktree deleted under a working agent does not come back cheaply.
    """
    pattern = str(worktree)
    pgrep = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False
    )
    pids = _real_users(pgrep.stdout.split(), self_marker)
    if pids:
        return "live", f"pgrep pids {','.join(pids)}"

    if shutil.which("lsof") is None:
        return "unknown", "lsof not installed"
    try:
        lsof = subprocess.run(
            ["lsof", "-w", "-t", "+D", str(worktree)],
            capture_output=True, text=True, check=False, timeout=LSOF_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "unknown", f"lsof +D timed out after {LSOF_TIMEOUT_S}s"
    open_pids = _real_users(lsof.stdout.split(), self_marker)
    if open_pids:
        return "live", f"lsof pids {','.join(open_pids)}"
    return "idle", ""


# ------------------------------------------------------------------ GitHub


def _pr_index(use_github: bool) -> dict[str, PullRequest]:
    """headRefName -> newest PR. One call, not one per branch."""
    if not use_github:
        return {}
    if shutil.which("gh") is None:
        raise RuntimeError("gh is not installed; re-run with --no-github to skip PR state")
    env = dict(os.environ)
    if "GH_TOKEN" not in env:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if token.returncode == 0 and token.stdout.strip():
            env["GH_TOKEN"] = token.stdout.strip()
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO_SLUG, "--state", "all", "--limit", "2000",
         "--json", "number,state,headRefName"],
        capture_output=True, text=True, check=False, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()}")
    index: dict[str, PullRequest] = {}
    for item in json.loads(proc.stdout):
        head = item["headRefName"]
        pull = PullRequest(number=item["number"], state=item["state"])
        current = index.get(head)
        if current is None or pull.number > current.number:
            index[head] = pull
    return index


# ------------------------------------------------------------------ collection

# ------------------------------------------------------------------ collection


def recheck(worktree: Path) -> tuple[str, str, list[str]]:
    """Fresh liveness + dirty/kept read, taken immediately before a destructive verb.

    `collect()` measures the whole worktree set once, up front, which is exactly
    what a reap loop must NOT rely on right before it deletes: everything the
    KEEP/REAP call rests on can go stale during the scan and the per-target
    backup that follows it. This re-reads only the two things liveness and a
    destructive removal actually need, for one worktree, so it is cheap enough
    to call again right before each `git worktree remove`.
    """
    self_marker = Path(__file__).name
    liveness, detail = _liveness(worktree, self_marker)
    paths = sorted(set(_dirty_paths(worktree)) | set(_kept_ignored(worktree)))
    return liveness, detail, paths


def collect(repo: Path, *, use_github: bool, with_size: bool) -> list[Worktree]:
    primary = _primary_checkout(repo)
    pr_index = _pr_index(use_github)
    self_marker = Path(__file__).name
    entries = _parse_worktree_list(repo)
    nested: dict[Path, list[Path]] = {p: [] for p, _, _ in entries}
    for path, _, _ in entries:
        for other, children in nested.items():
            if other != path and str(path).startswith(str(other) + os.sep):
                children.append(path)

    worktrees: list[Worktree] = []
    for path, branch, head in entries:
        exists = path.is_dir()
        is_primary = path.resolve() == primary.resolve()
        if not exists:
            worktrees.append(Worktree(
                path=path, branch=branch, head=head, is_primary=is_primary, exists=False,
                last_touch=0.0, dirty_paths=[], kept_ignored=[], size_kb=0,
                pr=pr_index.get(branch or ""), liveness="idle", live_detail="",
            ))
            continue
        size = None
        if with_size:
            size = _size_kb(path) - sum(_size_kb(child) for child in nested[path])
        liveness, detail = ("idle", "") if is_primary else _liveness(path, self_marker)
        worktrees.append(Worktree(
            path=path, branch=branch, head=head, is_primary=is_primary, exists=True,
            last_touch=_last_touch(path),
            dirty_paths=_dirty_paths(path),
            kept_ignored=_kept_ignored(path),
            size_kb=size,
            pr=pr_index.get(branch or ""),
            liveness=liveness, live_detail=detail,
        ))
    return worktrees


