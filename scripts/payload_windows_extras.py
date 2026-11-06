"""Windows (win32-x64) counterparts of the macOS payload's beatgrid runner, OAuth, telemetry and licenses.

scripts/build_engine_payload_win.py calls :func:`stage_extras` after the audio engine and the VC++
runtime are staged. Everything platform-neutral is IMPORTED from the macOS side, not forked:

- beatgrid: scripts.payload_beatgrid (runner closure export, verified checkpoint cache, notice) and
  build_engine_payload.install_dependencies. Only the launcher is Windows-specific (a .cmd), and the torch
  wheels: the lock pins the win32 closure to the cu126 index (2.6 GB, CUDA), while the runner is always
  started with --device cpu (own_beatgrid.DEFAULT_DEVICE), so the local version is stripped and PyPI's
  CPU-only win_amd64 wheel is installed. The staged build is proven CUDA-free, not assumed.
- OAuth and telemetry: scripts.payload_google_oauth / scripts.payload_telemetry bake the same files at the
  same payload-relative paths. A release build without an input raises; ``allow_missing`` (dev builds)
  records the reason under manifest["not_staged"] instead. Values are never fabricated or printed.
- licenses: scripts.third_party_licenses with the x86_64-pc-windows-msvc Rust target.

Requirements:
- ☑️✅ the beatgrid runner site, checkpoint, MIT notice and launcher are staged and re-derived from bytes
  [if] the site, checkpoint or launcher is missing [then] PayloadBuildError naming it
  [if] the checkpoint digest differs from the pinned one [then] PayloadBuildError
  [if] the staged torch is a CUDA build [then] PayloadBuildError
- ☑️✅ OAuth and telemetry are required for a release build
  [if] an input is missing and allow_missing is false [then] PayloadBuildError naming the env var
  [if] an input is missing and allow_missing is true [then] manifest["not_staged"] carries the reason
- ☑️✅ third-party licenses are inventoried for the Windows payload and verified by parsing
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.hashing import sha256_file_hex
from scripts import license_verify, payload_beatgrid, payload_google_oauth, payload_telemetry, third_party_licenses
from scripts.build_engine_payload import (
    PayloadBuildError,
    install_dependencies,
    parse_locked_export,
    sha256_tree,
    tree_bytes,
)

WINDOWS_RUST_TARGET_TRIPLE: str = "x86_64-pc-windows-msvc"
BEATGRID_LAUNCHER_RELATIVE: Path = payload_beatgrid.RUNNER_LAUNCHER_RELATIVE.with_suffix(".cmd")
RUNTIME_RELATIVE: str = "runtime/python.exe"

_CUDA_OR_CPU_LOCAL = re.compile(r"^(?P<head>(?:torch|torchaudio)==[0-9][^\s+;]*)\+(?:cu\d+|cpu)(?P<tail>\s*(?:;.*)?)$")

_RUNNER_PROBE = """
import importlib.metadata as md, importlib.util, json, sys
import beat_this.inference, torch, torchaudio, soundfile, numpy  # noqa: F401
names = sys.argv[1].split(',')
print(json.dumps({
    'versions': {n: md.version(n) for n in names},
    'apps_visible': importlib.util.find_spec('apps') is not None,
    'torch_cuda': torch.version.cuda,
}))
"""


@dataclass(frozen=True)
class ExtrasResult:
    """Manifest blocks for what was staged, and a reason for each piece that was not."""

    manifest_blocks: dict[str, object]
    not_staged: dict[str, str] = field(default_factory=dict)


def batch_header(title: str) -> str:
    return (
        "@echo off\r\n"
        f"rem {title}\r\n"
        "rem Written by scripts/build_engine_payload_win.py. Every line either works or\r\n"
        "rem stops the daemon (errorlevel is returned as-is): no fallback paths.\r\n"
        "setlocal EnableExtensions DisableDelayedExpansion\r\n"
        'for %%I in ("%~dp0..") do set "payload=%%~fI"\r\n'
    )


def strip_cuda_local_versions(export_text: str) -> str:
    """``torch==X+cu126`` -> ``torch==X`` (PyPI's win_amd64 wheel is CPU-only); everything else untouched."""
    return (
        "\n".join(
            (m["head"] + m["tail"]) if (m := _CUDA_OR_CPU_LOCAL.match(line)) else line
            for line in export_text.splitlines()
        )
        + "\n"
    )


def write_beatgrid_launcher(payload_dir: Path) -> Path:
    """bin/opendj-beatgrid-python.cmd: the payload's CPython with PYTHONPATH REPLACED by the runner site."""
    site = str(payload_beatgrid.RUNNER_SITE_RELATIVE).replace("/", "\\")
    text = (
        batch_header("Interpreter for apps/analysis_beatgrid/beat_this_runner.py (MDT_BEATGRID_RUNNER_PYTHON).")
        + f'set "PYTHONPATH=%payload%\\{site}"\r\n'
        + 'set "PYTHONDONTWRITEBYTECODE=1"\r\n'
        + 'set "PYTHONNOUSERSITE=1"\r\n'
        + 'set "PYTHONSAFEPATH=1"\r\n'
        + '"%payload%\\runtime\\python.exe" %*\r\n'
        + "exit /b %ERRORLEVEL%\r\n"
    )
    launcher = payload_dir / BEATGRID_LAUNCHER_RELATIVE
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(text.encode("ascii"))
    return launcher


def verify_beatgrid_runner_files(payload_dir: Path) -> str:
    """Presence and digest of site, checkpoint, notice, launcher; returns the checkpoint sha256."""
    for label, path in (
        ("site", payload_dir / payload_beatgrid.RUNNER_SITE_RELATIVE),
        ("launcher", payload_dir / BEATGRID_LAUNCHER_RELATIVE),
        ("checkpoint", payload_dir / payload_beatgrid.CHECKPOINT_RELATIVE),
        ("checkpoint license notice", payload_dir / payload_beatgrid.CHECKPOINT_LICENSE_RELATIVE),
    ):
        if not path.exists():
            raise PayloadBuildError(f"beatgrid runner {label} missing at {path}")
    from apps.analysis_beatgrid import weights

    digest = sha256_file_hex(payload_dir / payload_beatgrid.CHECKPOINT_RELATIVE)
    if digest != weights.CHECKPOINT_SHA256:
        raise PayloadBuildError(f"staged checkpoint has sha256 {digest}, expected {weights.CHECKPOINT_SHA256}")
    notice = (payload_dir / payload_beatgrid.CHECKPOINT_LICENSE_RELATIVE).read_text(encoding="utf-8")
    if not notice.startswith("MIT License") or payload_beatgrid.CHECKPOINT_LICENSE_HOLDER not in notice:
        raise PayloadBuildError("beatgrid checkpoint license notice is not Beat This!'s MIT notice")
    return digest


def _sandbox_env(sandbox: str) -> dict[str, str]:
    system_root = os.environ["SYSTEMROOT"]
    return {
        "SystemRoot": system_root,
        "ComSpec": f"{system_root}\\System32\\cmd.exe",
        "PATH": f"{system_root}\\System32;{system_root}",
        "USERPROFILE": sandbox,
        "TEMP": sandbox,
        "TMP": sandbox,
    }


def verify_beatgrid_runner(payload_dir: Path, repo_root: Path) -> dict[str, object]:
    """Re-derive every claim from the staged bytes; the manifest block on success."""
    digest = verify_beatgrid_runner_files(payload_dir)
    pins = payload_beatgrid.runner_pins(repo_root / payload_beatgrid.RUNNER_SCRIPT_RELATIVE)
    with tempfile.TemporaryDirectory(prefix="opendj-beatgrid-verify-") as sandbox:
        # A script FILE and a comma list: a .cmd launcher's %* cannot carry a multi-line -c body or quotes.
        probe_script = Path(sandbox) / "probe.py"
        probe_script.write_text(_RUNNER_PROBE, encoding="utf-8")
        result = subprocess.run(
            [str(payload_dir / BEATGRID_LAUNCHER_RELATIVE), str(probe_script), ",".join(sorted(pins))],
            cwd=sandbox,
            capture_output=True,
            text=True,
            check=False,
            env=_sandbox_env(sandbox),
        )
    if result.returncode != 0:
        raise PayloadBuildError(f"the beatgrid runner interpreter cannot import its stack:\n{result.stderr}")
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise PayloadBuildError(f"the beatgrid runner probe printed nothing (stderr: {result.stderr!r})")
    probe = json.loads(lines[-1])
    if probe["apps_visible"]:
        raise PayloadBuildError("the beatgrid runner interpreter can import `apps`; its site is not isolated")
    if probe["torch_cuda"] is not None:
        raise PayloadBuildError(f"the staged torch is a CUDA {probe['torch_cuda']} build; the runner is CPU only")
    wrong = {n: (probe["versions"][n], v) for n, v in pins.items() if probe["versions"][n] != v}
    if wrong:
        raise PayloadBuildError(f"runner site versions differ from the PEP 723 pins (installed, pinned): {wrong}")
    site = payload_dir / payload_beatgrid.RUNNER_SITE_RELATIVE
    return {
        "site": payload_beatgrid.RUNNER_SITE_RELATIVE.as_posix(),
        "launcher": BEATGRID_LAUNCHER_RELATIVE.as_posix(),
        "checkpoint": payload_beatgrid.CHECKPOINT_RELATIVE.as_posix(),
        "checkpoint_sha256": digest,
        "checkpoint_license": payload_beatgrid.CHECKPOINT_LICENSE_RELATIVE.as_posix(),
        "pins": probe["versions"],
        "torch_cuda": probe["torch_cuda"],
        "bytes": {
            "site": tree_bytes(site),
            "checkpoint": (payload_dir / payload_beatgrid.CHECKPOINT_RELATIVE).stat().st_size,
        },
    }


def stage_beatgrid_runner(repo_root: Path, payload_dir: Path) -> dict[str, object]:
    """Install the locked runner closure, stage the verified checkpoint and the launcher, then prove them."""
    python = payload_dir / RUNTIME_RELATIVE
    try:
        export = strip_cuda_local_versions(payload_beatgrid.runner_locked_export(repo_root))
        cached = payload_beatgrid.fetch_checkpoint(payload_beatgrid.CHECKPOINT_CACHE_DIR)
    except payload_beatgrid.PayloadBeatgridError as exc:
        raise PayloadBuildError(str(exc)) from exc
    install_dependencies(parse_locked_export(export), python, payload_dir / payload_beatgrid.RUNNER_SITE_RELATIVE)
    payload_beatgrid.stage_checkpoint(payload_dir, cached)
    write_beatgrid_launcher(payload_dir)
    return verify_beatgrid_runner(payload_dir, repo_root)


def stage_baked_inputs(
    payload_dir: Path, *, allow_missing: bool, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Bake the OAuth client and the ship DSN; returns {piece: reason} for each one skipped."""
    not_staged: dict[str, str] = {}
    for piece, bake, error_type in (
        ("google_oauth", payload_google_oauth.bake_google_oauth, payload_google_oauth.PayloadOAuthError),
        ("telemetry", payload_telemetry.bake_telemetry, payload_telemetry.PayloadTelemetryError),
    ):
        try:
            bake(payload_dir, env)
        except error_type as exc:
            if not allow_missing:
                raise PayloadBuildError(str(exc)) from exc
            not_staged[piece] = f"dev build, input absent: {exc}"
    return not_staged


def stage_licenses(repo_root: Path, payload_dir: Path) -> dict[str, object]:
    try:
        summary = third_party_licenses.write_payload_license_files(
            repo_root, payload_dir, rust_target_triple=WINDOWS_RUST_TARGET_TRIPLE
        )
        license_verify.verify_bundled_licenses(payload_dir)
    except third_party_licenses.LicenseInventoryError as exc:
        raise PayloadBuildError(str(exc)) from exc
    return summary


def stage_extras(
    repo_root: Path, payload_dir: Path, *, allow_missing_secrets: bool, env: Mapping[str, str] | None = None
) -> ExtrasResult:
    if not sys.flags.utf8_mode:
        raise PayloadBuildError(
            "run with PYTHONUTF8=1 (or python -X utf8): the reused helpers decode tool output as text"
        )
    beatgrid = stage_beatgrid_runner(repo_root, payload_dir)
    not_staged = stage_baked_inputs(payload_dir, allow_missing=allow_missing_secrets, env=env)
    licenses = stage_licenses(repo_root, payload_dir)
    return ExtrasResult({"beatgrid_runner": beatgrid, "third_party_licenses": licenses}, not_staged)


def apply_extras_to_manifest(
    manifest: dict[str, object],
    extras: ExtrasResult,
    *,
    remaining_gaps: dict[str, str],
) -> dict[str, object]:
    """Manifest with the extras' blocks; not_staged is the standing gaps plus any skipped secrets."""
    not_staged = {**remaining_gaps, **extras.not_staged}
    for key in extras.manifest_blocks:
        not_staged.pop(key, None)
    return {**manifest, **extras.manifest_blocks, "not_staged": not_staged}


def refresh_digests(payload_dir: Path, manifest: dict[str, object]) -> dict[str, object]:
    """Recompute the tree digests and byte counts after staging into an existing payload."""
    names = ("runtime", "pylib", "app")
    return {
        **manifest,
        "sha256": {name: sha256_tree(payload_dir / name) for name in names},
        "bytes": {**{name: tree_bytes(payload_dir / name) for name in names}, "total": tree_bytes(payload_dir)},
    }
