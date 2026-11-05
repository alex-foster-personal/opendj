"""Shared Doppler CLI secret reader for engine payload bake scripts.

Supersedes: private ``_doppler_get`` copies in
``scripts/payload_google_oauth.py`` and ``scripts/payload_telemetry.py``;
those helpers are deleted, not kept as wrappers.
"""

from __future__ import annotations

import shutil
import subprocess


def read_doppler_secret(name: str) -> str:
    """Read one Doppler secret. Never logs stdout (the value)."""
    doppler = shutil.which("doppler")
    if doppler is None:
        return ""
    try:
        result = subprocess.run(
            [
                doppler,
                "secrets",
                "get",
                name,
                "--project",
                "general",
                "--config",
                "dev_personal",
                "--plain",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
