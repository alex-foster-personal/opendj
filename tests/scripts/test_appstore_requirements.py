"""Store requirements that live in the Tauri config, pinned.

These are not a test of the publishing pipeline, which is deliberately out of
CI (see .agents/skills/ship-appstore/SKILL.md). They pin the two facts in
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
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
def test_entitlements_templates_are_present_and_are_templates(
    name: str, expected_key: str
) -> None:
    """Both templates parse as plists and carry their defining key.

    Parsed rather than grepped so a malformed plist fails here instead of at
    ``codesign``, where the error is considerably less clear.
    """
    path = TAURI_DIR / name
    assert path.is_file(), f"{name} is missing; the signing phase needs it"
    data = plistlib.loads(path.read_bytes())
    assert data.get(expected_key) is True, f"{name} must set {expected_key}"


def test_the_inherit_template_carries_exactly_one_entitlement() -> None:
    """macOS rejects a binary that pairs com.apple.security.inherit with anything.

    Worth its own test because the tempting mistake (adding network.server to
    the engine, which is the process that actually binds the port) produces an
    INVALID signature rather than a more permissive one.
    """
    data = plistlib.loads(
        (TAURI_DIR / "Entitlements.appstore.inherit.template.plist").read_bytes()
    )
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
EXTENSION_ONLY_PREDICATE = (
    "find \"$1\" -type f \\( -name '*.so' -o -name '*.dylib' \\) -print0"
)

darwin_only = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Mach-O is a macOS file format; file(1) reports ELF elsewhere",
)


@pytest.fixture(scope="module")
def payload(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A payload shaped like a staged one, minus the 3000 .py files.

    /bin/ls stands in for every Mach-O: it is genuinely Mach-O, genuinely
    extensionless and present on every macOS host, which is exactly the
    shape the old predicate missed.
    """
    if sys.platform != "darwin":
        pytest.skip("Mach-O fixture needs a macOS host")
    root = tmp_path_factory.mktemp("payload")
    (root / "runtime/bin").mkdir(parents=True)
    (root / "runtime/lib/python3.11/lib-dynload").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "pylib/pkg").mkdir(parents=True)

    # copyfile, not copy2: /bin/ls carries SIP-restricted st_flags that
    # copy2 tries to reproduce and cannot.
    macho = Path("/bin/ls")
    # The interpreter: extensionless, executable, and the whole point.
    shutil.copyfile(macho, root / "runtime/bin/python3.11")
    (root / "runtime/bin/python3.11").chmod(0o755)
    # What the launcher names. stage_runtime copies the tree with
    # symlinks=True, so the payload carries this symlink verbatim.
    (root / "runtime/bin/python3").symlink_to("python3.11")
    shutil.copyfile(macho, root / "runtime/lib/libpython3.11.dylib")
    (root / "runtime/lib/libpython3.11.dylib").chmod(0o755)
    # An extension module at 0644: no exec bit, so only the extension clause
    # catches it. Its presence proves the narrowing did not lose the files
    # the old predicate did find.
    ext = root / "runtime/lib/python3.11/lib-dynload/_crypt.cpython-311-darwin.so"
    shutil.copyfile(macho, ext)
    ext.chmod(0o644)

    launcher = root / "bin/opendj-engine"
    launcher.write_text("#!/bin/sh\nexec \"$payload/runtime/bin/python3\" -m x\n")
    launcher.chmod(0o755)

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
        ["bash", "-c", f'. "{MACHO_LIB}"; macho_files "$1"', "_", str(payload)],
        capture_output=True,
        check=True,
    )
    return sorted(
        str(Path(p).relative_to(payload))
        for p in result.stdout.decode().split("\0")
        if p
    )


@darwin_only
def test_the_extensionless_interpreter_is_discovered(payload: Path) -> None:
    """The regression, stated as the presence of the good thing."""
    found = _macho_files(payload)
    assert "runtime/bin/python3.11" in found, (
        "the bundled interpreter is not in the signing set. It carries no "
        "extension, so an extension-only predicate misses it, and the "
        "launcher execs it: unsigned, the App Store rejects the build. "
        f"Found: {found}"
    )


@darwin_only
def test_the_old_predicate_missed_only_the_interpreter(payload: Path) -> None:
    """The control: the replaced predicate still fires, and still misses one.

    Without this, "the new helper finds 4 files" says nothing about what the
    old one did. Run side by side, the old predicate finds the dylib and the
    .so -- proving it works -- and misses the interpreter alone, which is
    the entire defect and the entire fix.
    """
    result = subprocess.run(
        ["bash", "-c", EXTENSION_ONLY_PREDICATE, "_", str(payload)],
        capture_output=True,
        check=True,
    )
    old = sorted(
        str(Path(p).relative_to(payload))
        for p in result.stdout.decode().split("\0")
        if p
    )
    assert old, "the control predicate found nothing at all; the fixture is broken"
    assert "runtime/lib/libpython3.11.dylib" in old, (
        f"the control predicate cannot find a .dylib, so it is not measuring "
        f"anything: {old}"
    )
    assert "runtime/bin/python3.11" not in old, (
        "the control predicate found the interpreter, which it cannot do by "
        "extension. The fixture no longer reproduces the bug."
    )
    assert set(_macho_files(payload)) - set(old) == {"runtime/bin/python3.11"}, (
        "the fix must add the interpreter and nothing else; anything more "
        "means the helper is now handing codesign files it should not."
    )


@darwin_only
def test_the_shell_launcher_is_not_a_signing_candidate(payload: Path) -> None:
    """bin/opendj-engine is #!/bin/sh, and codesign rejects a script.

    The build asserts the launcher EXISTS instead, and lets the outer
    bundle's CodeResources seal it as the resource it is.
    """
    assert "bin/opendj-engine" not in _macho_files(payload), (
        "the executable-bit clause picked up the shell launcher; file(1) is "
        "what has to reject it, or codesign is handed a script"
    )


@darwin_only
def test_a_static_archive_is_not_a_signing_candidate(payload: Path) -> None:
    """A .a is an ar archive, not Mach-O, and codesign refuses it."""
    assert "pylib/pkg/libstatic.a" not in _macho_files(payload)


@darwin_only
def test_the_interpreter_symlink_is_not_signed_twice(payload: Path) -> None:
    """runtime/bin/python3 points at the versioned binary already in the set."""
    found = _macho_files(payload)
    assert "runtime/bin/python3" not in found, (
        "the symlink is in the set alongside its target, so codesign signs "
        f"one file twice under a name it does not record: {found}"
    )


@pytest.mark.parametrize("script", [SHIP_APPSTORE, SIGN_DEVELOPER_ID])
def test_both_signing_scripts_use_the_shared_helper(script: Path) -> None:
    """One definition, or the two paths drift apart again.

    They already did: the App Store copy narrowed to extensions while the
    Developer ID copy confirmed with file(1), and only one of them signed
    the interpreter.
    """
    body = script.read_text()
    assert "lib/macho.sh" in body, (
        f"{script.name} does not source scripts/lib/macho.sh. Both signing "
        "paths must find the same Mach-O files."
    )
    assert "macho_files" in body, f"{script.name} never calls macho_files"


@pytest.mark.parametrize("script", [MACHO_LIB, SHIP_APPSTORE, SIGN_DEVELOPER_ID])
def test_the_signing_scripts_parse(script: Path) -> None:
    """bash -n, because none of this path runs in CI to find a typo for us."""
    subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True)
