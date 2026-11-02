"""Run the PAIR-arrow browser spec on its dedicated fixture (Codex P1, #6062).

Portable on macOS, Linux and Windows: no shell, and every tool is resolved by
path before it is launched. The fixture builder runs through this checkout's
uv environment; pnpm and uv are found on PATH and launched by full path.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "apps" / "webui" / "frontend"
DATA_DIR = FRONTEND / "tests" / "e2e" / "fixtures" / "pairing-arrow-data"
MANIFEST = DATA_DIR / "fixture-manifest.json"
CONFIG = "tests/e2e/playwright.pairing-arrow.config.ts"


def _tool(name: str) -> str:
    """The full path of an executable on PATH; refuses to guess when it is missing."""
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"[ERROR] {name} is not on PATH; install it and retry")
    return found


def _run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def main() -> int:
    uv = _tool("uv")
    pnpm = _tool("pnpm")
    _run([pnpm, "exec", "svelte-kit", "sync"], FRONTEND)
    _run(
        [
            uv,
            "run",
            "--no-sync",
            "python",
            "-m",
            "apps.webui.frontend.tests.e2e.support.deckload_fixture",
            "--data-dir",
            str(DATA_DIR),
            "--seed-pairing-arrow",
            "--manifest",
            str(MANIFEST),
        ],
        REPO_ROOT,
    )
    _run([pnpm, "exec", "playwright", "test", "--config", CONFIG], FRONTEND)
    return 0


if __name__ == "__main__":
    sys.exit(main())
