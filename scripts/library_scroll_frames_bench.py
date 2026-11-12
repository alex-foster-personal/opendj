"""Library scroll frame bench runner (PERF-CAPTURE-04, #10230, Codex P1 on #10591).

Portable on macOS, Linux and Windows: no shell, pnpm resolved by full path.
Runs svelte-kit sync, production build, then the bench spec from
apps/webui/frontend.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "apps" / "webui" / "frontend"
CONFIG = "tests/e2e/playwright.playlist-switch-latency.config.ts"
SPEC = "library-scroll-frames.spec.ts"


def _tool(name: str) -> str:
    """The full path of an executable on PATH; refuses to guess when it is missing."""
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"[ERROR] {name} is not on PATH; install it and retry")
    return found


def steps(pnpm: str, extra: list[str]) -> list[list[str]]:
    """The argv of each step, in order: sync, production build, then the bench spec."""
    return [
        [pnpm, "exec", "svelte-kit", "sync"],
        [pnpm, "build"],
        [pnpm, "exec", "playwright", "test", "--config", CONFIG, SPEC, *extra],
    ]


def main(argv: list[str] | None = None) -> int:
    extra = list(sys.argv[1:] if argv is None else argv)
    pnpm = _tool("pnpm")
    for step in steps(pnpm, extra):
        code = subprocess.run(step, cwd=FRONTEND, check=False).returncode
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
