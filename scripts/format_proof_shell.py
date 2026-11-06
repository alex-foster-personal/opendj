"""Prove a shell change is layout only and exactly the pinned shfmt's output (issue #4456, part 7).

`scripts/format_proof.py` hands shell scripts here: tracked `*.sh` and `*.bash` files, and extensionless
files whose first line is a sh, bash, dash, ksh or mksh shebang. shfmt's own parser is the oracle, so no
shell grammar is re-implemented in Python: both sides are parsed with `shfmt --to-json`, every position
is dropped, and the rest of the tree, comments included, must be equal.

    python -m scripts.format_proof_shell install [--dest DIR]   # the pinned release binary, sha256-checked

Requirements (mini-PRD):
  prove_shell  ✔︎
    Each changed shell script parses on both sides, its syntax tree without positions is equal, every
    heredoc body and here-string is byte-identical, `bash -n` (`sh -n` under a POSIX shebang) gives the
    same verdict on both sides, the shebang line is byte-identical, and head is exactly what the pinned
    shfmt makes of base under the ratified flags `-i 4 -ci`.
      [if] a command, word, operator or comment differs once positions are dropped [then ⛔️] exit 1
      [if] a heredoc body or here-string changes by one byte, indentation included [then ⛔️] exit 1
      [if] head is not shfmt -i 4 -ci of base (any hand edit, or another style) [then ⛔️] exit 1
      [if] the shebang line changes [then ⛔️] exit 1
      [if] shfmt is missing or is not the pinned version [then ⛔️] exit 2 UNKNOWN, with the install command
      [if] a side does not parse, or base fails `bash -n` / `sh -n` [then ⛔️] exit 2 UNKNOWN, never a pass
      [if] the `-n` interpreter (bash or sh) is not on PATH, e.g. Windows with shfmt.exe [then ⛔️] exit 2 UNKNOWN
  install  ✔︎
    Downloads shfmt v3.14.1 for this OS and CPU from the mvdan/sh GitHub release and installs it only
    when its sha256 matches the digest pinned here.
      [if] the download's sha256 is not the pinned digest [then ⛔️] exit 1, nothing installed
      [if] this OS or CPU has no pinned digest [then ⛔️] exit 1
  ensure_pinned_shfmt  ✔︎
    With install_missing (pre-push step 3b, issue #4456 part 9), a missing or other-version shfmt is
    installed on demand into ~/.local/bin, sha256-checked, and the lookup retried.
      [if] the download fails (offline, DNS, timeout) [then ⛔️] a loud SKIP line naming `just shfmt-install`,
        the shell proof marked not run; never a silent pass
      [if] the download's sha256 is not the pinned digest [then ⛔️] exit 2 UNKNOWN, nothing installed
      [if] install_missing is off (CI, `just fmt-proof`) [then ⛔️] exit 2 UNKNOWN, as before

What could satisfy this without satisfying its intent: equality to shfmt's output alone trusts the
printer, so the position-free tree is compared too; a tree compare alone would pass a reindented heredoc
under `<<-`, where leading tabs are data, so heredoc and here-string bytes are compared as well.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from apps.shared.posix_shell import resolve_posix_bash


class CFG:
    SHFMT_VERSION: str = "3.14.1"
    # The ratified style ((private path), decision 3). Flags make shfmt ignore EditorConfig.
    SHFMT_FLAGS: tuple[str, ...] = ("-i", "4", "-ci")
    SUFFIXES: tuple[str, ...] = (".sh", ".bash")
    SHEBANG_RE: re.Pattern[bytes] = re.compile(
        rb"#!\s*(/usr/bin/env\s+(-S\s+)?)?(/usr/local/bin/|/usr/bin/|/bin/)?(ba|da|k|mk)?sh\b"
    )
    POSIX_SHEBANG_RE: re.Pattern[bytes] = re.compile(rb"#!\s*(/usr/bin/env\s+)?(/usr/bin/|/bin/)?(da)?sh\b")
    POSITION_KEYS: frozenset[str] = frozenset({"Offset", "Line", "Col"})
    RELEASE_URL: str = "https://github.com/mvdan/sh/releases/download/v{version}/shfmt_v{version}_{target}"
    # sha256 of each release asset, from the GitHub release v3.14.1 asset digests (Thu 5 Nov 2026).
    SHA256: ClassVar[dict[str, str]] = {
        "linux_amd64": "76e77641faa025814b77f153b29796b8e6fa2fca03e0c76a691608b86c7ea7bf",
        "linux_arm64": "5f2db09dae91fca848f7adbdd014632e921a383863a2ad7e0450ad3aba0c6489",
        "darwin_amd64": "d33eee0da0f92835b3562e9767a05cee7e4eaeef47daa03bfd09da17b4b590a6",
        "darwin_arm64": "b7c872db63553ccffc7253aba3ed7d4885a27d83f1ba567b1138c6315a5847e5",
        "windows_amd64.exe": "13629ce28442ca80b6b5a819f7574ab39e1c28c6e26734ca816c9714e04851df",
    }
    INSTALL_HINT: str = "python -m scripts.format_proof_shell install (or just shfmt-install)"
    INSTALL_DIR: Path = Path.home() / ".local" / "bin"
    SKIP_PREFIX: str = "[format-proof] SKIP"
    TIMEOUT_S: int = 60


@dataclass
class Verdict:
    exit_code: int
    files_checked: int = 0
    version: str = ""
    lines: list[str] = field(default_factory=list)
    skipped: bool = False  # the shell proof did not run (no pinned shfmt and none could be downloaded)


BlobReader = Callable[[str, str], bytes]


def is_shell_path(path: str) -> bool:
    """Decidable from the name alone: a shell suffix, or an extensionless name that needs its shebang read."""
    return path.endswith(CFG.SUFFIXES) or "." not in path.rsplit("/", 1)[-1]


def is_shell_source(path: str, head_bytes: bytes) -> bool:
    return path.endswith(CFG.SUFFIXES) or (is_shell_path(path) and bool(CFG.SHEBANG_RE.match(head_bytes)))


# ----- helpers --------------------------------------------------------------


def _unknown(message: str) -> Verdict:
    return Verdict(2, lines=[f"[format-proof] UNKNOWN {message}"])


def _version_of(binary: str) -> str:
    return subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout.strip()


def _installed_binary() -> Path:
    return CFG.INSTALL_DIR / ("shfmt.exe" if platform.system() == "Windows" else "shfmt")


def _shfmt_binary() -> str | Verdict:
    """The pinned shfmt from PATH, or from the install dir when PATH has none or another version."""
    on_path = shutil.which("shfmt")
    installed = _installed_binary()
    candidates = [on_path] if on_path else []
    if installed.is_file() and str(installed) != on_path:
        candidates.append(str(installed))
    for binary in candidates:
        if _version_of(binary) == f"v{CFG.SHFMT_VERSION}":
            return binary
    if on_path is None:
        return _unknown(f"shfmt is not on PATH; install the pinned v{CFG.SHFMT_VERSION} with {CFG.INSTALL_HINT}")
    version = _version_of(on_path)
    return _unknown(f"{on_path} is {version or 'unversioned'}, not the pinned v{CFG.SHFMT_VERSION}; {CFG.INSTALL_HINT}")


def ensure_pinned_shfmt(install_missing: bool) -> str | Verdict:
    """The pinned shfmt; with install_missing, install it on demand, or SKIP loudly when it cannot be downloaded."""
    found = _shfmt_binary()
    if isinstance(found, str) or not install_missing:
        return found
    why = found.lines[0].removeprefix("[format-proof] UNKNOWN ").split(";")[0]
    print(f"[format-proof] {why}: installing the pinned shfmt v{CFG.SHFMT_VERSION} into {CFG.INSTALL_DIR}")
    try:
        installed = install(CFG.INSTALL_DIR)
    # URLError and timeouts are OSError; a connection dropped mid-body is http.client.IncompleteRead.
    except (OSError, http.client.HTTPException, UnsupportedPlatform) as exc:
        return Verdict(
            0,
            skipped=True,
            lines=[
                f"{CFG.SKIP_PREFIX} shell proof NOT RUN: shfmt v{CFG.SHFMT_VERSION} could not be installed ({exc}). "
                f"Run `just shfmt-install` on this host, then `just fmt-ignore-revs-check`, to prove the shell part."
            ],
        )
    if installed != 0:
        return _unknown(f"the downloaded shfmt v{CFG.SHFMT_VERSION} failed its sha256 check; nothing installed")
    return _shfmt_binary()


def _run(command: list[str], data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, input=data, capture_output=True, timeout=CFG.TIMEOUT_S, check=False)


def _syntax_shell(data: bytes) -> str:
    return "sh" if CFG.POSIX_SHEBANG_RE.match(data) else "bash"


def _resolve_syntax_shell(name: str) -> str | None:
    """Absolute path of the `-n` interpreter, or None when this host has none.

    bash goes through resolve_posix_bash so Windows never launches System32's WSL
    launcher. sh has no System32 sh.exe, so PATH which is enough.
    """
    if name == "bash":
        return resolve_posix_bash()
    return shutil.which(name)


def _missing_syntax_shell(*sides: bytes) -> str | None:
    """The first `-n` interpreter a side needs that is not resolvable (Windows with shfmt.exe, no Bash)."""
    return next((shell for shell in map(_syntax_shell, sides) if _resolve_syntax_shell(shell) is None), None)


def _syntax_ok(path: str, data: bytes) -> bool:
    name = _syntax_shell(data)
    interpreter = _resolve_syntax_shell(name)
    if interpreter is None:
        raise RuntimeError(f"no {name} on PATH: check _missing_syntax_shell first")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp, Path(path).name)
        script.write_bytes(data)
        return subprocess.run([interpreter, "-n", str(script)], capture_output=True, check=False).returncode == 0


def _syntax_failure(path: str, before: bytes, after: bytes) -> tuple[int, str] | None:
    """(exit code, line) when the `-n` check cannot run or the sides disagree; None when both pass."""
    missing = _missing_syntax_shell(before, after)
    if missing is not None:
        return 2, f"[format-proof] UNKNOWN {path}: {missing} is not on PATH, so its -n syntax check cannot run"
    if not _syntax_ok(path, before):
        return 2, f"[format-proof] UNKNOWN {path}: the base side fails its shell's -n check"
    if not _syntax_ok(path, after):
        return 1, f"[format-proof] FAIL {path}: the base passes its shell's -n check and head does not"
    return None


def _without_positions(node: object) -> object:
    if isinstance(node, dict):
        return {
            key: _without_positions(value)
            for key, value in node.items()
            if not (isinstance(value, dict) and set(value) == CFG.POSITION_KEYS)
        }
    if isinstance(node, list):
        return [_without_positions(item) for item in node]
    return node


def _redirect_bodies(node: object, source: bytes, out: list[bytes]) -> list[bytes]:
    """Every heredoc body and here-string, as the exact bytes the source holds, in tree order."""
    if isinstance(node, dict):
        if "Op" in node and "OpPos" in node:
            op_at = node["OpPos"]["Offset"]
            body = (
                node.get("Hdoc")
                if node.get("Hdoc")
                else node.get("Word")
                if source[op_at:].startswith(b"<<<")
                else None
            )
            if isinstance(body, dict):
                out.append(source[body["Pos"]["Offset"] : body["End"]["Offset"]])
            elif source[op_at:].startswith(b"<<"):
                out.append(b"")
        for value in node.values():
            _redirect_bodies(value, source, out)
    elif isinstance(node, list):
        for item in node:
            _redirect_bodies(item, source, out)
    return out


def _parse(shfmt: str, path: str, data: bytes) -> object | str:
    proc = _run([shfmt, "--to-json", "--filename", path], data)
    if proc.returncode != 0:
        return proc.stderr.decode("utf-8", "replace").strip()[-300:]
    return json.loads(proc.stdout)


def _first_line(data: bytes) -> bytes:
    return data.split(b"\n", 1)[0]


def _file_failure(shfmt: str, path: str, before: bytes, after: bytes) -> tuple[int, str] | None:
    """(exit code, line) when this file fails or cannot be proven; None when it passes."""
    tree_before, tree_after = _parse(shfmt, path, before), _parse(shfmt, path, after)
    if isinstance(tree_before, str) or isinstance(tree_after, str):
        return (
            2,
            f"[format-proof] UNKNOWN {path}: shfmt cannot parse a side: {tree_before if isinstance(tree_before, str) else tree_after}",
        )
    syntax_failure = _syntax_failure(path, before, after)
    if syntax_failure is not None:
        return syntax_failure
    if before.startswith(b"#!") and _first_line(before) != _first_line(after):
        return 1, f"[format-proof] FAIL {path}: the shebang line changed"
    if _without_positions(tree_before) != _without_positions(tree_after):
        return 1, f"[format-proof] FAIL {path}: the syntax tree differs once positions are dropped"
    if _redirect_bodies(tree_before, before, []) != _redirect_bodies(tree_after, after, []):
        return 1, f"[format-proof] FAIL {path}: a heredoc body or here-string changed"
    formatted = _run([shfmt, *CFG.SHFMT_FLAGS, "--filename", path], before)
    if formatted.returncode != 0 or formatted.stdout != after:
        return (
            1,
            f"[format-proof] FAIL {path}: not shfmt@{CFG.SHFMT_VERSION} {' '.join(CFG.SHFMT_FLAGS)}'s output of the base file",
        )
    return None


# ----- proof ----------------------------------------------------------------


def prove_shell(
    read_blob: BlobReader, base: str, head: str, paths: list[str], install_missing: bool = False
) -> Verdict:
    """Each path must be layout-only and shfmt's exact output of its base; read_blob(rev, path) gives bytes."""
    shfmt = ensure_pinned_shfmt(install_missing)
    if isinstance(shfmt, Verdict):
        return shfmt
    verdict = Verdict(0, files_checked=len(paths), version=CFG.SHFMT_VERSION)
    for path in paths:
        try:
            before, after = read_blob(base, path), read_blob(head, path)
        except subprocess.CalledProcessError as exc:
            return _unknown(f"{path}: git show exited {exc.returncode}, this clone cannot read a side")
        failure = _file_failure(shfmt, path, before, after)
        if failure is None:
            continue
        code, line = failure
        if code == 2:
            return Verdict(2, lines=[line])
        verdict.exit_code = 1
        verdict.lines.append(line)
    return verdict


