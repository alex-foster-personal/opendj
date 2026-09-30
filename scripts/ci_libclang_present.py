"""Is there a libclang that bindgen can load here? Answer the way clang-sys would.

apps/audio-engine depends on signalsmith-stretch, whose build script runs
bindgen 0.70 with its default `runtime` feature: clang-sys 1.x finds ONE libclang
at build time and dlopens it, and when that fails the build panics with
"Unable to find libclang" (PR #4361 on agentbox, Wed 30 Sep 2026). Any LLVM major
will do, so this is a capability probe rather than a package check: LLVM 18 on
agentbox and LLVM 21 on nucbox-wsl both satisfy it.

It mirrors clang-sys 1.9.1's Linux search (build/common.rs and build/dynamic.rs,
`find(runtime = true)`), in its order:
  1. LIBCLANG_PATH, when set, is the ONLY place searched (a file or a directory).
  2. `llvm-config --prefix` (LLVM_CONFIG_PATH overrides the command): its bin/,
     lib/ and lib64/.
  3. every LD_LIBRARY_PATH directory, then every LIBRARY_PATH directory.
  4. the directories matching DIRECTORIES_LINUX, which include /usr/lib*/*/*,
     so /usr/lib/llvm-<major>/lib needs no LIBCLANG_PATH.
Each directory is matched against LIBRARY_PATTERNS (libclang-cpp is skipped),
libraries of the wrong ELF class are dropped, and the one with the highest
filename version wins, the earliest found breaking a tie. clang-sys then dlopens
that single library and never falls back to another, so neither does this.
Loading is proved by calling clang_getClangVersion, not by dlopen alone.
One deliberate difference: clang-sys globs the DIRECTORIES_LINUX patterns
case-insensitively; this globs them as the filesystem spells them.

Exit codes:
  0  the library clang-sys would pick loads; prints `libclang <version> loaded from <path> ...`
  1  no candidate, or the picked one does not load (the build would panic)
  2  not Linux: this models clang-sys's Linux search only, so it cannot measure

Callers: the contracts job in .github/workflows/ci.yml (probe, install libclang1
only when this exits nonzero, probe again), and the `libclang1` entry's verify in
ci/runner-toolset.yml, which ships this file to the host inside the verify
(`{repo_b64:...}`), so it must stay standard-library only.

Requirements:
  ✔︎ ✅ 🎯 R1 candidates are found where, and in the order, clang-sys looks.
    [if] LIBCLANG_PATH is set and another directory holds a newer libclang [then ⛔️] that one is picked.
    [if] only /usr/lib/llvm-21/lib holds a libclang [then ⛔️] it is not found.
    [if] libclang-cpp.so.18 sits beside libclang-18.so.1 [then ⛔️] it is a candidate.
  ✔︎ ✅ 🎯 R2 the pick is clang-sys's: highest filename version, earliest on a tie, right ELF class.
    [if] llvm-18 and llvm-21 are both present [then ⛔️] llvm-18 is picked.
    [if] a 32-bit libclang is the newest [then ⛔️] it is picked on a 64-bit host.
    [if] two candidates share a version [then ⛔️] the later-found one is picked.
  ✔︎ ✅ 🎯 R3 present means the picked library loads and answers clang_getClangVersion.
    [if] no candidate exists [then ⛔️] exit 0.
    [if] the picked library fails to load but an older one would [then ⛔️] exit 0.
    [if] a real shared library that is not libclang is picked [then ⛔️] it counts as loaded.
"""

from __future__ import annotations

