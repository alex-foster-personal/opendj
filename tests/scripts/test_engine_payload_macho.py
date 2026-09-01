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

import shutil
import struct
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    describe_macho,
    is_dylib_description,
    link_violations,
    macho_candidates,
    parse_otool,
    scan_linked_libraries,
)

MACHO_MAGIC_64: int = 0xFEEDFACF
CPU_TYPE_ARM64: int = 0x0100000C
MH_EXECUTE: int = 0x2
MH_DYLIB: int = 0x6
MH_BUNDLE: int = 0x8


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


def _macho_header(filetype: int) -> bytes:
    """magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, pad.

    file(1) reads the filetype straight out of these 32 bytes, so discovery
    can be exercised with no real binary and no macOS host. Anything that
    shells out to otool needs a REAL Mach-O and stays darwin-gated below.
    """
    return struct.pack(
        "<IiiIIII", MACHO_MAGIC_64, CPU_TYPE_ARM64, 0, filetype, 0, 0, 0
    ) + b"\0" * 4


def _macho_payload(tmp_path: Path, *, real: bool) -> Path:
    """A payload carrying the two shapes the old predicate could not tell apart.

    ``real`` copies /bin/ls, which otool can actually read; otherwise a
    constructed header, which file(1) can identify anywhere. copyfile rather
    than copy2 because /bin/ls carries SIP-restricted st_flags that copy2
    tries to reproduce and cannot.
    """
    root = tmp_path / "payload"
    (root / "runtime/bin").mkdir(parents=True)
    (root / "bin").mkdir()
    interpreter = root / "runtime/bin/python3.11"
    if real:
        shutil.copyfile("/bin/ls", interpreter)
    else:
        interpreter.write_bytes(_macho_header(MH_EXECUTE))
    interpreter.chmod(0o755)
    (root / "runtime/bin/python3").symlink_to("python3.11")
    launcher = root / "bin/opendj-engine"
    launcher.write_text("#!/bin/sh\nexec ./runtime/bin/python3 -m x\n", encoding="utf-8")
    launcher.chmod(0o755)
    (root / "notes.txt").write_text("not a binary\n", encoding="utf-8")
    return root


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.skipif(shutil.which("otool") is None, reason="otool is macOS-only")
def test_an_extensionless_macho_is_scanned(tmp_path: Path) -> None:
    """The regression, stated as the presence of the good thing."""
    root = _macho_payload(tmp_path, real=True)
    count, violations = scan_linked_libraries(root)
    assert count == 1, (
        "the bar must inspect the extensionless interpreter; an extension-only "
        f"predicate scans nothing here. Scanned {count}."
    )
    assert violations == []


@pytest.mark.requirement("INSTALL-10")
def test_the_old_predicate_scanned_nothing_here(tmp_path: Path) -> None:
    """The control: the replaced predicate, run on the same fixture.

    Without it, "the scan found 1 file" says nothing about what changed.
    """
    root = _macho_payload(tmp_path, real=False)
    old = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in (".so", ".dylib")]
    assert old == [], f"the fixture no longer reproduces the bug: {old}"
    assert (root / "runtime/bin/python3.11") in macho_candidates(root)


@pytest.mark.requirement("INSTALL-10")
def test_a_symlink_to_a_scanned_binary_is_not_scanned_twice(tmp_path: Path) -> None:
    """runtime/bin/python3 points at the versioned binary already in the set."""
    root = _macho_payload(tmp_path, real=False)
    assert (root / "runtime/bin/python3") not in macho_candidates(root)


@pytest.mark.requirement("INSTALL-10")
def test_a_shell_script_is_not_a_macho(tmp_path: Path) -> None:
    """bin/opendj-engine has the exec bit, so only file(1) can reject it."""
    root = _macho_payload(tmp_path, real=False)
    assert describe_macho(root / "bin/opendj-engine") is None
    assert describe_macho(root / "notes.txt") is None
    assert describe_macho(root / "runtime/bin/python3.11") is not None


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (SHARED_LIBRARY_DESCRIPTION, True),
        (FAT_LIBRARY_DESCRIPTION, True),
        (BUNDLE_DESCRIPTION, False),
        (EXECUTABLE_DESCRIPTION, False),
        (None, False),
    ],
)
def test_a_dylib_named_so_is_classified_by_filetype(
    description: str | None, expected: bool
) -> None:
    """Filetype, not suffix. A .so can be either, and four shipped ones are dylibs."""
    assert is_dylib_description(description) is expected


@pytest.mark.requirement("INSTALL-10")
def test_a_dylib_install_name_is_dropped_however_the_file_is_named() -> None:
    """protobuf's `.so` is a dylib whose id is a bazel-out path.

    Read as a bundle, that id counts as a dependency and fails the bar for a
    library nothing loads by that name.
    """
    output = (
        "/p/pylib/google/_upb/_message.abi3.so:\n"
        "\tbazel-out/osx-arm64-opt/bin/python/lib_message_binary.so"
        " (compatibility version 0.0.0)\n"
        "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    )
    as_bundle = parse_otool(output, is_dylib=is_dylib_description(BUNDLE_DESCRIPTION))
    assert link_violations("pylib/x.so", as_bundle), (
        "the fixture must reproduce the spurious violation, or the next "
        "assertion proves nothing"
    )
    as_dylib = parse_otool(
        output, is_dylib=is_dylib_description(SHARED_LIBRARY_DESCRIPTION)
    )
    assert link_violations("pylib/x.so", as_dylib) == []


@pytest.mark.requirement("INSTALL-10")
def test_an_executable_keeps_its_first_dependency() -> None:
    """An executable has no id line, so dropping one loses a real link.

    This is how a scan that finally inspects the interpreter could still be
    blind: widening the candidate set without fixing the filetype question
    would silently discard the interpreter's first dependency.
    """
    kept = parse_otool(
        EXECUTABLE_OUTPUT, is_dylib=is_dylib_description(EXECUTABLE_DESCRIPTION)
    )
    assert kept[0].endswith("/CoreFoundation")
    assert len(kept) == 3
    mistaken = parse_otool(
        EXECUTABLE_OUTPUT, is_dylib=is_dylib_description(SHARED_LIBRARY_DESCRIPTION)
    )
    assert len(mistaken) == 2, "the control must show what the wrong answer costs"
