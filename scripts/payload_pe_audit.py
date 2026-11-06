"""Windows link audit for the engine payload: PE import tables, stdlib only.

Windows counterpart of the otool allow-list in scripts/build_engine_payload.py.
No dumpbin, llvm-objdump or pefile is assumed: the import directory and the
delay-import directory are read straight from the PE headers, so the audit runs
on any build host with Python.

Requirements:
- ☑️✅ list the DLL names a PE file imports (static and delay-load)
  [if] a PE file imports KERNEL32.dll [then] it is listed
  [if] a delay-loaded DLL exists only in the delay table [then] it is still listed
  [if] the file is not a PE image [then] PeAuditError, never an empty list
- ☑️✅ classify each import as system, payload-local, or violation
  [if] an import is on the system allow-list [then] it is not a violation
  [if] an import is neither system nor shipped in the payload [then] it is a violation
  [if] the scan finds zero PE files [then] PeAuditError (zero is not a pass)
"""

from __future__ import annotations

import fnmatch
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PE_SUFFIXES: tuple[str, ...] = (".exe", ".dll", ".pyd")
MACHINE_AMD64: int = 0x8664
DIRECTORY_IMPORT: int = 1
DIRECTORY_DELAY_IMPORT: int = 13
PE32_MAGIC: int = 0x10B
PE32_PLUS_MAGIC: int = 0x20B
IMPORT_DESCRIPTOR_SIZE: int = 20
DELAY_DESCRIPTOR_SIZE: int = 32

#: Windows system DLLs present on every supported Windows install. Matched on
#: the lower-cased name, ``.dll`` stripped; a trailing ``*`` is a prefix match.
SYSTEM_DLLS: frozenset[str] = frozenset(
    name.lower()
    for name in (
        "kernel32",
        "kernelbase",
        "user32",
        "gdi32",
        "ws2_32",
        "wsock32",
        "advapi32",
        "bcrypt",
        "ncrypt",
        "ole32",
        "oleaut32",
        "winmm",
        "ntdll",
        "shell32",
        "shlwapi",
        "shcore",
        "crypt32",
        "secur32",
        "iphlpapi",
        "msvcrt",
        "ucrtbase",
        "version",
        "rpcrt4",
        "userenv",
        "winhttp",
        "mswsock",
        "dnsapi",
        "setupapi",
        "cfgmgr32",
        "comctl32",
        "comdlg32",
        "imm32",
        "uxtheme",
        "dwmapi",
        "mmdevapi",
        "avrt",
        "psapi",
        "dbghelp",
        "cryptbase",
        "normaliz",
        "powrprof",
        "propsys",
        "sspicli",
        "wintrust",
        "netapi32",
        "wldap32",
        "bcryptprimitives",
        "ksuser",
        "d3d11",
        "dxgi",
        "mfplat",
        "mf",
        "mfreadwrite",
        "api-ms-win-*",
        "ext-ms-win-*",
        # Further Windows OS libraries imported by pywin32, psutil, torch and cpal.
        "combase",
        "pdh",
        "winspool",
        "msimg32",
        "wevtapi",
        "sfc",
        "query",
        "msi",
        "loadperf",
        "ktmw32",
        "imagehlp",
        "credui",
        "wtsapi32",
        "wininet",
        "rasapi32",
        "odbc32",
        "ntdsapi",
        "mpr",
        "lz32",
        "dsound",
        "cabinet",
        "activeds",
        "aclui",
        "urlmon",
        "oleacc",
    )
)

#: The Visual C++ runtime. Not part of Windows itself: a bare machine has it
#: only if the VC++ redistributable is installed or the DLL ships beside the
#: importer. Accepted (the allow-list says so) but reported separately.
VC_RUNTIME_DLLS: frozenset[str] = frozenset(
    {
        "vcruntime140",
        "vcruntime140_1",
        "msvcp140",
        "msvcp140_1",
        "msvcp140_2",
        "msvcp140_atomic_wait",
        "msvcp140_codecvt_ids",
        "concrt140",
        "vcomp140",
    }
)


class PeAuditError(RuntimeError):
    """The audit could not measure, or found a violation. Never a silent pass."""


