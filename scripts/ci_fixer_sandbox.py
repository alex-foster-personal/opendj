"""Credential-free verification sandbox for PR-controlled ci-fixer commands."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scripts import ci_fixer_core as cf
from scripts.ci_health_core import PreconditionError

VERIFY_WORKTREE_PATH = "/workspace"
VERIFY_HOME = "/tmp/ci-fixer-home"
VERIFY_PATH = "/workspace/.venv/bin:/usr/local/bin:/usr/bin:/bin"


def run_recheck(
    command: list[str],
    worktree_dir: Path,
    working_directory: Path,
    timeout_seconds: float = cf.RECHECK_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run an Actions command with no network, credentials, or host home mounted."""
    cwd = (worktree_dir / working_directory).resolve()
    if not cwd.is_relative_to(worktree_dir.resolve()):
        raise PreconditionError(
            f"working directory escapes the worker worktree: {working_directory}"
        )
    if not cwd.is_dir():
        raise PreconditionError(
            f"working directory does not exist in worker worktree: {working_directory}"
        )
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise PreconditionError("bwrap is required for credential-free rechecks")
    return subprocess.run(
        _sandboxed_command(bwrap, command, worktree_dir, working_directory),
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        env={"HOME": VERIFY_HOME, "PATH": VERIFY_PATH, "LANG": "C.UTF-8"},
    )


def _sandboxed_command(
    bwrap: str, command: list[str], worktree_dir: Path, working_directory: Path
) -> list[str]:
    """Expose only the disposable worktree and read-only system runtime files."""
    return [
        bwrap,
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--bind",
        str(worktree_dir.resolve()),
        VERIFY_WORKTREE_PATH,
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        str(Path(VERIFY_WORKTREE_PATH) / working_directory),
        "--setenv",
        "HOME",
        VERIFY_HOME,
        "--setenv",
        "PATH",
        VERIFY_PATH,
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--",
        *command,
    ]
