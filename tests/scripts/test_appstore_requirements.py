"""Store requirements that live in the Tauri config, pinned.

These are not a test of the publishing pipeline, which is deliberately out of
CI (see (private path)). They pin the two facts in
``tauri.conf.json`` that App Store Connect refuses a build over, because both
are one-line reverts away and neither failure is visible until an upload is
rejected 20 minutes into a release.

  [if] bundle.category is removed, App Store Connect rejects the build for a
       missing LSApplicationCategoryType -> test_category_is_declared
  [if] minimumSystemVersion drops below 11.0 while the product ships arm64
       only, the store offers the app to Intel Macs that cannot launch it
       -> test_minimum_system_version_matches_the_shipped_architecture
  [if] the entitlements templates are deleted or rendered unusable, the
       signing phase cannot produce a sandboxed bundle
       -> test_entitlements_templates_are_present_and_are_templates
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.platform_capabilities import posix_bash_or_skip
from tests.scripts.macho_fixtures import BUNDLE_SO, DYLIB, EXECUTABLE, hydrate

TAURI_DIR = Path(__file__).resolve().parents[2] / "apps/desktop/src-tauri"
CONF = TAURI_DIR / "tauri.conf.json"


@pytest.fixture(scope="module")
def bundle() -> dict:
    return json.loads(CONF.read_text())["bundle"]


def test_category_is_declared(bundle: dict) -> None:
    """LSApplicationCategoryType is mandatory for a store submission."""
    assert bundle.get("category"), (
        "bundle.category is missing from tauri.conf.json. Tauri maps it to "
        "LSApplicationCategoryType, which App Store Connect requires; a build "
        "without it is rejected at upload."
    )


def test_minimum_system_version_matches_the_shipped_architecture(
    bundle: dict,
) -> None:
    """arm64 did not exist before macOS 11, so 10.x advertises a lie."""
    raw = bundle["macOS"]["minimumSystemVersion"]
    major = int(str(raw).split(".")[0])
    assert major >= 11, (
        f"minimumSystemVersion is {raw}, but the product ships arm64 only "
        "(the dmg recipe passes no --target, so it builds for the host). "
        "Anything below 11.0 offers the app to Intel Macs that cannot run it. "
        "Either raise this, or start building universal binaries."
    )


@pytest.mark.parametrize(
    ("name", "expected_key"),
    [
        ("Entitlements.appstore.template.plist", "com.apple.security.app-sandbox"),
        ("Entitlements.appstore.inherit.template.plist", "com.apple.security.inherit"),
    ],
)
def test_entitlements_templates_are_present_and_are_templates(name: str, expected_key: str) -> None:
    """Both templates parse as plists and carry their defining key.

    Parsed rather than grepped so a malformed plist fails here instead of at
    ``codesign``, where the error is considerably less clear.
    """
    path = TAURI_DIR / name
    assert path.is_file(), f"{name} is missing; the signing phase needs it"
    data = plistlib.loads(path.read_bytes())
    assert data.get(expected_key) is True, f"{name} must set {expected_key}"


def test_the_store_parent_grants_audio_input_for_set_recording() -> None:
    """SET-11: REC records through odj-audio, which inherits the parent's sandbox.

    The engine executables carry only com.apple.security.inherit in the store
    build, so the microphone grant must be on the parent or REC records nothing.
    """
    data = plistlib.loads((TAURI_DIR / "Entitlements.appstore.template.plist").read_bytes())
    assert data.get("com.apple.security.device.audio-input") is True, (
        "the App Store parent entitlements must set "
        "com.apple.security.device.audio-input: set recording captures in a "
        "payload executable that inherits this sandbox"
    )


def test_the_inherit_template_carries_exactly_one_entitlement() -> None:
    """macOS rejects a binary that pairs com.apple.security.inherit with anything.

    Worth its own test because the tempting mistake (adding network.server to
    the engine, which is the process that actually binds the port) produces an
    INVALID signature rather than a more permissive one.
    """
    data = plistlib.loads((TAURI_DIR / "Entitlements.appstore.inherit.template.plist").read_bytes())
    assert list(data) == ["com.apple.security.inherit"], (
        "the inherited-sandbox template must carry com.apple.security.inherit "
        f"and nothing else, got {sorted(data)}. A child of a sandboxed app "
        "receives the parent's entitlements through inheritance; declaring "
        "them again makes the signature invalid."
    )


# ----- payload Mach-O discovery ------------------------------------------
#
# The Mac App Store build signs every Mach-O inside
# Contents/Resources/payload with the inherited-sandbox entitlement before it
# seals the outer bundle. It used to find them with an extension-only
# predicate (-name '*.so' -o -name '*.dylib'), which silently skipped the
# bundled CPython at runtime/bin/python3.N: the interpreter carries no
# extension, and it is the process the launcher execs, so it is the one
# binary the inherited sandbox has to land on. An unsigned bundled
# interpreter is an App Store rejection.
#
# Both signing scripts now route through scripts/lib/macho.sh, so these
# exercise that one helper as a shell function against a payload fixture
# shaped like the real thing.
#
#   [if] macho_files omits an extensionless Mach-O, the bundled interpreter
#        ships unsigned -> test_the_extensionless_interpreter_is_discovered
#   [if] macho_files hands the #!/bin/sh launcher to codesign, the build
#        signs a script and reports it as a Mach-O
#        -> test_the_shell_launcher_is_not_a_signing_candidate
#   [if] macho_files follows runtime/bin/python3, the same file is signed
#        twice under a name codesign does not record
#        -> test_the_interpreter_symlink_is_not_signed_twice
#   [if] either signing script grows its own predicate again, the two paths
#        drift back apart -> test_both_signing_scripts_use_the_shared_helper

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHO_LIB = REPO_ROOT / "scripts/lib/macho.sh"
SHIP_APPSTORE = REPO_ROOT / "scripts/ship_appstore.sh"
SIGN_DEVELOPER_ID = REPO_ROOT / "scripts/sign_macos_developer_id.sh"

# The old, defective predicate, kept verbatim as a control. It must still
# find the files it always found -- a probe that finds nothing proves
# nothing -- while missing exactly the interpreter.
EXTENSION_ONLY_PREDICATE = "find \"$1\" -type f \\( -name '*.so' -o -name '*.dylib' \\) -print0"

# The fixtures are real, loadable Mach-O files captured from Apple's linker
# and verified against a manifest checksum, not constructed headers: AGENTS.md
# forbids stubs and permits captured real artifacts consumed through
# production paths. They also need no macOS host, which matters here -- the
# pytest lanes are ubuntu-latest and macos-packaging.yml runs only
# test_engine_payload*.py and test_desktop_lane_config.py, so a darwin-only
# marker would mean these assertions never execute in CI at all.
#
# Nothing below invokes codesign or otool. These exercise DISCOVERY, which is
# where the bug was.


@pytest.fixture(scope="module")
def payload(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A payload shaped like a staged one, minus the 3000 .py files."""
    root = tmp_path_factory.mktemp("payload")
    # The interpreter: extensionless, executable, and the whole point.
    hydrate(EXECUTABLE, root / "runtime/bin/python3.11")
    # What the launcher names. stage_runtime copies the tree with
    # symlinks=True, so the payload carries this symlink verbatim.
    (root / "runtime/bin/python3").symlink_to("python3.11")
    hydrate(DYLIB, root / "runtime/lib/libpython3.11.dylib")
    # An extension module at 0644: no exec bit, so only the extension clause
    # catches it. Its presence proves the narrowing did not lose the files
    # the old predicate did find.
    hydrate(
        BUNDLE_SO,
        root / "runtime/lib/python3.11/lib-dynload/_crypt.cpython-311-darwin.so",
        mode=0o644,
    )

    launcher = root / "bin/opendj-engine"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec "$payload/runtime/bin/python3" -m x\n')
    launcher.chmod(0o755)

    (root / "pylib/pkg").mkdir(parents=True)
    (root / "pylib/pkg/module.py").write_text("x = 1\n")
    subprocess.run(
        ["ar", "rcs", str(root / "pylib/pkg/libstatic.a"), str(root / "pylib/pkg/module.py")],
        check=True,
        capture_output=True,
    )
    return root