@dataclass(frozen=True)
class PeImports:
    relative: str
    machine: int
    imports: tuple[str, ...]


def _u16(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<H", data, offset)[0])


def _u32(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<I", data, offset)[0])


def _cstring(data: bytes, offset: int) -> str:
    end = data.index(b"\0", offset)
    return data[offset:end].decode("ascii")


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int]], path: Path) -> int:
    for virtual_address, extent, raw_pointer in sections:
        if virtual_address <= rva < virtual_address + extent:
            return rva - virtual_address + raw_pointer
    raise PeAuditError(f"{path}: RVA {rva:#x} is inside no section; truncated or corrupt PE")


def _names_in_directory(
    data: bytes, table_offset: int, stride: int, name_field: int, sections: list[tuple[int, int, int]], path: Path
) -> list[str]:
    names: list[str] = []
    cursor = table_offset
    while cursor + stride <= len(data):
        descriptor = data[cursor : cursor + stride]
        if descriptor == b"\0" * stride:
            return names
        name_rva = _u32(descriptor, name_field)
        names.append(_cstring(data, _rva_to_offset(name_rva, sections, path)))
        cursor += stride
    raise PeAuditError(f"{path}: import table has no terminating null descriptor")


def read_pe_imports(path: Path, relative: str | None = None) -> PeImports:
    """Every DLL ``path`` imports, static and delay-load, in table order."""
    data = path.read_bytes()
    label = relative or path.name
    if data[:2] != b"MZ" or len(data) < 0x40:
        raise PeAuditError(f"{path} is not a PE image (no MZ header)")
    pe_offset = _u32(data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PeAuditError(f"{path} is not a PE image (no PE signature at {pe_offset:#x})")
    machine = _u16(data, pe_offset + 4)
    section_count = _u16(data, pe_offset + 6)
    optional_size = _u16(data, pe_offset + 20)
    optional = pe_offset + 24
    magic = _u16(data, optional)
    if magic == PE32_MAGIC:
        directories = optional + 96
    elif magic == PE32_PLUS_MAGIC:
        directories = optional + 112
    else:
        raise PeAuditError(f"{path}: unknown optional-header magic {magic:#x}")
    section_table = optional + optional_size
    sections: list[tuple[int, int, int]] = []
    for index in range(section_count):
        base = section_table + 40 * index
        virtual_size = _u32(data, base + 8)
        raw_size = _u32(data, base + 16)
        sections.append((_u32(data, base + 12), max(virtual_size, raw_size), _u32(data, base + 20)))
    names: list[str] = []
    for index, stride, name_field in (
        (DIRECTORY_IMPORT, IMPORT_DESCRIPTOR_SIZE, 12),
        (DIRECTORY_DELAY_IMPORT, DELAY_DESCRIPTOR_SIZE, 4),
    ):
        rva = _u32(data, directories + 8 * index)
        if rva == 0:
            continue
        offset = _rva_to_offset(rva, sections, path)
        names.extend(_names_in_directory(data, offset, stride, name_field, sections, path))
    return PeImports(relative=label, machine=machine, imports=tuple(names))


def _stem(dll_name: str) -> str:
    lowered = dll_name.lower()
    for suffix in (".dll", ".drv"):
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def is_system_dll(dll_name: str) -> bool:
    stem = _stem(dll_name)
    for allowed in SYSTEM_DLLS:
        if allowed.endswith("*") and stem.startswith(allowed[:-1]):
            return True
        if stem == allowed:
            return True
    return False


def pe_candidates(payload_dir: Path) -> list[Path]:
    return sorted(path for path in payload_dir.rglob("*") if path.is_file() and path.suffix.lower() in PE_SUFFIXES)


def _vc_runtime_ships_beside(payload_dir: Path, path: Path, relative: str, dll: str) -> bool:
    """Windows resolves a DLL from the loading process's own directory first:
    bin/*.exe sees only bin/, everything else is loaded by runtime/python.exe
    and also sees runtime/."""
    search = [path.parent] if relative.startswith("bin/") else [path.parent, payload_dir / "runtime"]
    # PE import names carry whatever case the linker wrote (VCRUNTIME140.dll)
    # and the loader matches case-insensitively, but the audit also runs on
    # case-sensitive filesystems, so compare lowercased names.
    wanted = dll.lower()
    return any(entry.name.lower() == wanted for folder in search if folder.is_dir() for entry in folder.iterdir())


def _classify_import(
    payload_dir: Path,
    path: Path,
    relative: str,
    dll: str,
    shipped: set[str],
    vc_runtime_must_ship: bool,
    vc_runtime: dict[str, list[str]],
) -> str | None:
    """A finding message for one import, or None. Non-strict VC++ gaps go to ``vc_runtime``."""
    if _stem(dll) in VC_RUNTIME_DLLS:
        if _vc_runtime_ships_beside(payload_dir, path, relative, dll):
            return None
        if vc_runtime_must_ship:
            return f"{relative}: imports {dll}, which does not ship beside it or in runtime/"
        vc_runtime.setdefault(dll.lower(), []).append(relative)
        return None
    if is_system_dll(dll) or dll.lower() in shipped:
        return None
    return f"{relative}: imports {dll}, which is neither a system DLL nor in the payload"


def audit_payload(
    payload_dir: Path, accepted: Mapping[str, str] | None = None, *, vc_runtime_must_ship: bool = False
) -> dict[str, object]:
    """Audit every PE file under ``payload_dir``; raise on any violation.

    An import passes when it is a Windows system DLL, a VC++ runtime DLL, or a
    DLL that ships inside this payload (matched by file name anywhere under it:
    wheels bundle their native libraries in ``*.libs`` directories). Anything
    else resolves on this machine and nowhere else.

    ``accepted`` maps a payload-relative glob to the reason a finding on those
    files is tolerated (a lazily loaded optional library, a data stub that is
    never loaded). An entry that matches no finding is a stale claim nobody
    re-checked, so it raises instead of lingering.

    ``vc_runtime_must_ship`` makes a VC++ runtime import with no copy beside its
    importer (bin/ for executables, the importer's directory or runtime/ for
    everything else) a violation instead of a "needs the redistributable" note.
    """
    accepted = dict(accepted or {})
    candidates = pe_candidates(payload_dir)
    if not candidates:
        raise PeAuditError(f"no PE files under {payload_dir}; an audit that scanned nothing proves nothing")
    shipped = {path.name.lower() for path in candidates}
    findings: list[tuple[str, str]] = []
    vc_runtime: dict[str, list[str]] = {}
    for path in candidates:
        relative = path.relative_to(payload_dir).as_posix()
        pe = read_pe_imports(path, relative)
        if pe.machine != MACHINE_AMD64:
            findings.append((relative, f"{relative}: machine {pe.machine:#x} is not x64"))
        for dll in pe.imports:
            message = _classify_import(payload_dir, path, relative, dll, shipped, vc_runtime_must_ship, vc_runtime)
            if message is not None:
                findings.append((relative, message))
    used: dict[str, int] = {}
    violations: list[str] = []
    for relative, message in findings:
        pattern = next((glob for glob in accepted if fnmatch.fnmatchcase(relative, glob)), None)
        if pattern is None:
            violations.append(message)
        else:
            used[pattern] = used.get(pattern, 0) + 1
    stale = sorted(set(accepted) - set(used))
    if stale:
        raise PeAuditError(f"accepted audit exemptions match no finding any more (delete or fix them): {stale}")
    if violations:
        listing = "\n  ".join(violations)
        raise PeAuditError(f"the payload links DLLs that exist only on the build machine:\n  {listing}")
    return {
        "pe_files_scanned": len(candidates),
        "external_link_references": [],
        "accepted_findings": {glob: {"count": used[glob], "reason": accepted[glob]} for glob in sorted(used)},
        # VC++ runtime DLLs no loader search directory carries: the target machine
        # needs the VC++ redistributable for these importers.
        "vc_runtime_needs_redistributable": {
            name: {"importers": len(files), "bin_importers": sorted(f for f in files if f.startswith("bin/"))}
            for name, files in sorted(vc_runtime.items())
        },
    }
