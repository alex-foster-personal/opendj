#!/usr/bin/env python3
"""bifrost2 remote shell layer: POSIX command construction and listing parsing.

SHELL: bifrost2's SSH shell is MSYS2/MinGW64 bash (Git for Windows), NOT Windows
cmd. Two scripts asserted cmd and acted on it, and both broke silently for
months: bash eats backslashes as escapes, so ``D:\\asset-store`` arrives as
``D:asset-store``, and ``dir /b /ad`` reaches GNU coreutils' ``dir``, which
reads ``/b`` as a path and exits 2. Windows .exe tools such as ``fsutil`` are
still callable, because bash execs them directly. Verify rather than trust:

    ssh bifrost2 'echo $0; uname -a'   -> /usr/bin/bash, MINGW64_NT

FUNCTIONAL CORE / IMPERATIVE SHELL. Everything above the ``io`` divider is
PURE: strings in, strings or dicts out, no subprocess and no network. That
split is the point of this module, not a style preference. AGENTS.md forbids
mocks, stubs and monkeypatching in tests, and permits "captured real protocol
payloads" only when they are "consumed through production paths". Pure command
builders and pure parsers can be handed a real bifrost2 payload and called
directly, so the production path IS the tested path and nothing needs stubbing.
The ``io`` section is the only part that cannot run without the host; it is
covered by a live test that reports UNAVAILABLE rather than skipping silently.

No PEP 723 header: this is an imported library module like ``b2listing.py``,
not a standalone entry point, so it runs under the repo interpreter and takes
its floor from ``pyproject.toml`` (>=3.11). A ``requires-python = ">=3.10"``
block here would advertise a floor nothing enforces.

-Claude
"""
from __future__ import annotations

import shlex
import subprocess

#----- pure: constants --------------------------------------------------------

# Printed by the remote existence guard when the target directory is absent:
# in-band on exit 0, so "absent" is never inferred from error text that a merely
# BROKEN command also produces. See find_command.
ABSENT_SENTINEL: str = "__B2_PATH_ABSENT__"


#----- pure: command construction ---------------------------------------------

def quote_remote(remote_path: str) -> str:
    """shlex-quote a remote path for bifrost2's bash, refusing backslashes.

    Fail fast rather than mangle: bash reads a backslash as an escape, so
    ``D:\\asset-store\\music`` arrives as ``D:asset-storemusic`` and every call
    then operates on a path that does not exist.
    """
    if "\\" in remote_path:
        raise ValueError(
            f"backslash in remote path {remote_path!r}: bifrost2 runs bash, "
            "which consumes backslashes as escapes. Use forward slashes."
        )
    return shlex.quote(remote_path)


def find_command(remote_root: str, find_args: str) -> str:
    """A `find` guarded by an explicit existence test.

    `[ -d ... ]` answers in stdout on exit 0 rather than leaving the caller to
    infer absence from stderr. Inference was tried and is wrong: the broken
    `dir /b /ad` printed "No such file or directory" three times, so a stderr
    match calls a BROKEN COMMAND an empty directory. Downstream that reads as
    "nothing is archived", which is the most expensive possible wrong answer.

    The guard is read-only. A `mkdir -p` here would make every read a write.
    """
    quoted = quote_remote(remote_root)
    return (f"if [ -d {quoted} ]; then find {quoted} {find_args}; "
            f"else echo {ABSENT_SENTINEL}; fi")


def mkdir_command(remote_paths: list[str]) -> str:
    """One idempotent `mkdir -p` for a batch of paths.

    `mkdir -p` needs no `if not exist` probe and cannot lose a race between two
    publishers, unlike cmd's test-then-create.
    """
    if not remote_paths:
        raise ValueError("mkdir_command needs at least one path")
    return "mkdir -p " + " ".join(quote_remote(p) for p in remote_paths)


#----- pure: listing parsing --------------------------------------------------

def listing_or_absent(stdout: str) -> str | None:
    """None if the guard reported the path absent, otherwise the listing."""
    return None if stdout.strip() == ABSENT_SENTINEL else stdout


def parse_sizes(listing: str, source: str = "bifrost2") -> dict[str, int]:
    """{relative path: bytes} from `find -printf '%s %P\\n'` output.

    Keyed by the root-relative path rather than the bare filename, so a file
    sitting in the wrong subdirectory reads as missing instead of as present, a
    distinction a flattened index cannot make.

    Raises on a row it cannot parse. A silently dropped row is a file that
    reads as unarchived and gets re-uploaded forever.
    """
    sizes: dict[str, int] = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        size, _, relative_path = line.partition(" ")
        if not relative_path or not size.isdigit():
            raise ValueError(f"unparseable find output from {source}: {line!r}")
        sizes[relative_path] = int(size)
    return sizes


def parse_names(listing: str) -> set[str]:
    """Names from `find -printf '%P\\n'` output, blank rows dropped."""
    return {line.strip() for line in listing.splitlines() if line.strip()}


#----- io ---------------------------------------------------------------------

def run(argv: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output, raising with stderr on failure.

    ``errors="replace"``: scp echoes remote filenames through unchanged and this
    library's names carry non-ASCII, so a strict decode would abort a multi-GB
    transfer over one cosmetic byte.
    """
    done = subprocess.run(argv, text=True, capture_output=True, timeout=timeout,
                          check=False, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(
            f"command failed ({done.returncode}): {' '.join(argv)}\n{done.stderr.strip()}"
        )
    return done


def ssh(host: str, ssh_opts: list[str], remote_cmd: str, timeout: int = 900) -> str:
    """Run a POSIX shell one-liner on bifrost2, return stdout. Raises on any
    non-zero exit: there is no failure here worth ignoring."""
    return run(["ssh", *ssh_opts, host, remote_cmd], timeout=timeout).stdout