def _macho_files(payload: Path) -> list[str]:
    """Run the shared helper and read back its NUL-separated output."""
    result = subprocess.run(
        [posix_bash_or_skip(), "-c", f'. "{MACHO_LIB}"; macho_files "$1"', "_", str(payload)],
        capture_output=True,
        check=True,
    )
    return sorted(str(Path(p).relative_to(payload)) for p in result.stdout.decode().split("\0") if p)


def test_the_fixture_is_recognizable_as_macho(payload: Path) -> None:
    """The control for every assertion below: file(1) must see these at all.

    If a host's file(1) did not recognize the fixture, every "not found"
    result below would be vacuously true, so this fails loudly here rather
    than passing quietly there.
    """
    described = subprocess.run(
        ["file", "-b", str(payload / "runtime/bin/python3.11")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Mach-O" in described, (
        f"file(1) does not recognize the fixture as Mach-O ({described!r}); "
        "the discovery assertions below would pass without measuring anything"
    )


def test_the_extensionless_interpreter_is_discovered(payload: Path) -> None:
    """The regression, stated as the presence of the good thing."""
    found = _macho_files(payload)
    assert "runtime/bin/python3.11" in found, (
        "the bundled interpreter is not in the signing set. It carries no "
        "extension, so an extension-only predicate misses it, and the "
        "launcher execs it: unsigned, the App Store rejects the build. "
        f"Found: {found}"
    )


def test_the_old_predicate_missed_only_the_interpreter(payload: Path) -> None:
    """The control: the replaced predicate still fires, and still misses one.

    Without this, "the new helper finds 3 files" says nothing about what the
    old one did. Run side by side, the old predicate finds the dylib and the
    .so -- proving it works -- and misses the interpreter alone, which is
    the entire defect and the entire fix.
    """
    result = subprocess.run(
        [posix_bash_or_skip(), "-c", EXTENSION_ONLY_PREDICATE, "_", str(payload)],
        capture_output=True,
        check=True,
    )
    old = sorted(str(Path(p).relative_to(payload)) for p in result.stdout.decode().split("\0") if p)
    assert old, "the control predicate found nothing at all; the fixture is broken"
    assert "runtime/lib/libpython3.11.dylib" in old, (
        f"the control predicate cannot find a .dylib, so it is not measuring anything: {old}"
    )
    assert "runtime/bin/python3.11" not in old, (
        "the control predicate found the interpreter, which it cannot do by "
        "extension. The fixture no longer reproduces the bug."
    )
    assert set(_macho_files(payload)) - set(old) == {"runtime/bin/python3.11"}, (
        "the fix must add the interpreter and nothing else; anything more "
        "means the helper is now handing codesign files it should not."
    )


def test_the_shell_launcher_is_not_a_signing_candidate(payload: Path) -> None:
    """bin/opendj-engine is #!/bin/sh, and codesign rejects a script.

    The build asserts the launcher EXISTS instead, and lets the outer
    bundle's CodeResources seal it as the resource it is.
    """
    assert "bin/opendj-engine" not in _macho_files(payload), (
        "the executable-bit clause picked up the shell launcher; file(1) is "
        "what has to reject it, or codesign is handed a script"
    )


def test_a_static_archive_is_not_a_signing_candidate(payload: Path) -> None:
    """A .a is an ar archive, not Mach-O, and codesign refuses it."""
    assert "pylib/pkg/libstatic.a" not in _macho_files(payload)


def test_the_interpreter_symlink_is_not_signed_twice(payload: Path) -> None:
    """runtime/bin/python3 points at the versioned binary already in the set."""
    found = _macho_files(payload)
    assert "runtime/bin/python3" not in found, (
        "the symlink is in the set alongside its target, so codesign signs "
        f"one file twice under a name it does not record: {found}"
    )


# ----- preflight counts what --build signs --------------------------------
#
# check_payload reports how many Mach-O files --build will sign. It counted
# with the extension-only predicate, so a payload holding nothing but the
# extensionless interpreter reported 0: a zero that reads as "clean" and
# means "not measured". Worse, a rename left it testing a variable local to
# build(), so under `set -u` the whole preflight aborted with an unbound
# variable -- and preflight is the DEFAULT mode that --build and --all both
# run first.
#
#   [if] check_payload reads a variable it does not set, every mode dies
#        -> test_the_payload_check_survives_a_staged_payload
#   [if] it counts by extension, it reports 0 for a real payload
#        -> test_the_payload_check_counts_the_interpreter


def _run_check_payload(tmp_path: Path, payload: Path | None) -> subprocess.CompletedProcess[str]:
    """Run check_payload out of the real script, against a payload of our own.

    Extracted rather than invoked through --preflight because the full
    preflight scans the whole source tree for sandbox reachability and takes
    minutes. The extraction is asserted non-empty by the caller, so a rename
    fails this loudly instead of silently testing nothing.
    """
    body = re.search(r"^check_payload\(\) \{.*?^\}", SHIP_APPSTORE.read_text(), re.MULTILINE | re.DOTALL)
    assert body, "check_payload is no longer a top-level function in ship_appstore.sh"
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'TAURI_DIR="{tmp_path}/tauri"\n'
        f'. "{MACHO_LIB}"\n'
        "ok()      { printf '[OK] %s\\n' \"$1\"; }\n"
        "gap()     { printf '[WARN] %s\\n' \"$1\"; }\n"
        "section() { printf '=== %s ===\\n' \"$1\"; }\n"
        f"{body.group(0)}\n"
        "check_payload\n"
    )
    staged = tmp_path / "tauri/payload"
    staged.mkdir(parents=True)
    if payload is not None:
        shutil.copytree(payload, staged, dirs_exist_ok=True, symlinks=True)
    return subprocess.run([posix_bash_or_skip(), str(harness)], capture_output=True, text=True, check=False)


