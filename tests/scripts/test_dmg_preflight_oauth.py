"""AUTH-01 / OPS-11: dmg preflight names a missing Google OAuth client.

just dmg used to package an engine whose health reported
google_oauth_configured: false because the client was never in the
build environment. Item I fails up front naming
OPENDJ_GOOGLE_OAUTH_CLIENT_ID and OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET.
THE VALUE IS NEVER PRINTED.

Doppler visibility is controlled through MDT_DOPPLER_BIN, an explicit
override scripts/dmg_preflight.sh accepts for exactly this purpose, not by
editing PATH. Editing PATH to hide doppler used to drop doppler's WHOLE PATH
directory (every entry containing a `doppler` executable), which is
host-layout dependent: on a runner where doppler happens to share a bin
directory with another tool the script needs before it ever reaches the
OAuth check (bash itself, git, uv, cargo, ...), hiding doppler silently hid
that tool too, and under `set -euo pipefail` the whole script aborted on an
unrelated precondition -- passing or failing depending on the runner's own
directory layout, not on the code under test (confirmed: failed on
agentbox-3, passed on agentbox-11, same code). MDT_DOPPLER_BIN sidesteps the
class of bug entirely: it points the script's doppler lookup at an exact
path, so "doppler absent" and "doppler present" are both deliberate,
hermetic choices, made without touching PATH at all.

Regression lines:
  - if a missing client still exits 0 on that item, packaged sign-in
    ships dead
  - if the report omits OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET, the operator
    is told they are broken without being told which secret to set
  - if a sentinel secret value reaches stdout/stderr, the report leaks,
    whether doppler is absent, present-and-empty, or present-and-live
  - if hiding doppler from PATH also hides the script's own interpreter,
    the run never starts and every check above is unmeasured, not passing
  - if doppler-absent vs doppler-present depends on the runner's directory
    layout instead of a deliberate choice, the test's outcome is luck, not
    proof
  - [if] the engine misses the OAuth id/secret [then] item I fails naming both, never value, [else stop]
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
# to resolve an interpreter from PATH. See
# test_preflight_still_launches_when_bash_and_doppler_share_a_directory for
# the collision this guards against. /bin/bash is a POSIX baseline present on
# every macOS and Linux runner GitHub Actions uses, and is the same
# convention every other subprocess-driving test in this suite already uses
# (tests/scripts/test_dmg_preflight_signing_identity.py, the autoreposync
# tests).
BASH = "/bin/bash"

OAUTH_ENV = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
    "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)

SENTINEL_ID = "oauth-id-MUST-NOT-APPEAR-9f3c"
SENTINEL_SECRET = "oauth-secret-MUST-NOT-APPEAR-9f3c"

# A path guaranteed not to exist on any host, so `command -v "$MDT_DOPPLER_BIN"`
# fails deterministically -- the "doppler absent" branch made explicit, with
# no dependency on the runner's PATH layout.
ABSENT_DOPPLER = "/nonexistent-for-dmg-preflight-tests/doppler-must-not-run"


def _run_preflight_oauth(
    extra_env: dict[str, str] | None = None,
    *,
    doppler_bin: str = ABSENT_DOPPLER,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("MDT_MACOS_SIGNING_IDENTITY", None)
    env.pop("MDT_MACOS_NOTARY_KEYCHAIN_PROFILE", None)
    env["MDT_SHIP_UNSIGNED"] = "1"
    for name in OAUTH_ENV:
        env.pop(name, None)
    env["MDT_DOPPLER_BIN"] = doppler_bin
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


def _fake_doppler(tmp_path: Path, *, client_id: str, client_secret: str) -> Path:
    """Build a stub `doppler` binary that answers `secrets get` deterministically.

    Deliberately exercises the "doppler present and configured" branch of
    check_google_oauth_client without depending on the real doppler CLI or a
    live token being available on whatever host runs the test.
    """
    script = tmp_path / "doppler"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "secrets" ] && [ "$2" = "get" ]; then\n'
        '  case "$3" in\n'
        f'    OPENDJ_GOOGLE_OAUTH_CLIENT_ID|GOOGLE_OAUTH_CLIENT_ID) echo "{client_id}" ;;\n'
        "    OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET|GOOGLE_OAUTH_CLIENT_SECRET)\n"
        f'      echo "{client_secret}" ;;\n'
        '    *) exit 1 ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'exit 1\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_a_missing_google_oauth_client_is_an_unmet_precondition() -> None:
    result = _run_preflight_oauth()
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in out, out
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET" in out, out
    assert SENTINEL_ID not in out
    assert SENTINEL_SECRET not in out


def test_oauth_secret_value_never_reaches_the_report() -> None:
    """Env-supplied secret values never leak, with doppler deliberately absent.

    Doppler is not consulted at all on this path (the client is already in
    the environment), so ABSENT_DOPPLER is used here purely to keep the run
    hermetic -- no PATH edits, no dependency on whether the host happens to
    have a doppler binary. The outcome no longer depends on `shutil.which
    ("doppler")`, which is exactly the host-dependent condition that made
    this test flaky.
    """
    result = _run_preflight_oauth(
        {
            "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": SENTINEL_ID,
            "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": SENTINEL_SECRET,
        }
    )
    out = result.stdout + result.stderr
    assert SENTINEL_ID not in out, out
    assert SENTINEL_SECRET not in out, out
    assert "Google OAuth" in out, out
    assert "[OK]" in out, out


def test_oauth_secret_value_never_reaches_the_report_via_live_doppler(
    tmp_path: Path,
) -> None:
    """Same guarantee, with doppler deliberately PRESENT and answering live.

    The env-supplied client from the previous test never reaches doppler at
    all, so that test alone does not prove the doppler-resolution branch is
    safe. This drives it directly: no client in the environment, a stub
    `doppler` on MDT_DOPPLER_BIN returns a live-looking client id and secret,
    and the report must still never print the secret value.
    """
    doppler = _fake_doppler(tmp_path, client_id=SENTINEL_ID, client_secret=SENTINEL_SECRET)
    result = _run_preflight_oauth(doppler_bin=str(doppler))
    out = result.stdout + result.stderr
    assert SENTINEL_ID not in out, out
    assert SENTINEL_SECRET not in out, out
    # Only item I (the OAuth client) is under test here; other preconditions
    # (a clean tree, a built SPA, an updater signing key) are free to fail on
    # whatever tree happens to run this, so the overall exit code is not
    # asserted -- only that this item resolved via doppler and did not leak.
    assert "available from Doppler" in out, out


def test_preflight_still_launches_when_bash_and_doppler_share_a_directory(
    tmp_path: Path,
) -> None:
    """Regression for `/usr/bin/env: 'bash': No such file or directory`.

    Hiding doppler by dropping its ENTIRE PATH directory is safe only as
    long as nothing else load-bearing lives in the same directory -- and on
    a self-hosted runner where Homebrew installs bash and doppler into one
    shared bin dir (bash is famously not the OS-shipped one on macOS), it is
    the ONLY bash on PATH. Hiding doppler then hides the shebang's
    interpreter too, and the script never starts. This is a narrower,
    already-fixed regression (the explicit BASH interpreter above) kept
    alive here on purpose: it is orthogonal to MDT_DOPPLER_BIN, which lets
    the OTHER tests above avoid PATH surgery altogether, but this one still
    needs to reproduce the PATH collision itself.

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

    # Hide doppler exactly the way the (now-retired) PATH-surgery approach
    # did: drop every PATH entry holding a `doppler` executable. This test
    # is specifically about that surgery colliding with bash, so it
    # reproduces the surgery on purpose rather than using MDT_DOPPLER_BIN.
    kept = [
        part for part in env["PATH"].split(os.pathsep) if part and not (Path(part) / "doppler").exists()
    ]
    assert not any((Path(p) / "bash").exists() for p in kept), (
        "fixture is invalid: bash is still reachable after hiding doppler, "
        "so this run would not exercise the collision"
    )
    env["PATH"] = os.pathsep.join(kept)

    # Invoked the same way _run_preflight_oauth does: an explicit
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
