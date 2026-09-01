"""Quarantine known, regenerable repository artifacts without touching source.

The cleanup is intentionally allowlisted. It does not search for generic names
such as ``target`` or ``node_modules`` because this repository contains locked
fixtures and many concurrent worktrees. A dry run is the default; ``--apply``
moves the exact known artifact directories into a recoverable Trash session and
writes a manifest describing how to restore them.

Usage::

    uv run --frozen python -m scripts.cleanup_repo --repo "$PWD"
    uv run --frozen python -m scripts.cleanup_repo --repo "$PWD" --apply
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class CleanupTarget:
    relative_path: str
    reason: str


CLEANUP_TARGETS: tuple[CleanupTarget, ...] = (
    CleanupTarget("spikes/aalink-js-link/node_modules", "Node dependency tree"),
    CleanupTarget("spikes/carabiner-link/.tools", "downloaded CMake toolchain"),
    CleanupTarget("spikes/carabiner-link/build", "CMake build output"),
    CleanupTarget("spikes/carabiner-link/upstream", "downloaded upstream clone"),
    CleanupTarget("spikes/rusty-link/.cargo-home", "downloaded Cargo cache"),
    CleanupTarget("spikes/rusty-link/.rustup-home", "downloaded Rust toolchain"),
    CleanupTarget("spikes/rusty-link/.tool-venv", "generated Python tool environment"),
    CleanupTarget("spikes/rusty-link/target", "Cargo build output"),
)

EMPTY_SPIKE_PARENTS: tuple[str, ...] = (
    "spikes/aalink-js-link",
    "spikes/rusty-link",
)


class CleanupError(RuntimeError):
    """The cleanup cannot proceed without weakening its safety contract."""


@dataclass
class PlannedMove:
    relative_path: str
    reason: str
    source: str
    destination: str
    bytes: int
    files: int
    status: str = "pending"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CleanupError(f"git {' '.join(args)} failed for {repo}: {detail}")
    return result.stdout.strip()


def validate_repo(repo: Path) -> Path:
    repo = repo.expanduser().resolve()
    actual = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if actual != repo:
        raise CleanupError(f"--repo must be the worktree root: supplied {repo}, resolved {actual}")
    return repo


def default_trash_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / ".Trash"
    if sys.platform.startswith("linux"):
        return Path.home() / ".local/share/Trash/files"
    raise CleanupError("no default Trash location on this platform; pass --trash-root")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or _inside(first, second) or _inside(second, first)


def registered_worktrees(repo: Path) -> list[Path]:
    """Return every worktree registered in ``repo``'s Git common directory."""
    worktrees: list[Path] = []
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            worktrees.append(Path(line.removeprefix("worktree ")).expanduser().resolve())
    return worktrees