def test_the_payload_check_survives_a_staged_payload(tmp_path: Path, payload: Path) -> None:
    """A staged payload is the normal state, and it used to abort every mode."""
    result = _run_check_payload(tmp_path, payload)
    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"


def test_the_payload_check_counts_the_interpreter(tmp_path: Path, payload: Path) -> None:
    """The number preflight prints must be the number --build will sign."""
    result = _run_check_payload(tmp_path, payload)
    assert "payload staged, 3 Mach-O" in result.stdout, (
        "preflight must count by content, like the signing pass. An "
        f"extension-only count reports 2 here and 0 for a runtime-only "
        f"payload. Got: {result.stdout!r}"
    )


def test_an_empty_payload_directory_is_reported_not_passed(tmp_path: Path) -> None:
    """Zero is an error signature here, not a clean bill."""
    result = _run_check_payload(tmp_path, None)
    assert result.returncode == 0
    assert "no Mach-O files found" in result.stdout, result.stdout


@pytest.mark.parametrize("script", [SHIP_APPSTORE, SIGN_DEVELOPER_ID])
def test_both_signing_scripts_use_the_shared_helper(script: Path) -> None:
    """One definition, or the two paths drift apart again.

    They already did: the App Store copy narrowed to extensions while the
    Developer ID copy confirmed with file(1), and only one of them signed
    the interpreter.
    """
    body = script.read_text()
    assert "lib/macho.sh" in body, (
        f"{script.name} does not source scripts/lib/macho.sh. Both signing paths must find the same Mach-O files."
    )
    assert "macho_files" in body, f"{script.name} never calls macho_files"


