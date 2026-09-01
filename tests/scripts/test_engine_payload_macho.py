"""Mach-O discovery for the payload's linked-library bar.

Split out of test_engine_payload.py, which crossed the 600-line file-size
ratchet. Registered in macos-packaging.yml's PACKAGING_TESTS alongside it;
the ubuntu fast lane collects all of tests/ and needs no registration.

scan_linked_libraries used to pick candidates with
``path.suffix in (".so", ".dylib")``, so the bar never inspected the bundled
interpreter at runtime/bin/python3.N: it carries no extension. Widening the
set is only half of it -- parse_otool needs the FILETYPE, which the suffix
answered only by convention, and four shipped `.so` files in this repo's own
locked set are MH_DYLIB rather than MH_BUNDLE.

Only the tests that shell out to otool are macOS-gated. Discovery runs
everywhere on constructed 32-byte Mach-O headers, because a darwin marker on
an ubuntu lane is a test that passes by not running.

- if an extensionless Mach-O is not scanned, the interpreter's links go
  unchecked -> broken.
- if a .so that is really a dylib is parsed as a bundle, its install name is
  counted as a dependency -> broken.
- if an executable is parsed as a dylib, its first real dependency is
  silently dropped -> broken.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    DYLIB_FILETYPE,
    MACHO_DESCRIPTION,
    PayloadBuildError,
    describe_macho,
    link_violations,
    macho_candidates,
    macho_filetype,
    parse_otool,
    scan_linked_libraries,
)
from tests.scripts.macho_fixtures import (
    BUNDLE_SO,
    DYLIB,
    DYLIB_NAMED_SO,
    EXECUTABLE,
    MANIFEST_NAME,
    SUPPORTED_MANIFEST_VERSION,
    disposable_fixture_dir,
    hydrate,
    load_manifest,
    verified,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# ----- Mach-O by content, not by extension --------------------------------
#
# scan_linked_libraries used to pick its candidates with
# ``path.suffix in (".so", ".dylib")``, so the linked-library bar never
# inspected the bundled interpreter at runtime/bin/python3.N: it carries no
# extension. Same defect the signing scripts carried, fixed the same way.
#
# Widening the candidate set is only half of it. parse_otool needs to know
# whether otool will open with the file's own install name, and that is a
# question about FILETYPE, which the suffix answered only by convention.
# Measured on this repo's own locked dependency set, four shipped `.so`
# files are MH_DYLIB rather than MH_BUNDLE (pydantic_core, rpds, watchfiles,
# rbox), and protobuf's `_message.abi3.so` -- a dylib whose install name is
# a bazel-out build path -- turns that mismatch into a spurious violation
# that would fail the build.
#
#   [if] an extensionless Mach-O is not scanned, the interpreter's links go
#        unchecked -> test_an_extensionless_macho_is_scanned
#   [if] a .so that is really a dylib is parsed as a bundle, its install
#        name is counted as a dependency
#        -> test_a_dylib_named_so_is_classified_by_filetype
#   [if] an executable is parsed as a dylib, its FIRST real dependency is
#        silently dropped -> test_an_executable_keeps_its_first_dependency

# Observed verbatim from `file -b` on a staged payload, Tue 1 Sep 2026.
EXECUTABLE_DESCRIPTION: str = "Mach-O 64-bit executable arm64"
BUNDLE_DESCRIPTION: str = "Mach-O 64-bit bundle arm64"
SHARED_LIBRARY_DESCRIPTION: str = "Mach-O 64-bit dynamically linked shared library arm64"
# A universal binary repeats a description per slice, inline.
FAT_LIBRARY_DESCRIPTION: str = (
    "Mach-O universal binary with 2 architectures: "
    "[x86_64:Mach-O 64-bit dynamically linked shared library x86_64] "
    "[arm64:Mach-O 64-bit dynamically linked shared library arm64]"
)

EXECUTABLE_OUTPUT: str = (
    "/p/runtime/bin/python3.11:\n"
    "\t/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
    " (compatibility version 150.0.0)\n"
    "\t@executable_path/../lib/libpython3.11.dylib (compatibility version 3.11.0)\n"
    "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
)


def _macho_payload(tmp_path: Path) -> Path:
    """A payload carrying the four shapes the old predicate could not separate.

    Every file is a real, loadable Mach-O from tests/fixtures/macho, verified
    against its manifest checksum and hydrated into this disposable tree. No
    constructed bytes: otool reads these, which is what lets the scan itself
    be exercised rather than only the candidate predicate.
    """
    root = tmp_path / "payload"
    hydrate(EXECUTABLE, root / "runtime/bin/python3.11")
    # stage_runtime copies the tree with symlinks=True, so a payload carries
    # this symlink to the versioned interpreter verbatim.
    (root / "runtime/bin/python3").symlink_to("python3.11")
    hydrate(DYLIB, root / "runtime/lib/libpython3.11.dylib")
    # 0644 on purpose: a CPython extension module has no exec bit, so only
    # the extension clause reaches it.
    hydrate(BUNDLE_SO, root / "pylib/_crypt.cpython-311-darwin.so", mode=0o644)
    # A dylib wearing a .so name: pydantic_core, rpds, watchfiles, rbox and
    # protobuf all ship this shape, and it is where suffix and filetype
    # disagree.
    hydrate(DYLIB_NAMED_SO, root / "pylib/_pydantic_core.cpython-311-darwin.so", mode=0o644)

    launcher = root / "bin/opendj-engine"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec "$payload/runtime/bin/python3" -m x\n')
    launcher.chmod(0o755)
    (root / "pylib/module.py").write_text("x = 1\n")
    return root


@pytest.mark.requirement("INSTALL-10")
def test_the_fixtures_are_real_loadable_macho() -> None:
    """The control every assertion below rests on.

    If file(1) did not recognize these, the not-a-candidate assertions would
    all be vacuously true.

    The assertion is the INVARIANT, not the wording. An earlier revision
    pinned file(1)'s exact string from the manifest and broke on ubuntu,
    where GNU file says "Mach-O 64-bit arm64 executable, flags:<...>" against
    macOS's "Mach-O 64-bit executable arm64". The manifest still records each
    platform-specific string as provenance; nothing asserts on it.
    """
    for name in (EXECUTABLE, DYLIB, BUNDLE_SO, DYLIB_NAMED_SO):
        described = describe_macho(verified(name))
        assert described is not None, f"{name} is not recognized as Mach-O"
        assert MACHO_DESCRIPTION in described, described


@pytest.mark.requirement("INSTALL-10")
def test_the_extensionless_interpreter_is_a_candidate(tmp_path: Path) -> None:
    """The regression, stated as the presence of the good thing."""
    root = _macho_payload(tmp_path)
    found = {p.relative_to(root).as_posix() for p in macho_candidates(root)}
    assert "runtime/bin/python3.11" in found, (
        "the bundled interpreter is not a candidate. It carries no extension, "
        f"so an extension-only predicate never inspects it. Found: {sorted(found)}"
    )


@pytest.mark.requirement("INSTALL-10")
def test_the_old_predicate_missed_only_the_interpreter(tmp_path: Path) -> None:
    """The control: the replaced predicate still fires, and still misses one.

    "The new predicate finds 4 files" says nothing until the old one is run
    beside it. It finds the dylib and both .so files -- proving it works --
    and misses the interpreter alone.
    """
    root = _macho_payload(tmp_path)
    old = {
        p.relative_to(root).as_posix()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink() and p.suffix in (".so", ".dylib")
    }
    # Candidates that file(1) confirms, which is the set the scan reads.
    # macho_candidates alone is the cheap narrowing pass and still contains
    # the shell launcher; describe_macho is what rejects it.
    new = {
        p.relative_to(root).as_posix()
        for p in macho_candidates(root)
        if describe_macho(p) is not None
    }
    assert "runtime/lib/libpython3.11.dylib" in old, (
        f"the control predicate cannot find a .dylib, so it measures nothing: {old}"
    )
    assert new - old == {"runtime/bin/python3.11"}, (
        "the fix must add the interpreter and nothing else; anything more means "
        f"the scan is now reading files it should not. old={old} new={new}"
    )


@pytest.mark.requirement("INSTALL-10")
def test_a_symlink_to_a_candidate_is_not_scanned_twice(tmp_path: Path) -> None:
    """runtime/bin/python3 points at the versioned binary already in the set."""
    root = _macho_payload(tmp_path)
    assert (root / "runtime/bin/python3") not in macho_candidates(root)


@pytest.mark.requirement("INSTALL-10")
def test_a_shell_script_is_not_a_macho(tmp_path: Path) -> None:
    """bin/opendj-engine has the exec bit, so only file(1) can reject it."""
    root = _macho_payload(tmp_path)
    assert describe_macho(root / "bin/opendj-engine") is None
    assert describe_macho(root / "pylib/module.py") is None
    assert describe_macho(root / "runtime/bin/python3.11") is not None


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.skipif(shutil.which("otool") is None, reason="otool is macOS-only")
def test_a_dylib_named_so_is_classified_by_filetype(tmp_path: Path) -> None:
    """The real shape: a genuine MH_DYLIB whose filename says .so.

    pydantic_core, rpds, watchfiles and rbox all ship it, and protobuf's
    _message.abi3.so is the one whose install name is a bazel-out path.
    Reading it as a bundle counts that id line as a dependency.
    """
    root = _macho_payload(tmp_path)
    named_so = root / "pylib/_pydantic_core.cpython-311-darwin.so"
    a_real_bundle = root / "pylib/_crypt.cpython-311-darwin.so"
    assert macho_filetype(named_so) == DYLIB_FILETYPE, (
        "a .so that is really a dylib must be classified by its filetype; the "
        "suffix answers this wrongly for four wheels in this repo's locked set"
    )
    assert macho_filetype(a_real_bundle) == "BUNDLE"
    assert macho_filetype(root / "runtime/bin/python3.11") == "EXECUTE"


@pytest.mark.requirement("INSTALL-10")
def test_an_unclassifiable_candidate_is_refused(tmp_path: Path) -> None:
    """file(1) failing must raise, never quietly drop the file from the scan.

    The exit code cannot carry this: measured on macOS 15, `file` reports a
    missing path by writing "cannot open ..." to STDOUT and exiting 0. A
    guard keyed on the return code would never fire, so this test is
    specifically the one that would catch that mistake.
    """
    missing = tmp_path / "vanished"
    with pytest.raises(PayloadBuildError, match="could not classify"):
        describe_macho(missing)


@pytest.mark.requirement("INSTALL-10")
def test_a_drifted_fixture_is_refused(tmp_path: Path) -> None:
    """Canonical fixtures are immutable; a changed one must not be used.

    The corruption happens on a disposable COPY of the whole set, passed to
    the real verifier through its own fixture_dir parameter. Nothing here
    reassigns module globals: swapping them would be monkeypatching, and it
    would also mean this test never exercises the production path.
    """
    fake = disposable_fixture_dir(tmp_path / "fixtures")
    assert verified(EXECUTABLE, fake), "the copy must verify before it is broken"
    (fake / EXECUTABLE).write_bytes(b"tampered")
    with pytest.raises(AssertionError, match="manifest checksum"):
        verified(EXECUTABLE, fake)


def test_an_unsupported_manifest_version_is_refused(tmp_path: Path) -> None:
    """Fail closed on a manifest shape this module does not understand.

    A checksum compared against a field that may have moved is not a
    verification, so the version gate runs before any hash does.
    """
    fake = disposable_fixture_dir(tmp_path / "fixtures")
    manifest = json.loads((fake / MANIFEST_NAME).read_text())
    manifest["version"] = SUPPORTED_MANIFEST_VERSION + 1
    (fake / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(AssertionError, match="manifest version"):
        verified(EXECUTABLE, fake)


@pytest.mark.requirement("INSTALL-10")
def test_the_disposable_copy_refuses_a_drifted_source(tmp_path: Path) -> None:
    """Provenance is established BEFORE the set is copied, not after.

    A caller that takes the whole directory names no fixture, so a per-file
    check at the call site cannot cover the ones it does not mention. The
    drift here is planted in DYLIB, which no assertion below ever names: if
    the copy were made first, or if only the fixture a test happens to use
    were checked, this would pass while handing the suite evidence whose
    provenance was never established.
    """
    source = disposable_fixture_dir(tmp_path / "source")
    (source / DYLIB).write_bytes(b"tampered")
    with pytest.raises(AssertionError, match="manifest checksum"):
        disposable_fixture_dir(tmp_path / "copy", fixture_dir=source)
    assert not (tmp_path / "copy").exists(), (
        "a refused source must leave no half-hydrated copy behind for a later "
        "test to pick up as if it had been verified"
    )


def test_the_fixtures_load_on_the_oldest_macos_the_app_supports() -> None:
    """LC_BUILD_VERSION minos must not exceed the app's declared minimum.

    An invariant, not a recorded number. The first capture of these fixtures
    defaulted to the host SDK and encoded minos 26.0, which dyld on the
    macos-14 packaging runner could not have loaded, so "these are loadable
    Mach-O files" was false in exactly the environment they stand for. Tying
    the assertion to tauri.conf.json means raising or lowering the app's floor
    cannot silently invalidate the fixtures.
    """
    declared = json.loads(
        (REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json").read_text()
    )["bundle"]["macOS"]["minimumSystemVersion"]
    ceiling = tuple(int(part) for part in str(declared).split("."))
    for name, entry in load_manifest()["files"].items():
        minos = tuple(int(part) for part in entry["minos"].split("."))
        assert minos <= ceiling, (
            f"{name} requires macOS {entry['minos']} but the app declares "
            f"{declared}. Rebuild it with -mmacosx-version-min={declared}; a "
            "fixture the shipped app's own floor cannot load is not evidence."
        )


def test_the_manifest_filetypes_match_the_binaries(tmp_path: Path) -> None:
    """The recorded filetype is a claim, so it is checked rather than trusted."""
    if shutil.which("otool") is None:
        pytest.skip("otool is macOS-only")
    for name, entry in load_manifest()["files"].items():
        assert macho_filetype(verified(name)) == entry["filetype"], name


# ----- otool: the scan itself, macOS only ---------------------------------
@pytest.mark.requirement("INSTALL-10")
@pytest.mark.skipif(shutil.which("otool") is None, reason="otool is macOS-only")
def test_every_macho_in_the_payload_is_scanned(tmp_path: Path) -> None:
    """All four, including the interpreter, and the bar still passes."""
    root = _macho_payload(tmp_path)
    count, violations = scan_linked_libraries(root)
    assert count == 4, f"expected all four Mach-O files scanned, got {count}"
    assert violations == [], violations


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.skipif(shutil.which("otool") is None, reason="otool is macOS-only")
def test_a_dylibs_install_name_is_not_read_as_a_dependency(tmp_path: Path) -> None:
    """Run against real otool output, not a transcribed string.

    The dylib fixture's install name is @rpath/..., which the bar allows, so
    a misclassification would not surface as a violation here. The assertion
    is therefore on the PARSE: the id line must be absent from the
    dependencies, and present when the filetype is answered wrongly.
    """
    root = _macho_payload(tmp_path)
    named_so = root / "pylib/_pydantic_core.cpython-311-darwin.so"
    output = subprocess.run(
        ["otool", "-L", str(named_so)], capture_output=True, text=True, check=True
    ).stdout
    as_dylib = parse_otool(output, is_dylib=True)
    as_bundle = parse_otool(output, is_dylib=False)
    assert len(as_bundle) == len(as_dylib) + 1, (
        "the fixture must carry an id line, or this proves nothing"
    )
    assert as_bundle[0].endswith("opendj-probe-dylib-named.so"), as_bundle
    assert not any(d.endswith("opendj-probe-dylib-named.so") for d in as_dylib)
    assert link_violations("pylib/x.so", as_dylib) == []


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.skipif(shutil.which("otool") is None, reason="otool is macOS-only")
def test_an_executable_keeps_its_first_dependency(tmp_path: Path) -> None:
    """An executable has no id line, so dropping one loses a real link.

    This is how a scan that finally inspects the interpreter could still be
    blind: widening the candidate set without fixing the filetype question
    silently discards the interpreter's first dependency.
    """
    root = _macho_payload(tmp_path)
    output = subprocess.run(
        ["otool", "-L", str(root / "runtime/bin/python3.11")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    kept = parse_otool(output, is_dylib=False)
    mistaken = parse_otool(output, is_dylib=True)
    assert kept, "the fixture must link something, or this proves nothing"
    assert len(mistaken) == len(kept) - 1, (
        "the control must show what the wrong answer costs"
    )
