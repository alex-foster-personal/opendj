"""Build and stage the Electron CoreAudio output probe."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

DESKTOP_CRATE = "apps/desktop/src-tauri"
OUTPUT_PROBE_RELATIVE = "bin/odj-output-probe"


class PayloadBuildError(RuntimeError):
    """The payload cannot be staged, or cannot be proven safe to ship."""


def stage_output_probe(repo_root: Path, payload_dir: Path) -> None:
    """Build the output probe and copy it into the payload."""
    if shutil.which("cargo") is None:
        raise PayloadBuildError("cargo is not on PATH; cannot build odj-output-probe")
    crate = repo_root / DESKTOP_CRATE
    target_dir = crate / "target"
    resource_dir = crate / "payload"
    created_resource_dir = not resource_dir.exists()
    if created_resource_dir:
        resource_dir.mkdir()
    try:
        result = subprocess.run(
            [
                "cargo",
                "build",
                "--release",
                "--locked",
                "--bin",
                "odj-output-probe",
                "--manifest-path",
                str(crate / "Cargo.toml"),
                "--target-dir",
                str(target_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if created_resource_dir:
            resource_dir.rmdir()
    if result.returncode != 0:
        raise PayloadBuildError(f"cargo build of odj-output-probe failed:\n{result.stderr}")
    staged = payload_dir / OUTPUT_PROBE_RELATIVE
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target_dir / "release" / "odj-output-probe", staged)
    staged.chmod(0o755)


def output_probe_manifest(payload_dir: Path) -> dict[str, str]:
    """Return the manifest entry for the staged output probe."""
    staged = payload_dir / OUTPUT_PROBE_RELATIVE
    return {"path": OUTPUT_PROBE_RELATIVE, "sha256": _sha256_file(staged)}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
