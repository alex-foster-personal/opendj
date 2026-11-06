"""PE audit, VC++ redistributable staging, and built-payload checks for the Windows stager.

Cheap tests run everywhere on real bytes (a synthetic PE image, a tiny fake
redist tree). The built-payload test needs a payload produced by
``python -m scripts.build_engine_payload_win``.

- if the PE reader returns an empty list for a file that imports DLLs, the link audit passes vacuously -> broken.
- if an import that is neither system nor shipped passes the audit, the payload runs only on the build host -> broken.
- if an audit that scanned zero PE files reports success, zero is mistaken for clean -> broken.
- if the delay-import directory is ignored, a delay-loaded foreign DLL is never audited -> broken.
- if stage_vcruntime skips silently when no redistributable exists, a clean machine cannot load the payload -> broken.
- if the staged VC++ DLLs are not in both runtime/ and bin/, python or odj-audio.exe cannot find them -> broken.
- if a VC++ import with no shipped copy passes the strict audit, the payload needs the redistributable -> broken.
- if a built payload lacks the launchers, interpreter or odj-audio.exe, the shell cannot boot it -> broken.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from scripts import payload_launchers_win, payload_pe_audit
from scripts.build_engine_payload import MANIFEST_KIND, PayloadBuildError
from scripts.build_engine_payload_win import AUDIO_ENGINE_WIN_RELATIVE, RUNTIME_PYTHON_RELATIVE
from scripts.payload_vcruntime import stage_vcruntime

BUILT_PAYLOAD_ENV: str = "ODJ_WINDOWS_PAYLOAD_DIR"
SECTION_RVA: int = 0x1000
SECTION_FILE_OFFSET: int = 0x200


def synthetic_pe(imports: list[str], machine: int = payload_pe_audit.MACHINE_AMD64, *, delay: bool = False) -> bytes:
    """A minimal PE32+ image whose import or delay-import directory names ``imports``."""
    stride = payload_pe_audit.DELAY_DESCRIPTOR_SIZE if delay else payload_pe_audit.IMPORT_DESCRIPTOR_SIZE
    table_size = stride * (len(imports) + 1)
    names = b""
    entries = b""
    for name in imports:
        name_rva = SECTION_RVA + table_size + len(names)
        if delay:
            entries += struct.pack("<8I", 0, name_rva, 0, 0, 0, 0, 0, 0)
        else:
            entries += struct.pack("<5I", 0, 0, 0, name_rva, 0)
        names += name.encode("ascii") + b"\0"
    section = entries + b"\0" * stride + names
    dos = b"MZ" + b"\0" * 58 + struct.pack("<I", 0x40)
    coff = struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, 240, 0x22)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, payload_pe_audit.PE32_PLUS_MAGIC)
    directory = payload_pe_audit.DIRECTORY_DELAY_IMPORT if delay else payload_pe_audit.DIRECTORY_IMPORT
    struct.pack_into("<II", optional, 112 + 8 * directory, SECTION_RVA, table_size)
    section_header = struct.pack(
        "<8sIIIIIIHHI", b".idata", len(section), SECTION_RVA, len(section), SECTION_FILE_OFFSET, 0, 0, 0, 0, 0
    )
    header = dos + b"PE\0\0" + coff + bytes(optional) + section_header
    return header.ljust(SECTION_FILE_OFFSET, b"\0") + section


def write_pe(
    path: Path, imports: list[str], machine: int = payload_pe_audit.MACHINE_AMD64, *, delay: bool = False
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(synthetic_pe(imports, machine, delay=delay))
    return path


def test_the_pe_reader_lists_static_imports_in_table_order(tmp_path: Path) -> None:
    pe = write_pe(tmp_path / "x.exe", ["KERNEL32.dll", "WS2_32.dll"])
    parsed = payload_pe_audit.read_pe_imports(pe)
    assert parsed.imports == ("KERNEL32.dll", "WS2_32.dll")
    assert parsed.machine == payload_pe_audit.MACHINE_AMD64


def test_the_pe_reader_lists_a_delay_imported_dll(tmp_path: Path) -> None:
    pe = write_pe(tmp_path / "delay.exe", ["USER32.dll"], delay=True)
    parsed = payload_pe_audit.read_pe_imports(pe)
    assert parsed.imports == ("USER32.dll",)


def test_the_pe_reader_refuses_a_non_pe_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.dll"
    junk.write_bytes(b"not a pe image" * 10)
    with pytest.raises(payload_pe_audit.PeAuditError):
        payload_pe_audit.read_pe_imports(junk)


def test_the_audit_passes_system_and_shipped_imports(tmp_path: Path) -> None:
    write_pe(tmp_path / "runtime" / "python.exe", ["KERNEL32.dll", "python311.dll"])
    write_pe(tmp_path / "runtime" / "python311.dll", ["api-ms-win-crt-heap-l1-1-0.dll", "ADVAPI32.dll"])
    report = payload_pe_audit.audit_payload(tmp_path)
    assert report["pe_files_scanned"] == 2
    assert report["external_link_references"] == []


def test_the_audit_names_a_foreign_import_as_a_violation(tmp_path: Path) -> None:
    write_pe(tmp_path / "bin" / "odj-audio.exe", ["KERNEL32.dll", "libbuildhost-only.dll"])
    with pytest.raises(payload_pe_audit.PeAuditError, match=r"libbuildhost-only\.dll"):
        payload_pe_audit.audit_payload(tmp_path)


def test_the_audit_refuses_a_non_x64_binary(tmp_path: Path) -> None:
    write_pe(tmp_path / "bin" / "old.exe", ["KERNEL32.dll"], machine=0x14C)
    with pytest.raises(payload_pe_audit.PeAuditError, match="not x64"):
        payload_pe_audit.audit_payload(tmp_path)


def test_an_audit_that_scanned_nothing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(payload_pe_audit.PeAuditError, match="scanned nothing"):
        payload_pe_audit.audit_payload(tmp_path)


def test_the_vc_runtime_is_reported_when_it_does_not_ship_beside_the_importer(tmp_path: Path) -> None:
    write_pe(tmp_path / "bin" / "odj-audio.exe", ["KERNEL32.dll", "VCRUNTIME140.dll"])
    report = payload_pe_audit.audit_payload(tmp_path)
    needs = report["vc_runtime_needs_redistributable"]
    assert needs == {"vcruntime140.dll": {"importers": 1, "bin_importers": ["bin/odj-audio.exe"]}}


def test_a_vc_runtime_dll_beside_the_interpreter_resolves_for_pylib_extensions(tmp_path: Path) -> None:
    write_pe(tmp_path / "runtime" / "python.exe", ["KERNEL32.dll"])
    write_pe(tmp_path / "runtime" / "vcruntime140.dll", ["KERNEL32.dll"])
    write_pe(tmp_path / "pylib" / "ext.pyd", ["KERNEL32.dll", "VCRUNTIME140.dll"])
    write_pe(tmp_path / "bin" / "odj-audio.exe", ["KERNEL32.dll", "VCRUNTIME140.dll"])
    needs = payload_pe_audit.audit_payload(tmp_path)["vc_runtime_needs_redistributable"]
    assert needs == {"vcruntime140.dll": {"importers": 1, "bin_importers": ["bin/odj-audio.exe"]}}


def make_fake_redist(root: Path, version: str = "14.44.35112") -> Path:
    """A VC/Redist/MSVC tree with real small files, plus an older version that must lose."""
    redist = root / "MSVC"
    for ver, payload in ((version, b"new"), ("14.30.00001", b"old")):
        crt = redist / ver / "x64" / "Microsoft.VC143.CRT"
        omp = redist / ver / "x64" / "Microsoft.VC143.OpenMP"
        crt.mkdir(parents=True)
        omp.mkdir(parents=True)
        for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_atomic_wait.dll"):
            (crt / name).write_bytes(synthetic_pe(["KERNEL32.dll"]) + b"crt-" + name.encode() + payload)
        (omp / "vcomp140.dll").write_bytes(synthetic_pe(["KERNEL32.dll"]) + b"omp" + payload)
    return redist


def test_stage_vcruntime_copies_newest_crt_and_openmp_into_runtime_and_bin(tmp_path: Path) -> None:
    redist = make_fake_redist(tmp_path)
    payload = tmp_path / "payload"
    block = stage_vcruntime(payload, redist)
    for folder in ("runtime", "bin"):
        assert (payload / folder / "msvcp140.dll").read_bytes().endswith(b"crt-msvcp140.dllnew")
        assert (payload / folder / "vcomp140.dll").read_bytes().endswith(b"ompnew")
    expected = hashlib.sha256((payload / "bin" / "vcomp140.dll").read_bytes()).hexdigest()
    assert block["dlls"]["vcomp140.dll"] == expected  # type: ignore[index]
    assert set(block["dlls"]) == {  # type: ignore[arg-type]
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_atomic_wait.dll",
        "vcomp140.dll",
    }
    assert "14.44.35112" in json.dumps(block["source"])
    assert block["destinations"] == ["runtime", "bin"]


def test_stage_vcruntime_fails_naming_the_searched_location_when_none_exist(tmp_path: Path) -> None:
    empty = tmp_path / "MSVC"
    (empty / "14.44.1" / "x64").mkdir(parents=True)
    with pytest.raises(PayloadBuildError, match=r"searched .*Microsoft\.VC\*\.CRT"):
        stage_vcruntime(tmp_path / "payload", empty)
    assert not (tmp_path / "payload").exists()


def test_stage_vcruntime_fails_for_a_missing_explicit_dir_and_for_missing_vswhere(tmp_path: Path) -> None:
    with pytest.raises(PayloadBuildError, match="not a directory"):
        stage_vcruntime(tmp_path / "payload", tmp_path / "nope")
    with pytest.raises(PayloadBuildError, match=r"vswhere\.exe does not exist"):
        stage_vcruntime(tmp_path / "payload", None, vswhere=tmp_path / "vswhere.exe")


def test_stage_vcruntime_refuses_a_redist_without_the_core_dlls(tmp_path: Path) -> None:
    redist = make_fake_redist(tmp_path)
    (redist / "14.44.35112" / "x64" / "Microsoft.VC143.CRT" / "msvcp140.dll").unlink()
    with pytest.raises(PayloadBuildError, match=r"msvcp140\.dll"):
        stage_vcruntime(tmp_path / "payload", redist)


def test_strict_audit_flags_a_vc_import_with_no_shipped_copy_and_passes_after_staging(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_pe(payload / "runtime" / "python.exe", ["KERNEL32.dll"])
    write_pe(payload / "pylib" / "ext.pyd", ["KERNEL32.dll", "MSVCP140.dll", "VCOMP140.DLL"])
    write_pe(payload / "bin" / "odj-audio.exe", ["KERNEL32.dll", "VCRUNTIME140.dll"])
    with pytest.raises(payload_pe_audit.PeAuditError, match=r"(?s)odj-audio\.exe.*VCRUNTIME140.*ext\.pyd.*MSVCP140"):
        payload_pe_audit.audit_payload(payload, vc_runtime_must_ship=True)
    stage_vcruntime(payload, make_fake_redist(tmp_path))
    report = payload_pe_audit.audit_payload(payload, vc_runtime_must_ship=True)
    assert report["vc_runtime_needs_redistributable"] == {}


def test_strict_audit_still_flags_a_non_vc_foreign_import_after_staging(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    write_pe(payload / "bin" / "odj-audio.exe", ["KERNEL32.dll", "VCRUNTIME140.dll", "libbuildhost-only.dll"])
    stage_vcruntime(payload, make_fake_redist(tmp_path))
    with pytest.raises(payload_pe_audit.PeAuditError, match=r"libbuildhost-only\.dll"):
        payload_pe_audit.audit_payload(payload, vc_runtime_must_ship=True)


@pytest.mark.skipif(
    BUILT_PAYLOAD_ENV not in os.environ,
    reason=f"{BUILT_PAYLOAD_ENV} is unset: build a payload with python -m scripts.build_engine_payload_win "
    "and point this variable at it",
)
def test_a_built_payload_carries_launchers_interpreter_audio_engine_and_manifest() -> None:
    payload = Path(os.environ[BUILT_PAYLOAD_ENV])
    for relative in (
        payload_launchers_win.LAUNCHER_RELATIVE,
        payload_launchers_win.CLI_LAUNCHER_RELATIVE,
        RUNTIME_PYTHON_RELATIVE,
        AUDIO_ENGINE_WIN_RELATIVE,
    ):
        assert (payload / relative).is_file(), f"{relative} missing from {payload}"
    manifest = json.loads((payload / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["platform"] == "win32"
    assert manifest["kind"] == MANIFEST_KIND
    assert manifest["audio_engine"]["device"] is True
    assert manifest["verification"]["pe_audit"]["external_link_references"] == []
