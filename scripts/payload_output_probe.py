"""Stage the CoreAudio output-health helper ``bin/odj-output-probe`` (ELECTRON-6272).

The Electron shell does not link CoreAudio. On macOS it answers
``/api/v1/audio/output-health`` and ``/api/v1/audio/switch-output`` by running
``<payload>/bin/odj-output-probe probe|switch``
(``apps/desktop/electron/src/output-health.ts``), and without the helper it
can only answer the ADR-0125 ``unknown`` verdict. The helper is the
``odj-output-probe`` bin target of the Tauri shell crate
(``apps/desktop/src-tauri/src/bin/odj-output-probe.rs``), which includes
``output_health.rs`` in place.

macOS only: the helper exists to call CoreAudio, and the Electron shell
answers ``unknown`` off darwin without looking for it. Off darwin the build
records why it was skipped instead of staging nothing silently.

Kept out of scripts/build_engine_payload.py so that module stays within the
file-size ratchet, like scripts/payload_ffmpeg.py.

-Claude
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

OUTPUT_PROBE_RELATIVE: str = "bin/odj-output-probe"
OUTPUT_PROBE_BIN: str = "odj-output-probe"
OUTPUT_PROBE_CRATE: str = "apps/desktop/src-tauri"

# The crate has no Cargo features to narrow the build, and its build.rs runs
# tauri-build for every bin target. tauri-build copies every file under the
# ``bundle.resources`` map (``payload``) into the target dir and fails when
# ``apps/desktop/src-tauri/payload`` is absent, which it is when the payload is
# staged elsewhere (the Electron stage, apps/desktop/electron/out/payload-stage).
# The helper reads no resources, so this override (a JSON merge patch
# tauri-build applies from TAURI_CONFIG) empties the list for this build only:
# no dependence on that directory and no copy of a half-built payload.
NO_RESOURCES_TAURI_CONFIG: str = json.dumps({"bundle": {"resources": []}})

# What the helper prints for an argv it does not know, before it touches any
# device (odj-output-probe.rs ``USAGE``). The gate below reads it to prove the
# staged file is this helper and that it runs.
USAGE_MARKER: str = "usage: odj-output-probe"
USAGE_EXIT_CODE: int = 2


def builds_output_probe(platform: str) -> bool:
    """Whether this host builds and stages the helper (darwin only)."""
    return platform == "darwin"


def skip_reason(platform: str) -> str:
    return (
        f"{OUTPUT_PROBE_RELATIVE} is macOS-only (it calls CoreAudio); not built on "
        f"{platform}, where the Electron shell answers output health 'unknown' "
        "without it"
    )


def cargo_command(cargo: str, crate: Path, target_dir: Path) -> list[str]:
    """``cargo`` is the program ``shutil.which`` resolved: the one validated is the one run."""
    return [
        cargo,
        "build",
        "--release",
        "--locked",
        "--bin",
        OUTPUT_PROBE_BIN,
        "--manifest-path",
        str(crate / "Cargo.toml"),
        "--target-dir",
        str(target_dir),
        "--message-format=json-render-diagnostics",
    ]


def built_executable(cargo_stdout: str) -> Path:
    """The helper's path as cargo reports it, wherever the target triple put it.

    A ``CARGO_BUILD_TARGET`` or ``build.target`` config writes the binary under
    ``target/<triple>/release`` instead of ``target/release``; cargo's own
    ``compiler-artifact`` message names the file it wrote in every case.
    """
    from scripts.build_engine_payload import PayloadBuildError

    for line in cargo_stdout.splitlines():
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("reason") != "compiler-artifact":
            continue
        if message.get("target", {}).get("name") != OUTPUT_PROBE_BIN:
            continue
        executable = message.get("executable")
        if isinstance(executable, str) and executable:
            return Path(executable)
    raise PayloadBuildError(f"cargo reported no executable for {OUTPUT_PROBE_BIN}; nothing to stage")


def stage_output_probe(repo_root: Path, payload_dir: Path, platform: str = sys.platform) -> dict[str, object]:
    """Build the helper and stage it executable at ``bin/odj-output-probe``.

    Returns the manifest block: ``staged`` true with the path, or false with
    the reason it was skipped.
    """
    from scripts.build_engine_payload import PayloadBuildError

    if not builds_output_probe(platform):
        reason = skip_reason(platform)
        print(f"[SKIP] {reason}", file=sys.stderr)
        return {"staged": False, "path": OUTPUT_PROBE_RELATIVE, "reason": reason}
    cargo = shutil.which("cargo")
    if cargo is None:
        raise PayloadBuildError(f"cargo is not on PATH; cannot build {OUTPUT_PROBE_BIN}")
    crate = repo_root / OUTPUT_PROBE_CRATE
    target_dir = crate / "target"
    result = subprocess.run(
        cargo_command(cargo, crate, target_dir),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "TAURI_CONFIG": NO_RESOURCES_TAURI_CONFIG},
    )
    if result.returncode != 0:
        raise PayloadBuildError(f"cargo build of {OUTPUT_PROBE_BIN} failed:\n{result.stderr}")
    staged = payload_dir / OUTPUT_PROBE_RELATIVE
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built_executable(result.stdout), staged)
    staged.chmod(0o755)
    verify_output_probe(staged)
    return {"staged": True, "path": OUTPUT_PROBE_RELATIVE}


def verify_output_probe(binary: Path) -> None:
    """Run the staged helper with no argv and read its usage error.

    No argv is a usage error that exits 2 without opening a device, so this
    needs no output device on the build machine and still proves the staged
    file is the helper and that it runs.
    """
    from scripts.build_engine_payload import PayloadBuildError

    try:
        result = subprocess.run(
            [str(binary)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PayloadBuildError(f"{binary} did not answer a usage error within 30 s") from exc
    except OSError as exc:
        raise PayloadBuildError(f"{binary} could not be run: {exc}") from exc
    if result.returncode != USAGE_EXIT_CODE or USAGE_MARKER not in result.stderr:
        raise PayloadBuildError(
            f"{binary} is not the output probe helper: with no argv it exited "
            f"{result.returncode} (want {USAGE_EXIT_CODE}) with stderr "
            f"{result.stderr[:300]!r} (want {USAGE_MARKER!r})"
        )
