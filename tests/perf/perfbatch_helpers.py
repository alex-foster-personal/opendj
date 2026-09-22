"""Shared fixtures for the PERFBATCH gate tests (path-level and hunk-level).

Every diff the tests classify comes out of a real ``git diff`` in a disposable
repository, through the production ``--diff`` path. Nothing here fabricates
protocol output.
"""

from __future__ import annotations

import io
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.perf import perfbatch_gate as mod

IDLE = "perfbatch: pipelines-idle jobs.db empty on builder 2026-09-11T07:06:49+00:00"
QUALITY = (
    "perfbatch-03: quality-ok reqs=PERFBATCH-01 "
    "signal=scripts/bench/judgement-calibration.json "
    "before_after=docs/research/local-stems-20260908-nucbox-ledger.md"
)

LOCAL_WORKER = "scripts/stems_local_worker.py"
STEMS_JOB = "apps/stems/job.py"
LYRICS_FETCH = "apps/webui/frontend/src/lib/components/rb/wave/lyrics-fetch.svelte.ts"
FARM_WATCH = "scripts/stem_farm_watch_pull_down.sh"

# The shape of scripts/stems_local_worker.py around run(), and the exact edit
# PR #3331 (branch af--windows-b0) made to it. Real git produces the hunk.
WORKER_BEFORE = '''"""Local stems worker."""

import contextlib
import os

NICE_LEVEL = 10


def run(
    data_dir: str,
    track_timeout_s: float = 3600.0,
) -> int:
    """Separate every track locally, skipping fresh bundles."""
    with contextlib.suppress(OSError):
        os.nice(NICE_LEVEL)

    root = data_dir
    return 0 if root else 1
'''
WORKER_AFTER = WORKER_BEFORE.replace(
    "    with contextlib.suppress(OSError):\n",
    "    # os.nice does not exist on Windows at all (AttributeError, not OSError) -\n"
    "    # guard both: absent priority-lowering is a no-op, never a crash.\n"
    "    with contextlib.suppress(OSError, AttributeError):\n",
)


class GateRepo:
    """A disposable git repository whose working-tree diff feeds ``perfbatch_gate --diff``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "gate@example.com")
        self.git("config", "user.name", "gate")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return proc.stdout

    def write(self, path: str, content: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8", newline="\n")

    def seed(self, files: dict[str, str | bytes]) -> None:
        """Commit ``files`` as the base and point origin/main at it."""
        for path, content in files.items():
            self.write(path, content)
        self.git("add", "-A")
        # --allow-empty: re-seeding the content HEAD already holds is a valid base.
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def edit(self, path: str, before: str | bytes, after: str | bytes) -> None:
        """Seed ``before`` as the base, then leave ``after`` in the working tree."""
        self.seed({path: before})
        self.write(path, after)

    def make_executable(self, path: str) -> None:
        """Flip the executable bit in the index (and on disk where the OS has one)."""
        target = self.root / path
        os.chmod(target, target.stat().st_mode | 0o111)
        self.git("update-index", "--chmod=+x", path)
        self.git("commit", "-q", "-m", "mode")

    def names(self) -> list[str]:
        return mod.local_diff_names(self.root)

    def diff(self) -> str:
        return mod.local_diff_text(self.names(), self.root)

    def gate(self, body: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(["--diff"], repo_root=self.root, body=body)
        return rc, out.getvalue(), err.getvalue()
