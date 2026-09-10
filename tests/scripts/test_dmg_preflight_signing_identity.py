"""dmg preflight item C: the signing identity must EXIST, not merely be named.

WHY THIS EXISTS. On Thu 10 Sep 2026 a dmg build was attempted on silver.
`MDT_MACOS_SIGNING_IDENTITY` was set (it lives in `~/.zshenv`), so every
signing check passed. The machine held no Developer ID certificate in any
keychain at all, and the build died at codesign 30 seconds later, after
staging and verifying the whole payload:

    Developer ID Application: the maintainer (5K6FYJMYR6): no identity found
    [ERROR] codesign failed inside the payload

Converting exactly that into a one-second refusal is what this preflight is
for, so checking only that the variable is a non-empty string was a check that
could not fail for the condition it existed to catch.

A SECOND, SUBTLER STATE. After the certificate was installed, silver still
reported `0 valid identities` because Apple's G2 intermediate was missing:
`security find-identity -p codesigning` listed it, `-v` did not. codesign
fails in that state too, so the check must use the `-v` form. Matching on the
looser listing would pass a machine that cannot sign.

Regression lines:
  - if the preflight passes with an identity that no keychain holds then broken
  - if it passes when the identity is listed but NOT valid (broken chain) then broken
  - if a genuinely present identity is refused then broken (the check must be able to pass)
  - if a configured identity is refused under MDT_SHIP_UNSIGNED=1 then broken
  - if the absence of `security` renders as a pass then broken (unmeasured is not a verdict)
  - if the failure report omits what the machine actually has then broken
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPS-11")

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "dmg_preflight.sh"

REAL_LOOKING = "Developer ID Application: Nobody Real (ZZ9PLURAL)"


def _run(env_extra: dict[str, str], path_prepend: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}:{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(PREFLIGHT)], cwd=REPO, env=env, capture_output=True, text=True, timeout=300
    )


def _fake_security(tmp_path: Path, valid: str, *, listed: str | None = None) -> Path:
    """A stand-in `security` so the test controls what identities exist.

    FLAG AWARE, and that is the whole point. `valid` is what the `-v` form
    returns; `listed` is what the looser `-p codesigning` form returns, and
    defaults to the same thing.

    Found by mutation: an earlier version of this stub answered every
    `find-identity` invocation with one string, so swapping the preflight from
    `-v` to `-p` kept the entire suite GREEN. The test that claims to cover a
    broken certificate chain could not distinguish the two flags and was
    therefore asserting something it never measured, which is the same defect
    class this file exists to catch, one level up.

    The real binary reports whatever this developer machine happens to hold,
    which would make results depend on who runs the suite.
    """
    if listed is None:
        listed = valid
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "security"
    fake.write_text(
        "#!/bin/bash\n"
        # Only the identity listing is stubbed; anything else the preflight
        # asks of `security` must not be silently answered with this text.
        'if [[ "$*" == *"find-identity"* ]]; then\n'
        '  if [[ "$*" == *"-v"* ]]; then\n'
        f"    cat <<'VEOF'\n{valid}\nVEOF\n"
        "  else\n"
        f"    cat <<'LEOF'\n{listed}\nLEOF\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return bindir


def _signing_env(identity: str) -> dict[str, str]:
    return {
        "MDT_MACOS_SIGNING_IDENTITY": identity,
        "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE": "test-notary-profile",
        "MDT_SHIP_UNSIGNED": "",
    }


def test_identity_absent_from_every_keychain_is_refused(tmp_path: Path) -> None:
    """The exact silver failure: the name is configured, the cert is nowhere."""
    bindir = _fake_security(tmp_path, "     0 valid identities found")
    result = _run(_signing_env(REAL_LOOKING), path_prepend=bindir)
    assert "signing identity" in result.stdout, (
        "a configured identity that no keychain holds must be reported as an unmet "
        f"precondition, got:\n{result.stdout}"
    )
    assert REAL_LOOKING in result.stdout


def test_identity_listed_but_not_valid_is_refused(tmp_path: Path) -> None:
    """Broken chain: `-p codesigning` lists it, `-v` does not. codesign fails."""
    # Exactly the state silver was in after the cert was installed and before
    # Apple's G2 intermediate was: `-p codesigning` LISTS the identity, `-v`
    # does not. codesign fails here, so consulting the loose listing would pass
    # a machine that cannot sign.
    bindir = _fake_security(
        tmp_path,
        valid="     0 valid identities found",
        listed=f'  1) 5FC0AB0D99BA6AFA2718595C528EC3AC1A70A0AA "{REAL_LOOKING}"\n     1 identities found',
    )
    result = _run(_signing_env(REAL_LOOKING), path_prepend=bindir)
    assert "signing identity" in result.stdout, (
        "an identity whose chain is incomplete reports 0 valid and cannot sign; "
        f"it must not pass:\n{result.stdout}"
    )


def test_present_identity_is_accepted(tmp_path: Path) -> None:
    """The check must be able to PASS, or it is not a check."""
    listing = (
        f'  1) 5FC0AB0D99BA6AFA2718595C528EC3AC1A70A0AA "{REAL_LOOKING}"\n'
        "     1 valid identities found"
    )
    bindir = _fake_security(tmp_path, listing)
    result = _run(_signing_env(REAL_LOOKING), path_prepend=bindir)
    assert "signing identity" not in (result.stdout.split("=== summary ===")[-1]), (
        f"a present, valid identity must not be reported as unmet:\n{result.stdout}"
    )
    assert f"signing as: {REAL_LOOKING}" in result.stdout


def test_unsigned_build_does_not_require_an_identity(tmp_path: Path) -> None:
    """MDT_SHIP_UNSIGNED=1 is a deliberate path and must not need a certificate."""
    bindir = _fake_security(tmp_path, "     0 valid identities found")
    env = {
        "MDT_MACOS_SIGNING_IDENTITY": "",
        "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE": "",
        "MDT_SHIP_UNSIGNED": "1",
    }
    result = _run(env, path_prepend=bindir)
    assert "signing identity" not in result.stdout.split("=== summary ===")[-1], (
        f"an explicitly unsigned build must not be blocked on a certificate:\n{result.stdout}"
    )


def test_failure_report_shows_what_the_machine_actually_has(tmp_path: Path) -> None:
    """An operator must not have to guess what is installed instead."""
    other = '  1) AAAA "Developer ID Application: Someone Else (QQ1QQ1QQ1)"\n     1 valid identities found'
    bindir = _fake_security(tmp_path, other)
    result = _run(_signing_env(REAL_LOOKING), path_prepend=bindir)
    assert "Someone Else" in result.stdout, (
        f"the report must list the identities that ARE present:\n{result.stdout}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_missing_security_binary_is_unmeasured_not_a_pass(tmp_path: Path) -> None:
    """No `security` means the check did not run. That must not read as OK.

    A pass here would make "this host has no security binary" indistinguishable
    from "the identity is present", which is the failure this whole file is about,
    one level up.
    """
    # A PATH holding only the handful of tools the earlier rungs need, minus
    # `security`, is not reconstructable portably; instead assert the script
    # contains the unmeasured branch and never an ok() on that path.
    body = PREFLIGHT.read_text()
    assert "signing identity UNMEASURED" in body, (
        "the no-security path must report UNMEASURED explicitly"
    )
    idx = body.index("signing identity UNMEASURED")
    window = body[idx : idx + 400]
    assert "ok " not in window and 'ok "' not in window, (
        "the unmeasured branch must not report an ok verdict"
    )
