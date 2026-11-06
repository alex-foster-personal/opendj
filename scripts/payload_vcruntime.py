"""Stage the Visual C++ runtime DLLs beside their importers in the Windows payload.

The 26 native extensions, torch and odj-audio.exe import msvcp140 / vcruntime140 /
vcomp140. A clean Windows machine has them only if the VC++ redistributable was
installed, so the payload ships its own copy, taken from the Visual Studio
redistributable tree (VC/Redist/MSVC/<ver>/x64/Microsoft.VC*.CRT and ...OpenMP),
which Microsoft's license lets us redistribute. Never from C:/Windows/System32.

Requirements:
- ☑️ find the newest redistributable x64 CRT and OpenMP directories
  [if] --vc-redist-dir names a missing directory [then] PayloadBuildError naming it
  [if] no version directory holds x64/Microsoft.VC*.CRT [then] PayloadBuildError naming the search
  [if] vswhere is absent and no explicit directory is given [then] PayloadBuildError, never a skip
- ☑️ copy their DLLs into runtime/ (python.exe and pyds) and bin/ (odj-audio.exe)
  [if] the CRT dir lacks vcruntime140.dll or msvcp140.dll [then] PayloadBuildError
  [if] a staged DLL differs from its source bytes [then] broken (sha256 recorded)
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from scripts.build_engine_payload import PayloadBuildError

#: Directories (payload-relative) that receive the DLLs: runtime/ is the loader
#: directory of python.exe (and so of every pyd it loads); bin/ holds odj-audio.exe.
VCRUNTIME_DESTINATIONS: tuple[str, ...] = ("runtime", "bin")
REDIST_FLAVOURS: tuple[str, ...] = ("Microsoft.VC*.CRT", "Microsoft.VC*.OpenMP")
REQUIRED_DLLS: tuple[str, ...] = ("vcruntime140.dll", "msvcp140.dll")
VERSION_DIR = re.compile(r"^\d+(\.\d+)+$")
REDIST_SUBPATH: str = "VC/Redist/MSVC"


def default_vswhere() -> Path:
    return Path(os.environ["PROGRAMFILES(X86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"


def locate_redist_root(explicit: Path | None, vswhere: Path | None = None) -> Path:
    """The ``VC/Redist/MSVC`` directory: explicit flag, else vswhere's newest install."""
    if explicit is not None:
        if not explicit.is_dir():
            raise PayloadBuildError(f"--vc-redist-dir {explicit} is not a directory")
        return explicit
    vswhere = vswhere or default_vswhere()
    if not vswhere.is_file():
        raise PayloadBuildError(
            f"cannot locate the VC++ redistributable: {vswhere} does not exist and no --vc-redist-dir was given"
        )
    result = subprocess.run(
        [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    install = result.stdout.strip()
    if result.returncode != 0 or not install:
        raise PayloadBuildError(
            f"{vswhere} found no Visual Studio install (exit {result.returncode}, stdout {install!r}, "
            f"stderr {result.stderr.strip()!r}); pass --vc-redist-dir"
        )
    root = Path(install) / REDIST_SUBPATH
    if not root.is_dir():
        raise PayloadBuildError(
            f"Visual Studio at {install} has no {REDIST_SUBPATH} (install the VC++ redistributable component)"
        )
    return root


def _version_key(directory: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in directory.name.split("."))


def find_redist_dirs(root: Path) -> dict[str, Path]:
    """Newest version's x64 CRT and OpenMP directories under ``root``."""
    versions = sorted(
        (d for d in root.iterdir() if d.is_dir() and VERSION_DIR.match(d.name)), key=_version_key, reverse=True
    )
    searched = [str(root / "<version>" / "x64" / flavour) for flavour in REDIST_FLAVOURS]
    for version in versions:
        found = {flavour: sorted((version / "x64").glob(flavour)) for flavour in REDIST_FLAVOURS}
        if all(found.values()):
            return {flavour: matches[-1] for flavour, matches in found.items()}
    raise PayloadBuildError(f"no VC++ redistributable x64 CRT and OpenMP directories; searched {searched} under {root}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_vcruntime(
    payload_dir: Path, vc_redist_dir: Path | None = None, vswhere: Path | None = None
) -> dict[str, object]:
    """Copy the redistributable CRT + OpenMP DLLs into runtime/ and bin/; return the manifest block."""
    root = locate_redist_root(vc_redist_dir, vswhere)
    sources = find_redist_dirs(root)
    dlls = sorted(dll for directory in sources.values() for dll in directory.glob("*.dll"))
    names = {dll.name.lower() for dll in dlls}
    missing = [name for name in REQUIRED_DLLS if name not in names]
    if missing:
        raise PayloadBuildError(f"{sorted(sources.values())} lack {missing}; not a usable VC++ redistributable")
    for relative in VCRUNTIME_DESTINATIONS:
        (payload_dir / relative).mkdir(parents=True, exist_ok=True)
        for dll in dlls:
            shutil.copy2(dll, payload_dir / relative / dll.name)
    return {
        "source": {flavour: str(directory) for flavour, directory in sources.items()},
        "destinations": list(VCRUNTIME_DESTINATIONS),
        "dlls": {dll.name: _sha256(dll) for dll in dlls},
    }