# ----- install --------------------------------------------------------------


class UnsupportedPlatform(RuntimeError):
    """This OS and CPU have no pinned shfmt digest."""


def _release_target() -> str:
    system = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(platform.system(), "")
    cpu = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "")
    target = f"{system}_{cpu}" + (".exe" if system == "windows" else "")
    if target not in CFG.SHA256:
        raise UnsupportedPlatform(f"no pinned shfmt digest for {platform.system()} {platform.machine()}")
    return target


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=CFG.TIMEOUT_S) as response:
        return response.read()


def install(dest: Path) -> int:
    """Install the pinned release binary into dest; 1 on a digest mismatch. Raises OSError when offline."""
    target = _release_target()
    url = CFG.RELEASE_URL.format(version=CFG.SHFMT_VERSION, target=target)
    payload = _download(url)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != CFG.SHA256[target]:
        print(f"[ERROR] {url} has sha256 {digest}, not the pinned {CFG.SHA256[target]}; nothing installed")
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    binary = dest / ("shfmt.exe" if target.endswith(".exe") else "shfmt")
    staged = dest / f".{binary.name}.{os.getpid()}.part"
    staged.write_bytes(payload)
    staged.chmod(0o700)  # owner-only: it is a per-user tool in ~/.local/bin
    staged.replace(binary)  # atomic: an interrupted install never leaves a half-written shfmt
    print(f"[OK] shfmt v{CFG.SHFMT_VERSION} ({target}, sha256 {digest}) installed at {binary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install", help=f"install the pinned shfmt v{CFG.SHFMT_VERSION}")
    p_install.add_argument("--dest", type=Path, default=Path.home() / ".local" / "bin")
    args = parser.parse_args(argv)
    if args.command == "install":
        try:
            return install(args.dest)
        except UnsupportedPlatform as exc:
            raise SystemExit(f"[ERROR] {exc}") from exc
    raise SystemExit(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
