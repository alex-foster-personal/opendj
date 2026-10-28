"""Mach-O classification for the payload's linked-library bar.

Split out of scripts/build_engine_payload.py, which re-exports every name
here, so the builder stays inside its file-size budget. ``file -b`` answers
whether a file is Mach-O; ``otool -hv`` answers which kind.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _build_error(message: str) -> Exception:
    from scripts.build_engine_payload import PayloadBuildError

    return PayloadBuildError(message)


# `file -b` says "Mach-O 64-bit dynamically linked shared library arm64" for a
# dylib, "Mach-O 64-bit bundle arm64" for a CPython extension module, and
# "Mach-O 64-bit executable arm64" for the interpreter. A universal binary
# repeats a description per slice inline ("Mach-O universal binary with 2
# architectures: [x86_64:Mach-O 64-bit executable x86_64] [...]"), so a
# substring test answers for thin and fat alike. An ar archive reports
# "current ar archive random library" and is excluded, which matters: the
# staged runtime carries real .a files and codesign and otool both refuse them.
MACHO_DESCRIPTION: str = "Mach-O"
# How file(1) opens its error line. It writes this to STDOUT and exits 0, on
# both the BSD and GNU builds, so it is the only signal a classification
# failure gives.
FILE_ERROR_PREFIX: str = "cannot open"
# otool -hv's filetype column, a documented token rather than prose. Only a
# DYLIB carries an id line in otool -L output.
DYLIB_FILETYPE: str = "DYLIB"


def describe_macho(path: Path) -> str | None:
    """``file -b`` for a Mach-O file, or None for anything that is not one.

    Detection is by CONTENT. The predicate this replaced was
    ``path.suffix in (".so", ".dylib")``, which never inspected the one binary
    the payload cannot run without: the bundled CPython at
    runtime/bin/python3.N carries no extension, so this bar had never looked
    at it. The signing paths carried the same defect and are fixed the same
    way, in scripts/lib/macho.sh.
    """
    result = subprocess.run(
        ["file", "-b", str(path)], capture_output=True, text=True, check=False
    )
    description = " ".join(result.stdout.split())
    # A tool that cannot measure must report UNKNOWN, never a verdict. Reading
    # a failed classification as "not Mach-O" would drop the file from the
    # scan and let a release report success over something nothing inspected,
    # which is the same shape as the bug this function exists to fix.
    #
    # The exit code alone will NOT tell you (measured on macOS 15 and matching
    # the BSD and GNU manuals): `file` reports a missing or unreadable path by
    # writing "cannot open ..." to STDOUT and exiting 0. A returncode check
    # here would be a guard that never fires.
    if result.returncode != 0 or not description or description.startswith(FILE_ERROR_PREFIX):
        raise _build_error(
            f"file(1) could not classify {path} (exit {result.returncode}): "
            f"{description or result.stderr.strip() or 'no output'}. The "
            "linked-library bar cannot classify this candidate, and an "
            "unclassified payload file is not shippable."
        )
    return description if MACHO_DESCRIPTION in description else None


def macho_filetype(path: Path) -> str:
    """The Mach-O filetype token from ``otool -hv``: EXECUTE, DYLIB, BUNDLE.

    Whether ``otool -L`` opens with the file's own install name or with a real
    dependency is a question about FILETYPE. Only a DYLIB carries an id line;
    a BUNDLE (every CPython extension module) and an EXECUTABLE (the
    interpreter) do not, and treating one as though it did silently discards
    its FIRST dependency.

    Read from otool rather than from file(1) prose. file(1) answers the
    is-it-Mach-O question portably, but its wording will not carry a filetype:
    macOS says "Mach-O 64-bit executable arm64" where GNU file says "Mach-O
    64-bit arm64 executable, flags:<...>" (observed on ubuntu-latest, Tue 1
    Sep 2026), and GNU file describes a bundle with a phrase that CONTAINS the
    one it uses for a shared library, so a substring test would call every
    bundle a dylib. otool -hv prints a token.

    A universal binary repeats the header per slice; all slices share a
    filetype, and a file whose slices disagree is refused rather than guessed.
    """
    result = subprocess.run(
        ["otool", "-hv", str(path)], capture_output=True, text=True, check=False
    )
    types = {
        line.split()[4]
        for line in result.stdout.splitlines()
        if line.startswith("MH_") and len(line.split()) > 4
    }
    if result.returncode != 0 or len(types) != 1:
        raise _build_error(
            f"otool -hv could not give {path} a single Mach-O filetype "
            f"(exit {result.returncode}, saw {sorted(types) or 'nothing'}). "
            "Its linked libraries cannot be read correctly without one, and a "
            "guessed filetype drops a real dependency from the scan."
        )
    return types.pop()
