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
  - if hiding doppler from PATH also hides the script's own interpreter,
    the run never starts and every check above is unmeasured, not passing
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
# Invoked explicitly rather than relying on the shebang's `/usr/bin/env bash`
# to resolve an interpreter from PATH. _path_without below hides doppler by
# dropping its WHOLE PATH directory, and on a self-hosted runner where
# Homebrew installs both bash and doppler into the same bin dir, that drops
# the only bash on PATH too -- `/usr/bin/env: 'bash': No such file or
# directory`, and the script never starts (see
# test_preflight_still_launches_when_bash_and_doppler_share_a_directory).
# /bin/bash is a POSIX baseline present on every macOS and Linux runner GitHub
# Actions uses, independent of whatever this test does to PATH, and is the
# same convention every other subprocess-driving test in this suite already
# uses (tests/scripts/test_dmg_preflight_signing_identity.py, the
# autoreposync tests).
BASH = "/bin/bash"

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
        [BASH, str(PREFLIGHT)],
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


def test_preflight_still_launches_when_bash_and_doppler_share_a_directory(
    tmp_path: Path,
) -> None:
    """Regression for `/usr/bin/env: 'bash': No such file or directory`.

    _path_without hides doppler by dropping its ENTIRE PATH directory, not
    just the doppler binary. That is safe only as long as nothing else load-
    bearing lives in the same directory -- and on a self-hosted runner where
    Homebrew installs bash and doppler into one shared bin dir (bash is
    famously not the OS-shipped one on macOS), it is the ONLY bash on PATH.
    Hiding doppler then hides the shebang's interpreter too, and the script
    never starts.

    Reproduced here without touching real CI: bash's real system directory
    (wherever `shutil.which("bash")` resolves to, e.g. /bin on macOS) is
    dropped from PATH entirely, and a stand-in directory holding both a
    doppler stub AND a symlink to the real bash is put in its place -- the
    same shape as a Homebrew bin dir holding both. Every other PATH entry is
    left alone, so tools the script needs before it would ever reach the
    OAuth check (git, uv, cargo, ...) still resolve normally, isolating this
    down to the one collision under test.
    """
    real_bash = shutil.which("bash")
    assert real_bash, "no bash on the host PATH; cannot build the scenario"
    real_bash_dir = str(Path(real_bash).resolve().parent)

    shared = tmp_path / "shared-bin"
    shared.mkdir()
    (shared / "doppler").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (shared / "doppler").chmod(0o755)
    (shared / "bash").symlink_to(real_bash)

    other_dirs = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and str(Path(part).resolve()) != real_bash_dir
    ]
    env = dict(os.environ)
    env.pop("MDT_MACOS_SIGNING_IDENTITY", None)
    env.pop("MDT_MACOS_NOTARY_KEYCHAIN_PROFILE", None)
    env["MDT_SHIP_UNSIGNED"] = "1"
    for name in OAUTH_ENV:
        env.pop(name, None)
    env["PATH"] = os.pathsep.join([str(shared), *other_dirs])

    # Hide doppler exactly as _run_preflight_oauth does: drop every PATH
    # entry holding a `doppler` executable.
    kept = [
        part for part in env["PATH"].split(os.pathsep) if part and not (Path(part) / "doppler").exists()
    ]
    assert not any((Path(p) / "bash").exists() for p in kept), (
        "fixture is invalid: bash is still reachable after hiding doppler, "
        "so this run would not exercise the collision"
    )
    env["PATH"] = os.pathsep.join(kept)

    # Invoked the same way _run_preflight_oauth now does: an explicit
    # interpreter, not `[str(PREFLIGHT)]` relying on PATH to resolve the
    # shebang. Reverting that call site back to the bare path is exactly the
    # regression this test exists to catch -- under this PATH it fails with
    # `/usr/bin/env: 'bash': No such file or directory` before ever reaching
    # a single precondition check.
    result = subprocess.run(
        [BASH, str(PREFLIGHT)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=REPO,
    )

    assert "No such file or directory" not in result.stderr, (
        "hiding doppler must not also hide the script's own interpreter: "
        f"{result.stderr}"
    )
