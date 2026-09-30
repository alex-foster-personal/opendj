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
import re
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
        if k
        not in {
            "MDT_MACOS_SIGNING_IDENTITY",
            "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE",
            "MDT_MACOS_NOTARY_KEYCHAIN",
        }
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
# - if a temp archive left by a failed run blocks the next notarization, every
#   later signed build on that host dies at mkstemp "File exists" -> broken.
# - if `die` mid-notarization leaves the temp archive behind, an 83MB zip leaks
#   per failure and (with a fixed name) poisons the next run -> broken.
# - if any mktemp template carries characters after its X run, BSD mktemp
#   treats it as a literal name -> broken.

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
NOTARY_APP_DIR=""
cmd_notarize_app() {{
{setup}
{body}
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
    # Carry any trap lines straight after the RETURN one (the EXIT trap that
    # cleans up after `die`), so the harness runs every cleanup the script arms.
    end = trap[0] + 1
    while end < len(lines) and lines[end].strip().startswith("trap "):
        end += 1
    block = [line for line in lines[start:end] if not line.strip().startswith("#")]
    return "\n".join(block)


def _run_trap_harness(
    setup: str, tmpdir: Path, tmp_path: Path, body: str = "    echo NOTARIZED"
) -> subprocess.CompletedProcess[str]:
    tmpdir.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "harness.sh"
    script.write_text(
        _TRAP_HARNESS.format(tmpdir=shlex.quote(str(tmpdir)), setup=setup, body=body)
    )
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


# The literal name the pre-fix template produced on macOS, every single time.
PRE_FIX_LITERAL_ARCHIVE = "opendj-notary-app.XXXXXX.zip"


@pytest.mark.requirement("INSTALL-04")
@needs_system_bash_3
def test_a_stale_archive_from_a_failed_run_does_not_block_the_next(tmp_path: Path) -> None:
    """Hit live on silver Thu 10 Sep 2026: one failed submission, then every
    signed build died at `mkstemp failed ... File exists` 3 minutes in."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    (tmpdir / PRE_FIX_LITERAL_ARCHIVE).write_text("left by a failed run\n")

    control = _run_trap_harness(PRE_FIX_SETUP, tmpdir, tmp_path)
    assert control.returncode != 0, "the pre-fix template coped with a stale file"
    assert "File exists" in control.stderr, control.stderr

    result = _run_trap_harness(_shipped_notarize_setup(), tmpdir, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REACHED-END" in result.stdout


@pytest.mark.requirement("INSTALL-04")
@needs_system_bash_3
def test_a_die_mid_notarization_still_deletes_the_temp_archive(tmp_path: Path) -> None:
    """`die` exits, and an exit never fires a RETURN trap."""
    dies = "    touch \"${NOTARY_APP_DIR:-$zip}\"/app.zip 2>/dev/null || true; exit 1"

    control_tmp = tmp_path / "control"
    control = _run_trap_harness(PRE_FIX_SETUP, control_tmp, tmp_path, body="    exit 1")
    assert control.returncode == 1
    assert list(control_tmp.glob(TEMP_ARCHIVE_GLOB)) != [], "control leaked nothing"

    tmpdir = tmp_path / "tmp"
    result = _run_trap_harness(_shipped_notarize_setup(), tmpdir, tmp_path, body=dies)
    assert result.returncode == 1, result.stdout + result.stderr
    leftover = list(tmpdir.glob(TEMP_ARCHIVE_GLOB))
    assert leftover == [], f"die left the temp archive behind: {leftover}"


# A template whose X run is followed by anything before the closing quote.
SUFFIXED_MKTEMP = re.compile(r"mktemp\b[^\n]*?X{3,}[^X\s\"')/]+[\"']")


def test_the_suffixed_template_detector_fires_on_the_known_bad_shapes() -> None:
    """Positive control for the invariant below: it must be able to say no."""
    assert SUFFIXED_MKTEMP.search('zip=$(mktemp "${TMPDIR:-/tmp}/opendj-notary-app.XXXXXX.zip")')
    assert SUFFIXED_MKTEMP.search('m="$(mktemp "${TMPDIR:-/tmp}/opendj-latest.XXXXXX.json")"')
    assert not SUFFIXED_MKTEMP.search('d=$(mktemp -d "${TMPDIR:-/tmp}/opendj-notary-app.XXXXXX")')
    assert not SUFFIXED_MKTEMP.search("mount=$(mktemp -d /tmp/opendj-dmg-verify.XXXXXX)")


@needs_system_bash_3
def test_bsd_mktemp_really_does_not_randomize_a_suffixed_template(tmp_path: Path) -> None:
    """Instrument check: the invariant below guards a real macOS behavior."""
    template = str(tmp_path / "probe.XXXXXX.zip")
    first = subprocess.run(["mktemp", template], capture_output=True, text=True, check=True)
    assert Path(first.stdout.strip()).name == "probe.XXXXXX.zip"
    second = subprocess.run(["mktemp", template], capture_output=True, text=True, check=False)
    assert second.returncode != 0 and "File exists" in second.stderr


def test_no_shell_mktemp_template_carries_a_suffix_after_its_xs() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "*.sh", "justfile", "**/justfile"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(tracked) > 20, f"scanned implausibly few files: {tracked}"
    hits = [
        f"{path}:{number}: {line.strip()}"
        for path in tracked
        for number, line in enumerate(
            (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines(), 1
        )
        if SUFFIXED_MKTEMP.search(line)
    ]
    assert hits == [], "\n".join(hits)


# ---------------------------------------------------------------------------
# INSTALL-26: the signed engine may JIT
# ---------------------------------------------------------------------------

ENGINE_ENTITLEMENTS: Path = REPO_ROOT / "apps/desktop/src-tauri/Entitlements.engine.plist"
JIT_ENTITLEMENT = "com.apple.security.cs.allow-unsigned-executable-memory"


def test_engine_entitlements_grant_exactly_unsigned_executable_memory() -> None:
    """If the engine plist loses this key, or swaps it for allow-jit, every numba
    JIT under the hardened runtime is SIGKILLed and own analysis never runs.

    Exactly one key: anything wider is attack surface the engine has not been
    measured to need.
    """
    import plistlib

    entitlements = plistlib.loads(ENGINE_ENTITLEMENTS.read_bytes())
    assert entitlements == {JIT_ENTITLEMENT: True}


def test_the_payload_stage_applies_the_engine_entitlements_and_proves_a_jit() -> None:
    """If `payload` stops calling either step, a build signs an interpreter that
    cannot analyze and still reports success.

    The behavior itself needs a Developer ID certificate and a real payload, so
    it is proved by the build (the JIT smoke runs on every signed `just dmg`);
    this pins that the stage still reaches both, after the unentitled pass.
    """
    source = SIGN_SCRIPT.read_text()
    start = source.index("cmd_payload() {")
    body = source[start : source.index("\n}\n", start)]
    unentitled = body.index("codesign --force --timestamp --options runtime")
    entitle = body.index('_sign_engine_executables "$payload"')
    smoke = body.index('_prove_signed_engine_can_jit "$payload"')
    assert unentitled < entitle < smoke
    entitlements_path = "$SCRIPT_DIR/../apps/desktop/src-tauri/Entitlements.engine.plist"
    assert f'ENGINE_ENTITLEMENTS="{entitlements_path}"' in source


# The notary --keychain tests moved to test_macos_signing_notary_keychain.py
# (this file was at 547 lines; appending them would have crossed the repo's
# 600-line-per-test-file ratchet). They import `_sign` and
# `UNRESOLVABLE_IDENTITY` from here.
