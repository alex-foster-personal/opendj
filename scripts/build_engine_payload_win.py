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
from scripts import payload_launchers_win, payload_pe_audit
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
from scripts.payload_vcruntime import stage_vcruntime
from scripts.payload_windows_extras import ExtrasResult, apply_extras_to_manifest, stage_extras

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
STANDING_GAPS: tuple[str, ...] = ("ffmpeg", "waveform_native")

SETUPTOOLS_STUB_REASON: str = (
    "setuptools entry-point launcher stubs for 32-bit and arm64 targets: data it copies into "
    "generated scripts, never executed from here"
)

#: Audit findings tolerated on purpose: payload-relative glob -> reason.
AUDIT_ACCEPTED: dict[str, str] = {
    "pylib/numba/np/ufunc/tbbpool*.pyd": (
        "numba's optional TBB threading layer imports tbb12.dll only when the tbb layer is selected; "
        "the tbb distribution is not in the closure and the default layers (omp, workqueue) do not use it"
    ),
    "*/setuptools/cli*.exe": SETUPTOOLS_STUB_REASON,
    "*/setuptools/gui*.exe": SETUPTOOLS_STUB_REASON,
}


def standing_not_staged() -> dict[str, str]:
    """Owner-decision gaps extras never stages: ffmpeg and the waveform native wheel."""
    return {key: NOT_STAGED[key] for key in STANDING_GAPS}


#: Removed from the staged Windows CPython, as globs relative to its root. The
#: daemon never installs packages, and pip vendors distlib's 32-bit and arm64
#: launcher stubs (t32.exe, w64-arm.exe, ...), which the PE audit rightly
#: refuses as non-x64. Mirrors the pip entries of RUNTIME_PRUNE_RELATIVE.
WINDOWS_RUNTIME_PRUNE_RELATIVE: tuple[str, ...] = (
    "Scripts/pip*",
    "Lib/site-packages/pip",
    "Lib/site-packages/pip-*.dist-info",
    "Lib/ensurepip",
)


def stage_windows_runtime(runtime_source: Path, destination: Path) -> list[str]:
    """Copy the relocatable CPython, which keeps ``python.exe`` at its root, minus pip and bytecode."""
    if not (runtime_source / "python.exe").is_file():
        raise PayloadBuildError(
            f"{runtime_source} does not look like a Windows python-build-standalone install (no python.exe at its root)"
        )
    shutil.copytree(runtime_source, destination, symlinks=False)
    pruned: list[str] = []
    for relative in WINDOWS_RUNTIME_PRUNE_RELATIVE:
        for target in sorted(destination.glob(relative)):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            pruned.append(target.relative_to(destination).as_posix())
    _prune_pycache(destination)
    return pruned


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
    not_staged: Mapping[str, str] | None = None,
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
        "not_staged": dict(NOT_STAGED if not_staged is None else not_staged),
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


def audit_built_payload(payload_dir: Path) -> dict[str, object]:
    try:
        return payload_pe_audit.audit_payload(payload_dir, AUDIT_ACCEPTED, vc_runtime_must_ship=True)
    except payload_pe_audit.PeAuditError as exc:
        raise PayloadBuildError(str(exc)) from exc


def _attach_pe_audit(manifest: dict[str, object], payload_dir: Path) -> dict[str, object]:
    verification = dict(manifest["verification"]) if isinstance(manifest.get("verification"), dict) else {}
    verification["pe_audit"] = audit_built_payload(payload_dir)
    return {**manifest, "verification": verification}


def _maybe_refresh_digests(payload_dir: Path, manifest: dict[str, object]) -> dict[str, object]:
    if "sha256" not in manifest and "bytes" not in manifest:
        return manifest
    from scripts.payload_windows_extras import refresh_digests

    return refresh_digests(payload_dir, manifest)


