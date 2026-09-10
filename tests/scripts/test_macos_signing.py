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
import shlex
import shutil
import subprocess
import sys
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


# ----- the RETURN trap in cmd_notarize_app --------------------------------
#
# macOS `/usr/bin/env bash` is bash 3.2, and a RETURN trap set inside a
# function is not scoped to that function: it stays armed and fires again when
# the CALLER returns. `cmd_notarize_app` cleaned up its temp zip that way with
# a `local`, so the second firing dereferenced a name that was gone by then and
# `set -u` killed a run that had already notarized, stapled and passed spctl.
# Cost of the defect is a whole build, roughly 8 minutes, spent and thrown away
# AFTER the expensive part succeeded.
#
# The harness below is deliberately variable-agnostic: it lifts the allocation
# line and the trap line out of the real function and runs them under the real
# nesting, so it keeps testing whatever shape the script actually ships rather
# than a copy of one that was correct once.
#
# Single-line acceptance checks:
#
# - if the shipped trap statement can fire a second time and reference a name
#   that is gone by then, a notarized build dies after its own success -> broken.
# - if the harness stops reproducing that failure with the PRE-FIX pair, the
#   guard is no longer measuring anything -> broken.
# - if the shipped pair stops deleting the temp archive, it traded a crash for
#   a zip of the whole app bundle leaked into TMPDIR on every signed build
#   -> broken.
# - if a TMPDIR containing an apostrophe breaks the cleanup, the path is being
#   re-parsed as code somewhere it should be read as data -> broken.

# The declaration line matters as much as the trap line: it is `local` that
# makes the name vanish before the second firing. A harness that lifts only the
# assignment turns `zip` into a global and the defect evaporates, which is a
# check that cannot fail. Caught by mutating this file's own guard, so these
# constants and the extraction below both carry the whole setup block.
PRE_FIX_SETUP = """    local zip out started elapsed
    zip=$(mktemp "${TMPDIR:-/tmp}/opendj-notary-app.XXXXXX.zip")
    trap 'rm -f "$zip"' RETURN"""

# The shape this branch pinned first: the path expanded INTO the trap string.
# It survives the caller's return, which is why it shipped for an hour, but a
# trap string is re-parsed as code when it fires, so an apostrophe anywhere in
# TMPDIR ends the quoting early. Kept as the control for the apostrophe case.
INTERPOLATED_SETUP = """    local zip out started elapsed
    zip=$(mktemp "${TMPDIR:-/tmp}/opendj-notary-app.XXXXXX.zip")
    trap "rm -f '$zip'" RETURN"""

# Same nesting as the real script: the subcommand function is called from
# main(), and main() is the last thing the script does. TMPDIR is redirected
# so the temp archive lands somewhere the test can inspect, and NOTARY_APP_ZIP
# is predeclared so `set -u` behaves exactly as it does in the real script,
# which declares it at file scope.
_TRAP_HARNESS = """#!/bin/bash
set -euo pipefail
export TMPDIR={tmpdir}
NOTARY_APP_ZIP=""
cmd_notarize_app() {{
{setup}
    echo NOTARIZED
}}
main() {{
    cmd_notarize_app
    echo MAIN-BODY-FINISHED
}}
main "$@"
echo REACHED-END
"""

TEMP_ARCHIVE_GLOB = "opendj-notary-app.*"


def _notarize_function_body() -> str:
    source = SIGN_SCRIPT.read_text()
    start = source.index("\ncmd_notarize_app() {")
    body = source[start:]
    return body[: body.index("\n}\n")]


def _shipped_notarize_setup() -> str:
    """The declaration, allocation and trap statements `cmd_notarize_app` ships.

    Lifted from the script rather than restated here, so reverting the fix
    makes these tests go red instead of leaving a copy of the fixed text
    behind. The slice runs from the `local` declaration that precedes the
    allocation through the trap, because whether the path is `local` is half of
    what is under test.
    """
    lines = _notarize_function_body().splitlines()
    alloc = [
        i for i, line in enumerate(lines) if "mktemp" in line and not line.strip().startswith("#")
    ]
    trap = [
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("trap ") and line.strip().endswith(" RETURN")
    ]
    assert len(alloc) == 1, f"expected one mktemp in cmd_notarize_app, found {alloc}"
    assert len(trap) == 1, f"expected one RETURN trap in cmd_notarize_app, found {trap}"
    decl = [i for i, line in enumerate(lines[: alloc[0]]) if line.strip().startswith("local ")]
    start = decl[-1] if decl else alloc[0]
    block = [line for line in lines[start : trap[0] + 1] if not line.strip().startswith("#")]
    return "\n".join(block)