@pytest.mark.parametrize(
    "script",
    [MACHO_LIB, SHIP_APPSTORE, SIGN_DEVELOPER_ID, REPO_ROOT / "scripts/lib/sandboxed_jit_smoke.sh"],
)
def test_the_signing_scripts_parse(script: Path) -> None:
    """bash -n, because none of this path runs in CI to find a typo for us."""
    subprocess.run([posix_bash_or_skip(), "-n", str(script)], check=True, capture_output=True)


# ----- the engine JIT under the App Store signing (INSTALL-3287) ------------
#
# Measured Wed 16 Sep 2026 with this repo's payload under a sandboxed parent:
# an inherit-signed interpreter under the hardened runtime is SIGKILLed the
# moment numba JITs (CODESIGNING, Invalid Page, in llvmlite), and putting
# allow-unsigned-executable-memory on the PARENT does not help because
# hardened-runtime keys are not inherited. The fix signs the payload without
# the hardened runtime, which the store does not require.
#
#   [if] the payload is signed with the hardened runtime again, Open DJ's own
#        analysis dies in every store install
#        -> test_the_payload_is_signed_without_the_hardened_runtime
#   [if] the build stops proving the JIT before sealing, the regression ships
#        -> test_the_build_proves_a_sandboxed_jit_before_sealing_the_bundle
#   [if] the verdict accepts a kill, an unsandboxed run, a child that died
#        after JIT_OK, or an unmeasured probe, the proof passes without
#        proving
#        -> test_the_verdict_*
#   [if] the probe can only sign with the store distribution identity, which
#        can refuse to launch locally, every real --build aborts UNKNOWN
#        -> test_the_probe_*identity*, test_the_probe_re_signs_the_clone_*

