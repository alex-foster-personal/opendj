"""Stage the Open DJ engine payload on Windows, and prove it boots.

The macOS builder (``build_engine_payload.py``) is the contract; this builds
the same directory shape for Windows: ``runtime\\python.exe`` (a relocatable
python-build-standalone CPython), ``pylib`` (the locked closure), ``app``
(engine source and the built SPA), ``bin\\odj-audio.exe`` (the Rust engine) and
the ``.cmd`` launchers from ``payload_launchers_win``. It reuses the macOS
builder's helpers wherever they are platform-neutral and adds a Windows arm
only where they are not.

A payload is not done when it is staged. ``boot_proof`` starts
``bin\\opendj-engine.cmd`` exactly as the Electron shell does and waits for
``/api/v1/health``; the build fails if that does not answer.

What it does NOT stage is written into the manifest as ``not_staged`` with a
reason, so a reader of a payload sees the gaps instead of inferring them.

Needs: a Windows host, uv, cargo with MSVC and libclang (``LIBCLANG_PATH``; the
PyPI ``libclang`` wheel's ``clang\\native`` folder works), and a frontend built
with ``cd apps/webui/frontend && pnpm run build``.

[if] the stager runs off Windows, lacks python.exe, or the staged engine does not answer health [then] it stops with the reason, never a payload that looks complete, [else stop]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from apps.engine_core.build_info import REQUIRED_IDENTITY_KEYS
from scripts import payload_launchers_win
from scripts.app_identity import read_app_identity
from scripts.build_engine_payload import (
    AUDIO_ENGINE_CRATE,
    PayloadBuildError,
    _interpreter_version,
    _prune_pycache,
    assert_runtime_matches_pin,
    assert_spa_is_fresh,
    git_identity,
    install_dependencies,
    locked_requirements,
    read_python_pin,
    resolve_program,
    stage_app_source,
    verify_audio_engine,
)

RUNTIME_PYTHON_RELATIVE: str = "runtime/python.exe"
AUDIO_ENGINE_WIN_RELATIVE: str = "bin/odj-audio.exe"
MANIFEST_NAME: str = "manifest.json"
BOOT_TIMEOUT_S: float = 90.0
HEALTH_PATH: str = "/api/v1/health"
#: What the engine must not inherit from the build terminal, same list as the Electron shell strips.
STRIPPED_ENV: tuple[str, ...] = (
    "MDT_DATA_DIR",
    "MDT_REKORDBOX_WRITEBACK_ENABLED",
    "MUSIC_DJ_STATE_BACKEND",
    "WEB_CONCURRENCY",
    "ODJ_AUDIO_BIN",
)

NOT_STAGED: dict[str, str] = {
    "ffmpeg": "no pinned LGPL ffmpeg build for Windows exists yet; ODJ_FFMPEG_BIN points at a file that is absent",
    "beatgrid_runner": "the Beat This! runner site and checkpoint are not staged for Windows; own-beatgrid backfill will refuse",
    "waveform_native": "the maturin waveform wheel is not built for Windows; the engine falls back to Python/NumPy and says so",
    "google_oauth": "the baked Desktop client is a signing-time input; sign-in is unavailable in this payload",
    "telemetry": "the baked ship Sentry DSN is a signing-time input; telemetry is off",
    "third_party_licenses": "the license inventory step is macOS-shaped and has not been run for the Windows payload",
}


def stage_windows_runtime(runtime_source: Path, destination: Path) -> None:
    """Copy the relocatable CPython, which keeps ``python.exe`` at its root, minus bytecode."""
    if not (runtime_source / "python.exe").is_file():
        raise PayloadBuildError(
            f"{runtime_source} does not look like a Windows python-build-standalone install (no python.exe at its root)"
        )
    shutil.copytree(runtime_source, destination, symlinks=False)
    _prune_pycache(destination)


def stage_windows_audio_engine(repo_root: Path, payload_dir: Path) -> dict[str, object]:
    """Build ``odj-audio.exe`` with device output, stage it, and read its hello."""
    cargo = resolve_program("cargo", "cannot build the audio engine")
    crate = repo_root / AUDIO_ENGINE_CRATE
    target_dir = crate / "target"
    result = subprocess.run(
        [
            cargo, "build", "--release", "--locked", "--features", "device", "--bin", "odj-audio",
            "--manifest-path", str(crate / "Cargo.toml"), "--target-dir", str(target_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(
            "cargo build of odj-audio failed (is LIBCLANG_PATH set for bindgen?):\n" + result.stderr[-3000:]
        )
    staged = payload_dir / AUDIO_ENGINE_WIN_RELATIVE
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target_dir / "release" / "odj-audio.exe", staged)
    return verify_audio_engine(staged)


def windows_system_tool(
    name: str,
    env: Mapping[str, str] = os.environ,
    exists: Callable[[str], bool] = os.path.isfile,
) -> str:
    """A System32 tool under the root the OS sets, never a PATH lookup (see electron platform.ts)."""
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or env.get("windir") or env.get("WINDIR")
    if not root or not PureWindowsPath(root).is_absolute():
        raise PayloadBuildError(f"cannot resolve {name}: no absolute SystemRoot or windir")
    tool = str(PureWindowsPath(root) / "System32" / name)
    if not exists(tool):
        raise PayloadBuildError(f"{tool} is missing")
    return tool


def windows_manifest(
    *,
    identity: dict[str, object],
    python_version: str,
    audio_engine_hello: dict[str, object],
    git: dict[str, object],
) -> dict[str, object]:
    """The payload manifest: identity the engine reads, plus an honest list of what is missing."""
    stamped = {
        **identity,
        "engine_version": identity.get("app_version"),
        "lane_label": "",
        "built_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        **git,
    }
    missing = [key for key in REQUIRED_IDENTITY_KEYS if key not in stamped]
    if missing:
        raise PayloadBuildError(f"the manifest identity lacks {missing}, so the engine would refuse it")
    return {
        "kind": "opendj-engine-payload",
        "schema": 1,
        "platform": "win32",
        "identity": stamped,
        "runtime": {"python": python_version},
        "audio_engine": audio_engine_hello,
        "not_staged": dict(NOT_STAGED),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health_status(port: int) -> int | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=2) as response:
            return int(response.status)
    except (urllib.error.URLError, OSError):
        return None


def boot_proof(payload_dir: Path, data_dir: Path, timeout_s: float = BOOT_TIMEOUT_S) -> dict[str, object]:
    """Start the staged engine the way the Electron shell does and wait for health."""
    launcher = payload_dir / payload_launchers_win.LAUNCHER_RELATIVE
    if not launcher.is_file():
        raise PayloadBuildError(f"{launcher} is missing; nothing to boot")
    cmd = windows_system_tool("cmd.exe")
    taskkill = windows_system_tool("taskkill.exe")
    data_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    log = data_dir / "boot.log"
    env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
    # One string, not a list: cmd.exe /s needs the quotes verbatim (see apps/desktop/electron/src/platform.ts).
    command = f'"{cmd}" /d /s /c ""{launcher}" --data-dir "{data_dir}" --host 127.0.0.1 --port {port}"'
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            command, stdout=sink, stderr=subprocess.STDOUT, env=env, creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            deadline = time.monotonic() + timeout_s
            status: int | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                status = _health_status(port)
                if status == 200:
                    break
                time.sleep(0.3)
            holder_pid = _lock_holder_pid(data_dir)
        finally:
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            proc.wait(timeout=30)
    if status != 200:
        tail = log.read_text(encoding="utf-8", errors="replace")[-2500:]
        raise PayloadBuildError(f"the staged engine did not answer {HEALTH_PATH} (last status {status}):\n{tail}")
    return {"port": port, "health_status": status, "lock_holder_pid": holder_pid, "boot_log": str(log)}


def _lock_holder_pid(data_dir: Path) -> int:
    lock = data_dir / ".engine.lock"
    try:
        return int(json.loads(lock.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError):
        return 0


def build_windows_payload(*, repo_root: Path, output_dir: Path, runtime_source: Path) -> dict[str, object]:
    if sys.platform != "win32":
        raise PayloadBuildError("the Windows payload is staged on a Windows host: the dependency wheels are chosen for the host")
    app = read_app_identity(repo_root)
    build_dir = assert_spa_is_fresh(repo_root / "apps/webui/frontend")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    stage_windows_runtime(runtime_source, output_dir / "runtime")
    python = output_dir / RUNTIME_PYTHON_RELATIVE
    python_version = _interpreter_version(python)
    assert_runtime_matches_pin(
        runtime_version=python_version, pin=read_python_pin(repo_root), runtime_source=runtime_source
    )
    locked, _orphaned = locked_requirements(repo_root)
    install_dependencies(locked, python, output_dir / "pylib")
    stage_app_source(repo_root, output_dir / "app", build_dir)
    payload_launchers_win.write_windows_launchers(output_dir)
    hello = stage_windows_audio_engine(repo_root, output_dir)

    manifest = windows_manifest(
        identity={
            "product_name": app.product_name,
            "bundle_identifier": app.identifier,
            "app_version": app.version,
        },
        python_version=python_version,
        audio_engine_hello=hello,
        git=git_identity(repo_root),
    )
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["boot_proof"] = boot_proof(output_dir, output_dir.parent / f"{output_dir.name}-boot-data")
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def default_windows_runtime_source() -> Path:
    """The uv-managed CPython the repo venv is built on (its base prefix), a relocatable install."""
    uv = resolve_program("uv", "cannot resolve the repo interpreter")
    result = subprocess.run(
        [uv, "run", "--no-sync", "python", "-c", "import sys; print(sys.base_prefix)"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        raise PayloadBuildError(f"cannot resolve the repo interpreter through uv: {result.stderr}")
    return Path(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        manifest = build_windows_payload(
            repo_root=args.repo_root.resolve(),
            output_dir=args.out.resolve(),
            runtime_source=(args.runtime_source or default_windows_runtime_source()).resolve(),
        )
    except PayloadBuildError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    proof = manifest["boot_proof"]
    print(f"[OK] payload: {args.out}")
    print(f"[OK] python {manifest['runtime']['python']}; engine answered health on port {proof['port']} (lock holder pid {proof['lock_holder_pid']})")
    print(f"[WARN] not staged: {', '.join(sorted(NOT_STAGED))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