def refresh_manifest_after_vcruntime(payload_dir: Path, vcruntime: dict[str, object]) -> dict[str, object]:
    """Fold a re-staged VC++ runtime into an existing manifest (vcruntime block and PE audit)."""
    manifest_path = payload_dir / MANIFEST_NAME
    current = {**json.loads(manifest_path.read_text(encoding="utf-8")), "vcruntime": vcruntime}
    manifest = _attach_pe_audit(_maybe_refresh_digests(payload_dir, current), payload_dir)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def refresh_manifest_after_extras(payload_dir: Path, extras: ExtrasResult) -> dict[str, object]:
    """Fold re-staged extras into an existing manifest (blocks, not_staged, PE audit)."""
    manifest_path = payload_dir / MANIFEST_NAME
    current = apply_extras_to_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8")), extras, remaining_gaps=standing_not_staged()
    )
    manifest = _attach_pe_audit(_maybe_refresh_digests(payload_dir, current), payload_dir)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build_windows_payload(
    *,
    repo_root: Path,
    output_dir: Path,
    runtime_source: Path,
    vc_redist_dir: Path | None = None,
    allow_missing_secrets: bool = False,
) -> dict[str, object]:
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
    vcruntime = stage_vcruntime(output_dir, vc_redist_dir)
    extras = stage_extras(repo_root, output_dir, allow_missing_secrets=allow_missing_secrets)
    pe_audit = audit_built_payload(output_dir)

    manifest = apply_extras_to_manifest(
        windows_manifest(
            identity={
                "product_name": app.product_name,
                "bundle_identifier": app.identifier,
                "app_version": app.version,
            },
            python_version=python_version,
            audio_engine_hello=hello,
            git=git_identity(repo_root),
        ),
        extras,
        remaining_gaps=standing_not_staged(),
    )
    manifest["vcruntime"] = vcruntime
    manifest["verification"] = {"pe_audit": pe_audit}
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
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--runtime-source", type=Path, default=None)
    parser.add_argument(
        "--vc-redist-dir",
        type=Path,
        default=None,
        help="a VC/Redist/MSVC directory; default: found through vswhere",
    )
    parser.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="dev build: a missing OAuth client or ship DSN is recorded under not_staged, not an error",
    )
    parser.add_argument("--audit-only", type=Path, default=None, metavar="PAYLOAD_DIR")
    parser.add_argument("--stage-vcruntime-only", type=Path, default=None, metavar="PAYLOAD_DIR")
    parser.add_argument("--stage-extras-only", type=Path, default=None, metavar="PAYLOAD_DIR")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        if args.audit_only is not None:
            print(json.dumps(audit_built_payload(args.audit_only.resolve()), indent=2, sort_keys=True))
            return 0
        if args.stage_vcruntime_only is not None:
            payload = args.stage_vcruntime_only.resolve()
            staged_block = stage_vcruntime(payload, args.vc_redist_dir)
            manifest = refresh_manifest_after_vcruntime(payload, staged_block)
            print(
                json.dumps(
                    {"vcruntime": staged_block, "pe_audit": manifest["verification"]["pe_audit"]},  # type: ignore[index]
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.stage_extras_only is not None:
            payload = args.stage_extras_only.resolve()
            payload_launchers_win.write_windows_launchers(payload)
            extras = stage_extras(repo_root, payload, allow_missing_secrets=args.allow_missing_secrets)
            manifest = refresh_manifest_after_extras(payload, extras)
            print(
                json.dumps(
                    {
                        "not_staged": manifest["not_staged"],
                        "third_party_licenses": manifest["third_party_licenses"],
                        "beatgrid_runner": manifest["beatgrid_runner"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.out is None:
            parser.error("--out is required for a full build")
        manifest = build_windows_payload(
            repo_root=repo_root,
            output_dir=args.out.resolve(),
            runtime_source=(args.runtime_source or default_windows_runtime_source()).resolve(),
            vc_redist_dir=args.vc_redist_dir,
            allow_missing_secrets=args.allow_missing_secrets,
        )
    except PayloadBuildError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    proof = manifest["boot_proof"]
    print(f"[OK] payload: {args.out}")
    print(f"[OK] python {manifest['runtime']['python']}; engine answered health on port {proof['port']} (lock holder pid {proof['lock_holder_pid']})")
    print(f"[WARN] not staged: {', '.join(sorted(manifest['not_staged']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