JIT_SMOKE_LIB = REPO_ROOT / "scripts/lib/sandboxed_jit_smoke.sh"

# Real probe output from the measurement, verbatim apart from the version.
PROBE_JIT_RAN_SANDBOXED = "JIT_OK 0.65.1 sandboxed=True\nCHILD_EXIT 0"
PROBE_CHILD_KILLED = "CHILD_SIGNAL 9"
PROBE_JIT_RAN_UNSANDBOXED = "JIT_OK 0.65.1 sandboxed=False\nCHILD_EXIT 0"
PROBE_PARENT_NEVER_RAN = ""
# Not observed: constructed from the parent's two report shapes, for a runtime
# that prints JIT_OK and then dies on the way out.
PROBE_JIT_RAN_THEN_CRASHED = "JIT_OK 0.65.1 sandboxed=True\nCHILD_SIGNAL 11"
PROBE_JIT_RAN_THEN_EXITED_NONZERO = "JIT_OK 0.65.1 sandboxed=True\nCHILD_EXIT 3"


def _build_body() -> str:
    body = SHIP_APPSTORE.read_text()
    return body[body.index("\nbuild() {") : body.index("\n#----- package")]


def _payload_signing_command(body: str) -> str:
    start = body.index('macho_files "$payload" | xargs -0 -n 1')
    return body[start : body.index("\n    ok ", start)]


@pytest.mark.requirement("INSTALL-3287")
def test_the_payload_is_signed_without_the_hardened_runtime() -> None:
    """[if] the payload regains runtime or the outer bundle loses it [then] fail, [else stop]."""
    body = _build_body()
    payload_signing = _payload_signing_command(body)
    assert "Entitlements.inherit.plist" in payload_signing, payload_signing
    assert "--options runtime" not in payload_signing, (
        "the payload is signed with the hardened runtime, which kills numba's "
        "JIT under the App Store signing (INSTALL-3287)"
    )
    outer = body[body.index("# The outer bundle LAST") :]
    assert "--options runtime" in outer.split('ok "signed the app bundle"')[0], (
        "the fix is scoped to the payload; the outer bundle keeps the hardened runtime"
    )


@pytest.mark.requirement("INSTALL-3287")
def test_the_build_proves_a_sandboxed_jit_before_sealing_the_bundle() -> None:
    """[if] the seal precedes the static check or fatal JIT proof [then] fail, [else stop]."""
    body = _build_body()
    signed = body.index('macho_files "$payload" | xargs -0 -n 1')
    static = body.index("grep -q 'flags=.*runtime' <<<\"$interpreter_signature\"")
    proof = body.index('prove_sandboxed_engine_can_jit "$payload" "$MDT_MAS_APP_CERT"')
    sealed = body.index("# The outer bundle LAST")
    assert signed < static < proof < sealed
    call = body[proof : body.index("\n", body.index("\n", proof) + 1)]
    assert "|| exit 1" in call
    # The store identity goes in as the PAYLOAD's identity, and the inherit
    # entitlements go in so a clone re-sign carries the same shape.
    assert '"$BUILD_DIR/Entitlements.inherit.plist"' in call, call
    assert '. "$REPO_ROOT/scripts/lib/sandboxed_jit_smoke.sh"' in SHIP_APPSTORE.read_text()


@pytest.mark.requirement("INSTALL-3287")
def test_the_probe_strips_only_keys_a_profile_must_authorize() -> None:
    """Too few and the probe cannot launch; too many and it stops being the
    app's sandbox. Each stripped key must really be in the app template.

    [if] the stripped set is not exactly the two profile-authorized keys [then] fail, [else stop].
    """
    lib = JIT_SMOKE_LIB.read_text()
    keys = re.search(r"SANDBOXED_JIT_PROFILE_KEYS=\(([^)]*)\)", lib).group(1).split()
    assert keys == ["com.apple.application-identifier", "com.apple.developer.team-identifier"]
    template = plistlib.loads((TAURI_DIR / "Entitlements.appstore.template.plist").read_bytes())
    assert set(keys) <= set(template)
    assert template["com.apple.security.app-sandbox"] is True


