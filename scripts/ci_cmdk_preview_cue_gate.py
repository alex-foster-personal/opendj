"""Run the Cmd K cue-preview browser gate on a CI runner (CMDK-07..08).

The step used to be shell: an inline ``export``, a subshell, and bare ``gcc``
and ``cargo`` names. This helper resolves every program it launches by path,
passes arguments as lists, and fails with the reason, so the step means the
same on any platform the runner has. The wrappers it calls (the cargo
self-heal, the host lock and the port reaper) stay where they are.

Usage (from the workflow, with the repository as the working directory)::

    python3 scripts/ci_cmdk_preview_cue_gate.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[1]
FRONTEND_ROOT: Path = REPOSITORY_ROOT / "apps" / "webui" / "frontend"
AUDIO_ENGINE_MANIFEST: str = "apps/audio-engine/Cargo.toml"
CUE_PREVIEW_PORTS: tuple[str, ...] = ("5337", "8717")
CUE_PREVIEW_CONFIG: str = "tests/e2e/playwright.cmdk-preview-cue.config.ts"
CUE_PREVIEW_OUTPUT: str = "test-results/cmdk-preview-cue"


def resolve(name: str) -> str:
    """The absolute path of a program on PATH, or SystemExit naming it."""
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"[ERROR] {name} is not on PATH; the cue-preview gate cannot run without it")
    return found


def run(argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(argv, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"[ERROR] {argv[0]} exited {result.returncode}")


def bash_run(script: Path, *args: str) -> list[str]:
    """A wrapper script launched through the bash found on PATH, not by its own mode or shebang.

    The wrappers are bash, so bash is the one shell this gate names. A runner without it fails here,
    with the reason, rather than with a bare "not executable" from the launch.
    """
    return [resolve("bash"), str(script), *args]


def bindgen_include(gcc: str) -> str:
    """The compiler's own include directory, which bindgen needs for the engine's C headers."""
    result = subprocess.run([gcc, "-print-file-name=include"], capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit(f"[ERROR] {gcc} did not report its include directory")
    return result.stdout.strip()


def main() -> int:
    env = dict(os.environ)
    env["BINDGEN_EXTRA_CLANG_ARGS"] = f"-I{bindgen_include(resolve('gcc'))}"
    cargo = resolve("cargo")
    selfheal = REPOSITORY_ROOT / "scripts" / "ci_cargo_selfheal.sh"
    run(
        bash_run(selfheal, AUDIO_ENGINE_MANIFEST, "release", cargo, "build", "--release", "--locked", "--manifest-path", AUDIO_ENGINE_MANIFEST),
        REPOSITORY_ROOT,
        env,
    )
    lock = REPOSITORY_ROOT / "scripts" / "ci_host_lock.sh"
    reaper = REPOSITORY_ROOT / "scripts" / "ci_reap_port_holders.sh"
    pnpm = resolve("pnpm")
    run(
        bash_run(
            lock, "cmdk-preview-cue",
            str(reaper), *CUE_PREVIEW_PORTS, "--",
            pnpm, "exec", "playwright", "test", "--config", CUE_PREVIEW_CONFIG, "--output", CUE_PREVIEW_OUTPUT,
        ),
        FRONTEND_ROOT,
        env,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
