"""Developer ID signing and notarization gates for the direct-download dmg.

These cover the half of INSTALL-04 that says a signing failure must fail the
build rather than degrade to unsigned. The other half (a notary profile
without an identity) is pinned in test_desktop_lane_config.py, which also
pins the ORDER: that guard runs first, so its message is the one a
half-configured build reports. Tests here must not disturb it.

Nothing here needs a certificate. Every case is a refusal, which is the
point: the refusals are what a machine with no Developer ID can still prove.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if `just dmg` builds an unsigned image when no signing identity is set and
  MDT_SHIP_UNSIGNED is not 1, an unsigned artifact ships by accident rather
  than by choice -> broken.
- if MDT_SHIP_UNSIGNED=1 and a signing identity together produce a build,
  the recipe guessed at contradictory intent instead of refusing -> broken.
- if a signing identity without a notary profile produces a build, an image
  ships that looks signed and is still refused by Gatekeeper -> broken.
- if any refusal happens after the compile starts, the guard is in the wrong
  place and costs a build to discover -> broken.
- if sign_macos_developer_id.sh runs any stage without a signing identity,
  it would report success over unsigned Mach-O files -> broken.
- if it reports success when codesign itself failed, the bundle gets sealed
  around an unsigned binary and the notary service rejects it later -> broken.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SIGN_SCRIPT: Path = REPO_ROOT / "scripts/sign_macos_developer_id.sh"

# A syntactically plausible Developer ID identity that resolves to nothing on
# any machine. Guards must refuse on the SHAPE of the configuration, before
# anything tries to resolve it.
UNRESOLVABLE_IDENTITY = "Developer ID Application: Nobody (XXXXXXXXXX)"


def _just_dmg(**overrides: str) -> subprocess.CompletedProcess[str]:
    """Run `just dmg` with a scrubbed signing environment plus overrides.

    MDT_LANE_LABEL is scrubbed because these target the signing guards, not
    OPS-09's lane double-intent gate, and must not depend on the invoking
    shell being free of a stray lane label.
    """
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed")
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "MDT_LANE_LABEL",
            "MDT_MACOS_SIGNING_IDENTITY",
            "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE",
            "MDT_SHIP_UNSIGNED",
        }
    }
    env.update(overrides)
    return subprocess.run(
        [just, "dmg"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _sign(*args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"MDT_MACOS_SIGNING_IDENTITY", "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE"}
    }
    env.update(overrides)
    return subprocess.run(
        [str(SIGN_SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# ----- justfile guards ---------------------------------------------------


@pytest.mark.requirement("INSTALL-04")
def test_no_signing_identity_refuses_the_build() -> None:
    """Refusal is the DEFAULT. Unsigned has to be asked for, never fallen into."""
    result = _just_dmg()
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "no Developer ID signing identity" in combined
    # The error must name both ways forward, or it strands the operator.
    assert "MDT_MACOS_SIGNING_IDENTITY" in combined
    assert "MDT_SHIP_UNSIGNED=1" in combined
    # It must have refused BEFORE spending a build.
    assert "Compiling" not in combined


@pytest.mark.requirement("INSTALL-04")
def test_unsigned_escape_hatch_with_an_identity_is_refused() -> None:
    """Contradictory intent is refused rather than resolved by guesswork."""
    result = _just_dmg(MDT_SHIP_UNSIGNED="1", MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "both set" in combined
    assert "Compiling" not in combined


@pytest.mark.requirement("INSTALL-04")
def test_signing_identity_without_a_notary_profile_is_refused() -> None:
    """A Developer ID signature nobody notarized still fails Gatekeeper."""
    result = _just_dmg(MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is not" in combined
    assert "Compiling" not in combined


# ----- sign_macos_developer_id.sh ----------------------------------------


@pytest.mark.requirement("INSTALL-04")
@pytest.mark.parametrize(
    "stage", ["payload", "verify-dmg-app", "notarize-app", "dmg", "notarize"]
)
def test_every_signing_stage_refuses_without_an_identity(stage: str) -> None:
    """No stage may run unsigned, and each must name the variable that is missing.

    The path argument points at nothing on purpose: the identity check has to
    come FIRST, or the operator is sent hunting for a path problem when the
    real fault is a missing certificate.
    """
    result = _sign(stage, "/tmp/does-not-exist")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "MDT_MACOS_SIGNING_IDENTITY is not set" in combined


@pytest.mark.requirement("INSTALL-04")
def test_notarize_names_the_missing_notary_profile() -> None:
    result = _sign(
        "notarize",
        "/tmp/does-not-exist",
        MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is not set" in combined
    assert "store-credentials" in combined


@pytest.mark.requirement("INSTALL-04")
def test_an_unknown_stage_is_refused() -> None:
    result = _sign("staple-everything", "/tmp/x")
    assert result.returncode != 0
    assert "unknown subcommand" in result.stdout + result.stderr


# The cases below reach past the environment guards into codesign itself, so
# they need the real macOS toolchain. The fast lane runs on ubuntu-latest,
# where they would assert on a "codesign not found" message and fail for a
# reason that has nothing to do with the behavior under test. Everything above
# this line is pure shell and runs everywhere.
needs_codesign = pytest.mark.skipif(
    shutil.which("codesign") is None,
    reason="needs the macOS codesign toolchain",
)


@pytest.mark.requirement("INSTALL-04")
@needs_codesign
def test_an_empty_payload_scan_is_refused(tmp_path: Path) -> None:
    """A payload with no Mach-O files is not "nothing to do", it is a staging bug.

    Reporting success over it would seal an empty or half-staged payload into
    the bundle and only surface at the notary service.
    """
    result = _sign("payload", str(tmp_path), MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY)
    assert result.returncode != 0
    assert "no Mach-O files" in result.stdout + result.stderr


@pytest.mark.requirement("INSTALL-04")
@needs_codesign
def test_a_codesign_failure_is_never_reported_as_success(tmp_path: Path) -> None:
    """The load-bearing case: codesign fails, so the stage must fail.

    A real Mach-O and a real (unresolvable) identity, so this exercises
    codesign itself rather than a guard. If this ever passes, the pipeline
    can seal an unsigned binary into the bundle and call it signed.
    """
    if not Path("/bin/echo").exists():
        pytest.skip("no system Mach-O available to copy")
    shutil.copy("/bin/echo", tmp_path / "probe-binary")
    result = _sign("payload", str(tmp_path), MDT_MACOS_SIGNING_IDENTITY=UNRESOLVABLE_IDENTITY)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "codesign failed inside the payload" in combined
    assert "[OK]" not in combined