import ctypes
import fnmatch
import glob
import os
import re
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# clang-sys 1.9.1 build/common.rs DIRECTORIES_LINUX, in its order of preference.
DIRECTORIES_LINUX = (
    "/usr/local/llvm*/lib*",
    "/usr/local/lib*/*/*",
    "/usr/local/lib*/*",
    "/usr/local/lib*",
    "/usr/lib*/*/*",
    "/usr/lib*/*",
    "/usr/lib*",
)
# clang-sys 1.9.1 build/dynamic.rs, Linux with runtime = true, in its order.
LIBRARY_PATTERNS = ("libclang.so", "libclang-*.so", "libclang.so.*", "libclang-*.so.*")
ELF_MAGIC = b"\x7fELF"
ELF_CLASS = {32: 1, 64: 2}
CLANG_VERSION_RE = re.compile(r"clang version (\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class Candidate:
    path: Path
    version: tuple[int, ...]


# ----- search (pure, rooted so tests can build a fake tree) ---------------------


def parse_version(filename: str) -> tuple[int, ...]:
    """clang-sys's parse_version, quirks included: libclang-18.so.1 reads as (18, 0)."""
    if filename.startswith("libclang.so."):
        text = filename[len("libclang.so.") :]
    elif filename.startswith("libclang-"):
        text = filename[9 : len(filename) - 3]
    else:
        return ()
    return tuple(int(part) if part.isdigit() else 0 for part in text.split("."))


def libraries_in(directory: Path) -> list[Path]:
    """Files in `directory` matching LIBRARY_PATTERNS, pattern by pattern, each sorted."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        directory / name
        for pattern in LIBRARY_PATTERNS
        for name in names
        if fnmatch.fnmatchcase(name, pattern) and "-cpp." not in name
    ]


def _split_paths(value: str) -> list[Path]:
    return [Path(part or ".") for part in value.split(os.pathsep)]


def search_directories(
    environ: Mapping[str, str], llvm_prefix: str | None, root: Path = Path("/")
) -> list[Path]:
    """Every libclang candidate path, in clang-sys's search order."""
    if libclang_path := environ.get("LIBCLANG_PATH"):
        given = Path(libclang_path)
        if any(p.name == given.name for p in libraries_in(given.parent)):
            return [given]
        return libraries_in(given)
    directories: list[Path] = []
    if llvm_prefix:
        directories += [Path(llvm_prefix) / sub for sub in ("bin", "lib", "lib64")]
    for variable in ("LD_LIBRARY_PATH", "LIBRARY_PATH"):
        if variable in environ:
            directories += _split_paths(environ[variable])
    for pattern in DIRECTORIES_LINUX:
        rooted = str(root / pattern.lstrip("/"))
        directories += [Path(d) for d in sorted(glob.glob(rooted)) if os.path.isdir(d)]
    return [library for directory in directories for library in libraries_in(directory)]


def elf_class_matches(path: Path, pointer_bits: int) -> bool:
    """clang-sys's validate_library on Linux: an ELF header of this process's class."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(5)
    except OSError:
        return False
    return header[:4] == ELF_MAGIC and header[4:5] == bytes([ELF_CLASS[pointer_bits]])


def pick(paths: list[Path], pointer_bits: int) -> tuple[Candidate | None, int]:
    """clang-sys's choice (highest version, earliest on a tie) and how many were valid."""
    valid = [
        Candidate(p, parse_version(p.name)) for p in paths if elf_class_matches(p, pointer_bits)
    ]
    if not valid:
        return None, 0
    return max(valid, key=lambda c: c.version), len(valid)


# ----- effects ------------------------------------------------------------------


def llvm_config_prefix(environ: Mapping[str, str]) -> str | None:
    """First line of `llvm-config --prefix`, or None when it cannot run (as clang-sys)."""
    command = environ.get("LLVM_CONFIG_PATH", "llvm-config")
    try:
        done = subprocess.run([command, "--prefix"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    lines = done.stdout.splitlines()
    return lines[0] if done.returncode == 0 and lines else None


class _CXString(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("private_flags", ctypes.c_uint)]


def load_clang_version(path: Path) -> str:
    """dlopen `path` and return clang_getClangVersion(); raises OSError/AttributeError."""
    library = ctypes.CDLL(str(path))
    library.clang_getClangVersion.restype = _CXString
    library.clang_getCString.argtypes = [_CXString]
    library.clang_getCString.restype = ctypes.c_char_p
    library.clang_disposeString.argtypes = [_CXString]
    cx_string = library.clang_getClangVersion()
    text = (library.clang_getCString(cx_string) or b"").decode(errors="replace")
    library.clang_disposeString(cx_string)
    return text


# ----- decision -----------------------------------------------------------------


def probe(
    environ: Mapping[str, str],
    llvm_prefix: str | None,
    load: Callable[[Path], str],
    root: Path = Path("/"),
    pointer_bits: int = struct.calcsize("P") * 8,
) -> tuple[int, str]:
    """(exit code, message) for the libclang clang-sys would load on this host."""
    paths = search_directories(environ, llvm_prefix, root)
    chosen, valid = pick(paths, pointer_bits)
    if chosen is None:
        return 1, (
            f"[libclang] MISSING: none of {len(paths)} candidate files is a {pointer_bits}-bit"
            " libclang where clang-sys searches (LIBCLANG_PATH, llvm-config --prefix,"
            " LD_LIBRARY_PATH, LIBRARY_PATH, /usr/local/llvm*/lib*, /usr/lib*/*/*, ...);"
            " bindgen would panic 'Unable to find libclang'"
        )
    try:
        reported = load(chosen.path)
    except (OSError, AttributeError) as error:
        return 1, (
            f"[libclang] MISSING: clang-sys would pick {chosen.path} (of {valid}) and it does"
            f" not load: {error}; clang-sys never falls back to another candidate"
        )
    found = CLANG_VERSION_RE.search(reported)
    if found is None:
        return 1, f"[libclang] MISSING: {chosen.path} loaded but reported no version: {reported!r}"
    return 0, (
        f"libclang {found.group(1)} loaded from {chosen.path}"
        f" (clang-sys's pick of {valid} candidates: {reported})"
    )


def main() -> int:
    if not sys.platform.startswith("linux"):
        print(f"[libclang] UNKNOWN: models clang-sys's Linux search only, not {sys.platform}")
        return 2
    code, message = probe(os.environ, llvm_config_prefix(os.environ), load_clang_version)
    print(message, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