def _run_trap_harness(setup: str, tmpdir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    tmpdir.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "harness.sh"
    script.write_text(_TRAP_HARNESS.format(tmpdir=shlex.quote(str(tmpdir)), setup=setup))
    script.chmod(0o755)
    return subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)


needs_system_bash_3 = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/bin/bash").exists(),
    reason="pins the RETURN-trap scoping of macOS's own /bin/bash, which is what ships this dmg",
)


@pytest.mark.requirement("INSTALL-04")
@needs_system_bash_3
def test_the_pre_fix_trap_shape_still_reproduces_the_double_fire(tmp_path: Path) -> None:
    """Instrument check: without this failing, the guards below prove nothing.

    A green result here would mean the harness no longer exercises the defect,
    so a reverted fix could sail through the next tests unnoticed.
    """
    tmpdir = tmp_path / "tmp"
    result = _run_trap_harness(PRE_FIX_SETUP, tmpdir, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"the pre-fix shape did not fail: {combined}"
    assert "unbound variable" in combined, combined
    # And it died AFTER the work it was protecting had already succeeded,
    # which is the whole reason this costs a build rather than catching one.
    assert "NOTARIZED" in result.stdout
    assert "MAIN-BODY-FINISHED" in result.stdout
    assert "REACHED-END" not in result.stdout


@pytest.mark.requirement("INSTALL-04")
@needs_system_bash_3
def test_the_shipped_notarize_trap_survives_the_callers_return(tmp_path: Path) -> None:
    """The shipped statements run to completion under the same nesting."""
    tmpdir = tmp_path / "tmp"
    result = _run_trap_harness(_shipped_notarize_setup(), tmpdir, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "REACHED-END" in result.stdout, combined
    assert "unbound variable" not in combined


@pytest.mark.requirement("INSTALL-04")
@needs_system_bash_3
def test_the_shipped_notarize_trap_still_deletes_its_temp_archive(tmp_path: Path) -> None:
    """The fix must not have bought its survival by dropping the cleanup.

    Asserted on the CONTENTS of TMPDIR rather than on a recorded path, so this
    stays true whatever the script names the variable.
    """
    tmpdir = tmp_path / "tmp"
    result = _run_trap_harness(_shipped_notarize_setup(), tmpdir, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    leftover = list(tmpdir.glob(TEMP_ARCHIVE_GLOB))
    assert leftover == [], f"the temp archive survived the trap: {leftover}"


@pytest.mark.requirement("INSTALL-04")
@needs_system_bash_3
def test_an_apostrophe_in_tmpdir_does_not_break_the_cleanup(tmp_path: Path) -> None:
    """TMPDIR is environment-derived, so it must never reach the trap as code.

    Raised as a BLOCKING P1 on PR #1634 against the earlier fix, which expanded
    the path into the trap STRING: a trap string is re-parsed when it fires, so
    an apostrophe closed the quoting early. The control below pins that the
    case is real rather than theoretical, and the assertion pins that the
    shipped shape is immune to it.
    """
    quoted_tmpdir = tmp_path / "al'ex tmp"

    # Control: the interpolating shape genuinely breaks here. Without this, a
    # pass below could mean nothing more than that apostrophes are harmless.
    control = _run_trap_harness(INTERPOLATED_SETUP, quoted_tmpdir, tmp_path)
    control_leftover = list(quoted_tmpdir.glob(TEMP_ARCHIVE_GLOB))
    assert control.returncode != 0 or control_leftover != [], (
        "the interpolating shape coped with an apostrophe, so this test is not "
        f"measuring the reported defect: {control.stdout + control.stderr}"
    )

    for stale in quoted_tmpdir.glob(TEMP_ARCHIVE_GLOB):
        stale.unlink()
    result = _run_trap_harness(_shipped_notarize_setup(), quoted_tmpdir, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "REACHED-END" in result.stdout, combined
    leftover = list(quoted_tmpdir.glob(TEMP_ARCHIVE_GLOB))
    assert leftover == [], f"the temp archive survived the trap: {leftover}"
