"""AUTH-01 / OPS-11: dmg preflight names a missing Google OAuth client.

just dmg used to package an engine whose health reported
google_oauth_configured: false because the client was never in the
build environment. Item I fails up front naming
OPENDJ_GOOGLE_OAUTH_CLIENT_ID and OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET.
THE VALUE IS NEVER PRINTED.

Doppler on PATH plus a live token would green the failure path, so the
missing-secret case PATH-hides doppler and pops the four env names.

Regression lines:
  - if a missing client still exits 0 on that item, packaged sign-in
    ships dead
  - if the report omits OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET, the operator
    is told they are broken without being told which secret to set
  - if a sentinel secret value reaches stdout/stderr, the report leaks
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("AUTH-01")

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "dmg_preflight.sh"

OAUTH_ENV = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
    "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)

SENTINEL_ID = "oauth-id-MUST-NOT-APPEAR-9f3c"
SENTINEL_SECRET = "oauth-secret-MUST-NOT-APPEAR-9f3c"


def _path_without(tool: str) -> str:
    kept = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and not (Path(part) / tool).exists()
    ]
    return os.pathsep.join(kept)


def _run_preflight_oauth(
    extra_env: dict[str, str] | None = None, *, hide_doppler: bool = True
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("MDT_MACOS_SIGNING_IDENTITY", None)
    env.pop("MDT_MACOS_NOTARY_KEYCHAIN_PROFILE", None)
    env["MDT_SHIP_UNSIGNED"] = "1"
    for name in OAUTH_ENV:
        env.pop(name, None)
    if hide_doppler:
        env["PATH"] = _path_without("doppler")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(PREFLIGHT)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=REPO,
    )


def test_a_missing_google_oauth_client_is_an_unmet_precondition() -> None:
    result = _run_preflight_oauth()
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in out, out
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET" in out, out
    assert SENTINEL_ID not in out
    assert SENTINEL_SECRET not in out


def test_oauth_secret_value_never_reaches_the_report() -> None:
    result = _run_preflight_oauth(
        {
            "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": SENTINEL_ID,
            "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": SENTINEL_SECRET,
        }
    )
    out = result.stdout + result.stderr
    assert SENTINEL_ID not in out, out
    assert SENTINEL_SECRET not in out, out
    assert shutil.which("doppler") is None or "Google OAuth" in out or "[OK]" in out