def _verdict(out: str, rc: int) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail; . "{JIT_SMOKE_LIB}"; sandboxed_jit_verdict "$1" "$2"'
    return subprocess.run(
        [posix_bash_or_skip(), "-c", script, "verdict", out, str(rc)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.requirement("INSTALL-3287")
def test_the_verdict_passes_a_jit_that_ran_inside_the_sandbox() -> None:
    """[if] JIT_OK sandboxed=True and CHILD_EXIT 0 are printed [then] pass, [else stop]."""
    result = _verdict(PROBE_JIT_RAN_SANDBOXED, 0)
    assert result.returncode == 0, result.stderr
    assert "(0.65.1)" in result.stdout


@pytest.mark.requirement("INSTALL-3287")
def test_the_verdict_fails_a_killed_child() -> None:
    """[if] the child is killed before any JIT_OK [then] fail, naming the runtime, [else stop]."""
    result = _verdict(PROBE_CHILD_KILLED, 137)
    assert result.returncode == 1
    assert "hardened runtime killing it" in result.stderr


@pytest.mark.requirement("INSTALL-3287")
def test_the_verdict_fails_a_jit_that_ran_outside_the_sandbox() -> None:
    """An unsandboxed run measures the Developer ID case, not the store's.

    [if] JIT_OK reports sandboxed=False [then] the verdict fails, [else stop].
    """
    assert _verdict(PROBE_JIT_RAN_UNSANDBOXED, 0).returncode == 1


@pytest.mark.requirement("INSTALL-3287")
def test_the_verdict_reports_an_unmeasured_probe_as_unknown() -> None:
    """[if] no CHILD_/SPAWN_ line was printed [then] UNKNOWN, exit 2, never a pass, [else stop]."""
    result = _verdict(PROBE_PARENT_NEVER_RAN, 137)
    assert result.returncode == 2
    assert "UNKNOWN" in result.stderr


@pytest.mark.requirement("INSTALL-3287")
@pytest.mark.parametrize(
    ("out", "rc"),
    [
        pytest.param(PROBE_JIT_RAN_THEN_CRASHED, 139, id="killed-after-jit"),
        pytest.param(PROBE_JIT_RAN_THEN_EXITED_NONZERO, 3, id="exited-3-after-jit"),
        pytest.param(PROBE_JIT_RAN_SANDBOXED, 1, id="parent-exit-1-with-child-exit-0"),
    ],
)
def test_the_verdict_fails_a_jit_whose_child_did_not_exit_clean(out: str, rc: int) -> None:
    """A runtime that prints JIT_OK and then dies is not release evidence.

    [if] JIT_OK is sandboxed but CHILD_EXIT 0 is missing or rc is nonzero [then] fail, [else stop].
    """
    result = _verdict(out, rc)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "did not exit 0" in result.stderr
    assert "[OK]" not in result.stdout


# ----- which identity the probe signs with -------------------------------


def _probe_identity(
    tmp_path: Path, payload_identity: str, keychain: str, env_extra: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Resolve the probe identity against a fake `security` that records its calls.

    Returns the process and how many times find-identity was consulted.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "security.calls"
    log.write_text("")
    identities = tmp_path / "identities"
    identities.write_text(keychain)
    fake = bin_dir / "security"
    fake.write_text('#!/bin/sh\necho "$*" >> "$FAKE_SECURITY_LOG"\ncat "$FAKE_SECURITY_IDENTITIES"\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "MDT_MAS_PROBE_CERT"}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_SECURITY_LOG"] = str(log)
    env["FAKE_SECURITY_IDENTITIES"] = str(identities)
    env.update(env_extra or {})
    script = f'set -euo pipefail; . "{JIT_SMOKE_LIB}"; sandboxed_jit_probe_identity "$1"'
    result = subprocess.run(
        [posix_bash_or_skip(), "-c", script, "identity", payload_identity],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result, log.read_text().count("find-identity")


DISTRIBUTION = "Apple Distribution: Dev Name (TEAM123456)"
DEVELOPMENT = "Apple Development: Dev Name (TEAM123456)"
DEVELOPER_ID = "Developer ID Application: Dev Name (TEAM123456)"
KEYCHAIN_WITH_DEVELOPMENT = f'  1) 0A1B "{DISTRIBUTION}"\n  2) 2C3D "{DEVELOPMENT}"\n     2 valid identities found\n'
KEYCHAIN_STORE_ONLY = f'  1) 0A1B "{DISTRIBUTION}"\n     1 valid identities found\n'


def _identity_and_reason(result: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    assert result.returncode == 0, result.stderr
    identity, _, reason = result.stdout.rstrip("\n").partition("\t")
    return identity, reason


@pytest.mark.requirement("INSTALL-3287")
@pytest.mark.parametrize(
    "store_identity",
    [DISTRIBUTION, "3rd Party Mac Developer Application: Dev Name (TEAM123456)"],
)
def test_the_probe_signs_with_a_development_identity_under_a_store_certificate(
    tmp_path: Path, store_identity: str
) -> None:
    """Says which identity it chose and why.

    [if] a store identity plus a Development one in the keychain [then] sign with it, [else stop].
    """
    result, consulted = _probe_identity(tmp_path, store_identity, KEYCHAIN_WITH_DEVELOPMENT)
    identity, reason = _identity_and_reason(result)
    assert identity == DEVELOPMENT
    assert "refuse to launch locally" in reason
    assert consulted == 1


@pytest.mark.requirement("INSTALL-3287")
def test_the_probe_falls_back_to_ad_hoc_when_only_the_store_identity_exists(
    tmp_path: Path,
) -> None:
    """Says which identity it chose and why.

    [if] a store identity and no Development one in the keychain [then] sign ad-hoc, [else stop].
    """
    result, consulted = _probe_identity(tmp_path, DISTRIBUTION, KEYCHAIN_STORE_ONLY)
    identity, reason = _identity_and_reason(result)
    assert identity == "-"
    assert "ad-hoc" in reason
    assert consulted == 1


@pytest.mark.requirement("INSTALL-3287")
def test_the_probe_keeps_an_identity_that_launches_locally(tmp_path: Path) -> None:
    """[if] the payload identity runs locally [then] sign with it, keychain unread, [else stop]."""
    result, consulted = _probe_identity(tmp_path, DEVELOPER_ID, KEYCHAIN_WITH_DEVELOPMENT)
    identity, reason = _identity_and_reason(result)
    assert identity == DEVELOPER_ID
    assert "runs locally" in reason
    assert consulted == 0, "the Developer ID measurements in the PR must keep signing byte for byte"


@pytest.mark.requirement("INSTALL-3287")
def test_an_explicit_probe_certificate_wins(tmp_path: Path) -> None:
    """[if] MDT_MAS_PROBE_CERT is set [then] sign with it over any keychain, [else stop]."""
    result, consulted = _probe_identity(
        tmp_path,
        DISTRIBUTION,
        KEYCHAIN_WITH_DEVELOPMENT,
        {"MDT_MAS_PROBE_CERT": "Mac Developer: Other (T)"},
    )
    identity, reason = _identity_and_reason(result)
    assert identity == "Mac Developer: Other (T)"
    assert "MDT_MAS_PROBE_CERT" in reason
    assert consulted == 0


@pytest.mark.requirement("INSTALL-3287")
def test_the_probe_re_signs_the_clone_in_the_store_shape_and_only_the_clone() -> None:
    """[if] a re-sign gains runtime, drops inherit or hits the payload [then] fail, [else stop]."""
    lib = JIT_SMOKE_LIB.read_text()
    start = lib.index("prove_sandboxed_engine_can_jit() {")
    body = lib[start : lib.index("\n}\n", start)]
    guard = body.index('[ "$identity" != "$payload_identity" ]')
    resign = body.index('codesign --force --sign "$identity"', guard)
    command = body[resign : body.index("\n", resign)]
    assert "--options runtime" not in command, command
    assert '--entitlements "$inherit_entitlements"' in command, command
    resign_target = body[body.rindex("macho_files ", guard, resign) : resign]
    assert '"$probe/Contents/Resources/payload"' in resign_target, resign_target
    assert 'macho_files "$payload"' not in body
    # The parent's signing stays on the same resolved identity.
    parent = body[body.index("codesign --force --options runtime --sign") :]
    assert parent.startswith('codesign --force --options runtime --sign "$identity"')
