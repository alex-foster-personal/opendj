"""The notary --keychain half of INSTALL-04, split out of test_macos_signing.py.

Hit live on the Air, Wed 30 Sep 2026: over ssh the login keychain is locked,
so a notary profile stored in the dedicated signing keychain never resolved
("keychainLocked(keychainName: \"default\")") until notarytool was handed
--keychain. These run the real `notarize` stage against a recording xcrun.

Split into its own file rather than appended to test_macos_signing.py so
that file stays under the repo's 600-line-per-test-file ratchet (it was at
547 lines before these three tests; appending them would have pushed it to
624). Shares `_sign` and `UNRESOLVABLE_IDENTITY` with that file rather than
duplicating them.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if notarize ignores MDT_MACOS_NOTARY_KEYCHAIN, a headless build dies on the
  locked login keychain -> broken.
- if notarize invents a --keychain flag when none is configured, GUI-session
  hosts that resolve the profile from the login keychain break -> broken.
- if notarize submits with a notary keychain path that is not a file, the
  failure surfaces minutes later as an opaque notary error instead of
  refusing up front -> broken.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.scripts.test_macos_signing import UNRESOLVABLE_IDENTITY, _sign

# ----- notary keychain ---------------------------------------------------


def _recording_toolchain(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH dir whose xcrun records its argv and accepts every submission."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "xcrun.calls"
    xcrun = bin_dir / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        'case "$1 $2" in "notarytool submit") echo "  status: Accepted";; esac\n'
        "exit 0\n"
    )
    spctl = bin_dir / "spctl"
    spctl.write_text("#!/bin/sh\nexit 0\n")
    for tool in (xcrun, spctl):
        tool.chmod(0o755)
    return bin_dir, calls


def _notarize_with(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir, calls = _recording_toolchain(tmp_path)
    dmg = tmp_path / "OpenDJ.dmg"
    dmg.write_bytes(b"not really a dmg")
    result = _sign(
        "notarize",
        str(dmg),
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY,
        MDT_MACOS_NOTARY_KEYCHAIN_PROFILE="opendj-notary",
        **overrides,
    )
    return result, calls.read_text() if calls.exists() else ""


@pytest.mark.requirement("INSTALL-04")
def test_notarize_passes_the_configured_notary_keychain(tmp_path: Path) -> None:
    """if notarize ignores MDT_MACOS_NOTARY_KEYCHAIN then a headless build dies on the locked login keychain -> broken"""
    keychain = tmp_path / "opendj-signing.keychain-db"
    keychain.write_bytes(b"")
    result, calls = _notarize_with(tmp_path, MDT_MACOS_NOTARY_KEYCHAIN=str(keychain))
    assert result.returncode == 0, result.stdout + result.stderr
    submits = [line for line in calls.splitlines() if line.startswith("notarytool submit")]
    assert submits, f"notarytool submit never ran: {calls!r}"
    assert all(f"--keychain {keychain}" in line for line in submits), submits


@pytest.mark.requirement("INSTALL-04")
def test_notarize_without_a_notary_keychain_keeps_the_default_lookup(tmp_path: Path) -> None:
    """if notarize invents a --keychain when none is configured then GUI-session hosts that resolve the profile from login break -> broken"""
    result, calls = _notarize_with(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "notarytool submit" in calls, calls
    assert "--keychain " not in calls, calls


@pytest.mark.requirement("INSTALL-04")
def test_notarize_refuses_a_notary_keychain_that_does_not_exist(tmp_path: Path) -> None:
    """if notarize submits with a notary keychain path that is not a file then the failure surfaces minutes later as a notary error -> broken"""
    missing = tmp_path / "absent.keychain-db"
    result, calls = _notarize_with(tmp_path, MDT_MACOS_NOTARY_KEYCHAIN=str(missing))
    assert result.returncode != 0
    assert "MDT_MACOS_NOTARY_KEYCHAIN" in result.stderr, result.stderr
    assert "notarytool" not in calls, calls