def git_markers(path: Path) -> list[Path]:
    """Find embedded ``.git`` entries without following symlinks."""
    if path.is_symlink() or not path.is_dir():
        return []

    markers: list[Path] = []
    pending = [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == ".git":
                    markers.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    return markers


def worktree_hazards(repo: Path, paths: list[Path]) -> list[str]:
    """Describe registered, linked, or dirty Git trees a move would disturb."""
    hazards: list[str] = []

    for worktree in registered_worktrees(repo):
        if worktree == repo:
            continue
        for target in paths:
            if _overlaps(worktree, target):
                hazards.append(f"registered worktree {worktree} overlaps {target}")

    for target in paths:
        for marker in git_markers(target):
            nested_repo = marker.parent.resolve()
            if marker.is_symlink() or not marker.is_dir():
                hazards.append(f"linked Git worktree marker {marker} is inside {target}")
                continue

            nested_top = Path(_git(nested_repo, "rev-parse", "--show-toplevel")).resolve()
            if nested_top != nested_repo:
                hazards.append(
                    f"embedded Git metadata {marker} resolves outside its directory to {nested_top}"
                )
                continue

            other_worktrees = [
                worktree
                for worktree in registered_worktrees(nested_repo)
                if worktree != nested_repo
            ]
            if other_worktrees:
                rendered = ", ".join(str(worktree) for worktree in other_worktrees)
                hazards.append(
                    f"nested repository {nested_repo} has registered worktrees: {rendered}"
                )

            dirty = _git(
                nested_repo,
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            )
            if dirty:
                hazards.append(f"nested repository {nested_repo} has uncommitted files")

    return hazards


def tree_stats(path: Path) -> tuple[int, int]:
    """Return apparent bytes and file/symlink count without following symlinks."""
    if path.is_symlink() or not path.is_dir():
        stat = path.lstat()
        return stat.st_size, 1

    total_bytes = 0
    files = 0
    pending = [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                stat = entry.stat(follow_symlinks=False)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    files += 1
                    total_bytes += stat.st_size
    return total_bytes, files


def human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    raise AssertionError("unreachable")


def discover(repo: Path) -> list[tuple[CleanupTarget, Path, int, int]]:
    found: list[tuple[CleanupTarget, Path, int, int]] = []
    for target in CLEANUP_TARGETS:
        path = repo / target.relative_path
        if not path.exists() and not path.is_symlink():
            continue
        size, files = tree_stats(path)
        found.append((target, path, size, files))
    return found


def open_handles(paths: list[Path]) -> tuple[list[dict[str, str]], str | None]:
    """Return lsof handles inside targets, or an explicit availability warning."""
    executable = shutil.which("lsof")
    if executable is None:
        return [], "lsof is unavailable; live-open-file detection was UNAVAILABLE"

    result = subprocess.run(
        (executable, "-nP", "-Fpcn"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise CleanupError(f"lsof failed with exit {result.returncode}: {result.stderr.strip()}")

    prefixes = tuple((str(path), f"{path}{os.sep}") for path in paths)
    pid = "?"
    command = "?"
    seen: set[tuple[str, str]] = set()
    hits: list[dict[str, str]] = []
    for raw in result.stdout.splitlines():
        if not raw:
            continue
        field, value = raw[0], raw[1:]
        if field == "p":
            pid = value
        elif field == "c":
            command = value
        elif field == "n" and any(
            value == exact or value.startswith(descendant) for exact, descendant in prefixes
        ):
            key = (pid, value)
            if key not in seen:
                seen.add(key)
                hits.append({"pid": pid, "command": command, "path": value})
    return hits, None


def _session_dir(trash_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = trash_root / f"music-dj-tools-cleanup-{stamp}-{os.getpid()}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=False)
    return candidate


def _write_manifest(
    path: Path,
    *,
    state: str,
    repo: Path,
    head: str,
    moves: list[PlannedMove],
    error: str | None = None,
) -> None:
    body = {
        "schema_version": 1,
        "state": state,
        "created_at": datetime.now(UTC).isoformat(),
        "repo": str(repo),
        "repo_head": head,
        "error": error,
        "moves": [asdict(move) for move in moves],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def apply_cleanup(
    repo: Path,
    trash_root: Path,
    found: list[tuple[CleanupTarget, Path, int, int]],
) -> Path:
    trash_root = trash_root.expanduser().resolve()
    if _inside(trash_root, repo):
        raise CleanupError(f"Trash root must be outside the repository: {trash_root}")

    sources = [path for _, path, _, _ in found]
    hazards = worktree_hazards(repo, sources)
    if hazards:
        sample = "\n".join(f"  {hazard}" for hazard in hazards[:20])
        raise CleanupError(
            f"cleanup targets overlap protected Git worktrees; nothing moved:\n{sample}"
        )

    handles, warning = open_handles(sources)
    if warning:
        raise CleanupError(f"{warning}; refusing --apply")
    if handles:
        sample = "\n".join(
            f"  pid={item['pid']} command={item['command']} path={item['path']}"
            for item in handles[:20]
        )
        raise CleanupError(f"cleanup targets are in active use; nothing moved:\n{sample}")

    trash_root.mkdir(parents=True, exist_ok=True)
    source_devices = {path.lstat().st_dev for _, path, _, _ in found}
    if source_devices != {trash_root.stat().st_dev}:
        raise CleanupError(
            "Trash root is on another filesystem; refusing a non-atomic copy-and-delete move"
        )

    session = _session_dir(trash_root)
    moves = [
        PlannedMove(
            relative_path=target.relative_path,
            reason=target.reason,
            source=str(source),
            destination=str(session / target.relative_path),
            bytes=size,
            files=files,
        )
        for target, source, size, files in found
    ]
    manifest = session / "cleanup-manifest.json"
    _write_manifest(
        manifest,
        state="moving",
        repo=repo,
        head=_git(repo, "rev-parse", "HEAD"),
        moves=moves,
    )

    try:
        for move in moves:
            source = Path(move.source)
            destination = Path(move.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            move.status = "moved"
            _write_manifest(
                manifest,
                state="moving",
                repo=repo,
                head=_git(repo, "rev-parse", "HEAD"),
                moves=moves,
            )
    except (OSError, shutil.Error) as exc:
        _write_manifest(
            manifest,
            state="incomplete",
            repo=repo,
            head=_git(repo, "rev-parse", "HEAD"),
            moves=moves,
            error=str(exc),
        )
        raise CleanupError(
            f"cleanup stopped after a move failed; inspect {manifest}: {exc}"
        ) from exc

    for relative in EMPTY_SPIKE_PARENTS:
        with contextlib.suppress(OSError):
            (repo / relative).rmdir()

    _write_manifest(
        manifest,
        state="complete",
        repo=repo,
        head=_git(repo, "rev-parse", "HEAD"),
        moves=moves,
    )
    return session


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="worktree root (default: current directory)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move the allowlisted artifacts to Trash; default is dry-run",
    )
    parser.add_argument(
        "--trash-root",
        type=Path,
        help="override the OS Trash root (primarily for controlled/test environments)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo = validate_repo(args.repo)
        found = discover(repo)
        if not found:
            print("[OK] no known generated spike artifacts found")
            return 0

        total_bytes = sum(size for _, _, size, _ in found)
        total_files = sum(files for _, _, _, files in found)
        for target, _, size, files in found:
            print(
                f"[PLAN] {target.relative_path}: {human_bytes(size)}, "
                f"{files} files ({target.reason})"
            )
        print(
            f"[PLAN] total: {human_bytes(total_bytes)}, {total_files} files "
            f"across {len(found)} exact paths"
        )

        if not args.apply:
            print("[DRY RUN] nothing moved; re-run with --apply to quarantine these paths")
            return 0

        session = apply_cleanup(
            repo,
            args.trash_root if args.trash_root is not None else default_trash_root(),
            found,
        )
    except CleanupError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    print(f"[OK] moved {len(found)} paths to {session}")
    print(f"[OK] recovery manifest: {session / 'cleanup-manifest.json'}")
    print("[NOTE] disk space is reclaimed only when that Trash item is emptied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
