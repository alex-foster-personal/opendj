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

NO STUBBED `security`, AND WHY THE COVERAGE IS SHAPED THE WAY IT IS.

An earlier version of this file prepended a generated `security` executable to
PATH and manufactured each outcome. AGENTS.md -> "No mocks and locked real
fixtures" prohibits that outright, and it also made the tests weaker than they
looked: whatever the production script asked of `security`, the stub answered,
so the tests could not tell the real command's semantics from the stub author's
belief about them.

Every case below therefore runs the REAL `scripts/dmg_preflight.sh` against the
REAL `security` on this host, and each derives its expectation from what that
host actually holds, read once here:

  - ABSENT: an identity name a real keychain cannot contain. The real listing
    is consulted and really does not match. Deterministic on any macOS host.
  - PRESENT: the name of an identity the host really holds. UNAVAILABLE, and
    skipped with that reason, on a host with none - never manufactured.
  - BROKEN CHAIN (listed by `-p codesigning`, absent from `-v`): that state
    cannot be produced on demand without fabricating certificate state, so it
    is NOT simulated. What is asserted instead is the production script's
    own choice of the `-v` form, the decision the state would have tested.
  - NO `security` AT ALL: a PATH without it is not reconstructable portably
    (the earlier rungs need many tools from the same directory), so the
    refusal branch is asserted against the production source. The behaviour it
    pins - unmeasured must not read as a pass - is the one that mattered.

Regression lines:
  - if the preflight passes with an identity that no keychain holds then broken
  - if a genuinely present identity is refused then broken (the check must be
    able to pass)
  - if a configured identity is refused under MDT_SHIP_UNSIGNED=1 then broken
  - if the identity check consults the loose listing rather than `-v` then
    broken (a broken chain reports 0 valid and cannot sign)
  - if the absence of `security` renders as a pass then broken (unmeasured is
    not a verdict)
  - if the failure report omits what the machine actually has then broken

[if] the preflight accepts an identity this machine cannot sign with [then] fail, [else stop].
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPS-11")

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "dmg_preflight.sh"

# A name no keychain can hold, and unmistakably not a real Developer ID.
ABSENT_IDENTITY = "Developer ID Application: Not A Real Certificate (TESTONLY0)"

darwin_only = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("security") is None,
    reason="UNAVAILABLE: the real `security` codesigning listing is macOS-only",
)


def _real_valid_identities() -> list[str]:
    """Identity names this host really holds, from the real production command.

    The same `security find-identity -v -p codesigning` the preflight runs.
    Returns [] when the host holds none, which the callers treat as UNAVAILABLE
    rather than as a result.
    """
    out = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return re.findall(r'^\s*\d+\)\s+[0-9A-F]+\s+"(.+)"\s*$', out.stdout, re.MULTILINE)


def _run(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(PREFLIGHT)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _signing_env(identity: str) -> dict[str, str]:
    return {
        "MDT_MACOS_SIGNING_IDENTITY": identity,
        "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE": "test-notary-profile",
        "MDT_SHIP_UNSIGNED": "",
    }


def _summary(stdout: str) -> str:
    return stdout.rsplit("=== summary ===", maxsplit=1)[-1]


@darwin_only
def test_identity_absent_from_every_keychain_is_refused() -> None:
    """The exact silver failure: the name is configured, the cert is nowhere."""
    assert ABSENT_IDENTITY not in _real_valid_identities(), (
        "this test's premise is that the host does NOT hold this identity"
    )
    result = _run(_signing_env(ABSENT_IDENTITY))
    assert "signing identity" in result.stdout, (
        "a configured identity that no keychain holds must be reported as an unmet "
        f"precondition, got:\n{result.stdout}"
    )
    assert ABSENT_IDENTITY in result.stdout


@darwin_only
def test_failure_report_shows_what_the_machine_actually_has() -> None:
    """An operator must not have to guess what is installed instead."""
    held = _real_valid_identities()
    if not held:
        pytest.skip("UNAVAILABLE: this host holds no valid codesigning identity to list")
    result = _run(_signing_env(ABSENT_IDENTITY))
    assert held[0] in result.stdout, (
        f"the report must list what IS present, expected {held[0]!r}:\n{result.stdout}"
    )


@darwin_only
def test_present_identity_is_accepted() -> None:
    """The check must be able to PASS, or it is not a check."""
    held = _real_valid_identities()
    if not held:
        pytest.skip("UNAVAILABLE: this host holds no valid codesigning identity to accept")
    result = _run(_signing_env(held[0]))
    assert "signing identity" not in _summary(result.stdout), (
        f"a present, valid identity must not be reported as unmet:\n{result.stdout}"
    )
    assert f"signing as: {held[0]}" in result.stdout


def test_unsigned_build_does_not_require_an_identity() -> None:
    """MDT_SHIP_UNSIGNED=1 is a deliberate path and must not need a certificate."""
    result = _run(
        {
            "MDT_MACOS_SIGNING_IDENTITY": "",
            "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE": "",
            "MDT_SHIP_UNSIGNED": "1",
        }
    )
    assert "signing identity" not in _summary(result.stdout), (
        f"an explicitly unsigned build must not be blocked on a certificate:\n{result.stdout}"
    )


def test_identity_lookup_uses_the_valid_form_not_the_loose_listing() -> None:
    """A broken chain is LISTED by `-p codesigning` and absent from `-v`.

    That state cannot be produced on this host without fabricating certificate
    state, so the state is not simulated; the production script's choice of
    command is pinned instead, which is the decision the state would have
    tested. Asserted as an invariant over the identity lookup rather than over
    the whole file, so an unrelated `-p codesigning` elsewhere cannot satisfy
    it.
    """
    body = PREFLIGHT.read_text(encoding="utf-8")
    lookup = re.search(r'^\s*available="\$\((.+)\)"\s*$', body, re.MULTILINE)
    assert lookup is not None, "the identity lookup is no longer where this guard reads it"
    command = lookup.group(1)
    assert "security find-identity" in command, command
    assert " -v " in f" {command} ", (
        "the identity lookup must use the -v (VALID) form: a certificate whose chain is "
        f"incomplete is listed by the loose form and cannot sign. Got: {command}"
    )


def test_a_host_that_cannot_measure_identities_refuses_rather_than_passing() -> None:
    """No `security` means the check did not run. That must not read as OK.

    A pass there would make "this host has no security binary" indistinguishable
    from "the identity is present", which is the failure this whole file is
    about, one level up. A PATH genuinely without `security` is not
    reconstructable portably (the earlier rungs need many tools from the same
    directory), so the branch is pinned in the production source.
    """
    body = PREFLIGHT.read_text(encoding="utf-8")
    branch = re.search(
        r"if ! command -v security[^\n]*\n(.*?)\n\s*else\b", body, re.DOTALL
    )
    assert branch is not None, "the no-security branch is no longer where this guard reads it"
    source = branch.group(1)
    assert "fail " in source, (
        "an unmeasurable signing identity must be an unmet precondition, not a warning "
        f"the exit code does not carry:\n{source}"
    )
    assert re.search(r"^\s*ok\b", source, re.MULTILINE) is None, (
        f"the unmeasured branch must not report an ok verdict:\n{source}"
    )
