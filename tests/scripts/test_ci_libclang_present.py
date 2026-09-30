"""scripts/ci_libclang_present.py finds libclang the way clang-sys does, and CI uses it.

bindgen (signalsmith-stretch, apps/audio-engine) dlopens ONE libclang picked by
clang-sys 1.9.1's search, and panics 'Unable to find libclang' when that fails
(PR #4361, agentbox, Wed 30 Sep 2026). The probe is the capability check behind
both the contracts job's install-if-absent step and the manifest's verify.

Regression lines:
  - if a libclang under /usr/lib/llvm-21/lib is not found then nucbox-wsl reads as MISSING
  - if LIBCLANG_PATH is set and anything else is searched then the pick is not clang-sys's
  - if the newest candidate does not load and the probe passes then CI skips an install it needs
  - if a real library that is not libclang counts as loaded then a broken host passes
  - if the contracts job builds the wheel or runs cargo before the probe step then broken
  - if the manifest verify does not carry this exact script then verify and CI disagree
"""

from __future__ import annotations

import base64
import ctypes.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_libclang_present as probe_mod
from scripts.runner_toolset_scan import REPO_ROOT, load_manifest
from scripts.runner_toolset_verify import expand_verify

SCRIPT = REPO_ROOT / "scripts" / "ci_libclang_present.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PROBE = "scripts.ci_libclang_present"
INSTALL = "apt-get install -y libclang1"


def _lib(path: Path, bits: int = 64) -> Path:
    """A file with a real ELF header of `bits` class: a candidate, not a loadable library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + bytes([1 if bits == 32 else 2]) + b"\0" * 59)
    return path


def _loads(version: str = "21.1.2"):
    def load(path: Path) -> str:
        return f"Ubuntu clang version {version} (1ubuntu1)"

    return load


def _search(root: Path, environ: dict[str, str] | None = None, prefix: str | None = None):
    return probe_mod.search_directories(environ or {}, prefix, root)


# ----- search order (R1) --------------------------------------------------------


def test_search_order_is_clang_sys_order(tmp_path: Path) -> None:
    root = tmp_path / "root"
    local = _lib(root / "usr/local/lib/libclang.so.1")
    llvm = _lib(root / "usr/lib/llvm-18/lib/libclang-18.so.1")
    _lib(root / "usr/lib/llvm-18/lib/libclang-cpp.so.18")
    multiarch = _lib(root / "usr/lib/x86_64-linux-gnu/libclang-18.so.18")
    ld = _lib(tmp_path / "ld/libclang-17.so")
    prefixed = _lib(tmp_path / "prefix/lib/libclang.so")
    found = _search(root, {"LD_LIBRARY_PATH": str(ld.parent)}, str(tmp_path / "prefix"))
    assert found == [prefixed, ld, local, llvm, multiarch], found


def test_llvm_21_alone_is_found_without_libclang_path(tmp_path: Path) -> None:
    """nucbox-wsl's shape: LLVM 21 only, nothing from LLVM 18, no LIBCLANG_PATH."""
    lib = _lib(tmp_path / "usr/lib/llvm-21/lib/libclang-21.so.1")
    code, message = probe_mod.probe({}, None, _loads("21.1.2"), tmp_path, 64)
    assert (code, message.split(" ")[:5]) == (0, ["libclang", "21.1.2", "loaded", "from", str(lib)])


def test_libclang_path_is_the_only_place_searched(tmp_path: Path) -> None:
    _lib(tmp_path / "root/usr/lib/llvm-21/lib/libclang-21.so.1")
    given = _lib(tmp_path / "given/libclang-18.so.1")
    sibling = _lib(tmp_path / "given/libclang.so.1")
    root = tmp_path / "root"
    # Pattern order, not name order: libclang.so.* is tried before libclang-*.so.*.
    assert _search(root, {"LIBCLANG_PATH": str(given.parent)}) == [sibling, given]
    assert _search(root, {"LIBCLANG_PATH": str(given)}) == [given]
    assert _search(root, {"LIBCLANG_PATH": str(tmp_path / "absent")}) == []


@pytest.mark.parametrize(
    ("filename", "version"),
    [
        ("libclang-18.so.1", (18, 0)),
        ("libclang-17.so", (17,)),
        ("libclang.so.1", (1,)),
        ("libclang.so.21.1", (21, 1)),
        ("libclang.so", ()),
    ],
)
def test_filename_version_matches_clang_sys(filename: str, version: tuple[int, ...]) -> None:
    assert probe_mod.parse_version(filename) == version


# ----- the pick (R2) ------------------------------------------------------------


def test_highest_version_wins_over_search_order(tmp_path: Path) -> None:
    old = _lib(tmp_path / "a/libclang-18.so.1")
    new = _lib(tmp_path / "b/libclang-21.so.1")
    assert probe_mod.pick([old, new], 64)[0].path == new


def test_a_tie_goes_to_the_earliest_found(tmp_path: Path) -> None:
    first = _lib(tmp_path / "a/libclang-18.so.1")
    second = _lib(tmp_path / "b/libclang-18.so.18")
    assert probe_mod.pick([first, second], 64)[0].path == first


def test_wrong_elf_class_non_elf_and_absent_files_are_not_candidates(tmp_path: Path) -> None:
    good = _lib(tmp_path / "a/libclang-18.so.1")
    thirty_two = _lib(tmp_path / "b/libclang-21.so.1", bits=32)
    text = tmp_path / "c/libclang-22.so.1"
    text.parent.mkdir()
    text.write_text("not a library")
    absent = tmp_path / "d/libclang-23.so.1"
    chosen, valid = probe_mod.pick([good, thirty_two, text, absent], 64)
    assert (chosen.path, valid) == (good, 1)
    assert probe_mod.pick([thirty_two, text, absent], 64) == (None, 0)


# ----- the verdict (R3) ---------------------------------------------------------


def test_no_candidate_is_missing(tmp_path: Path) -> None:
    code, message = probe_mod.probe({}, None, _loads(), tmp_path, 64)
    assert code == 1 and message.startswith("[libclang] MISSING"), message


def test_the_pick_failing_to_load_is_missing_with_no_fallback(tmp_path: Path) -> None:
    """clang-sys dlopens its one pick; an older loadable libclang does not rescue it."""
    _lib(tmp_path / "usr/lib/llvm-18/lib/libclang-18.so.1")
    newest = _lib(tmp_path / "usr/lib/llvm-21/lib/libclang-21.so.1")

    def load(path: Path) -> str:
        if path == newest:
            raise OSError("cannot open shared object file")
        return "Ubuntu clang version 18.1.3"

    code, message = probe_mod.probe({}, None, load, tmp_path, 64)
    assert code == 1 and str(newest) in message and "does not load" in message, message


def test_a_load_without_a_version_is_missing(tmp_path: Path) -> None:
    _lib(tmp_path / "usr/lib/llvm-18/lib/libclang-18.so.1")
    code, message = probe_mod.probe({}, None, lambda _: "", tmp_path, 64)
    assert code == 1 and "reported no version" in message, message


def test_the_real_loader_rejects_an_absent_file_and_a_fake_elf(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        probe_mod.load_clang_version(tmp_path / "libclang-99.so.1")
    fake = _lib(tmp_path / "usr/lib/llvm-99/lib/libclang-99.so.1")
    with pytest.raises(OSError):
        probe_mod.load_clang_version(fake)
    code, message = probe_mod.probe({}, None, probe_mod.load_clang_version, tmp_path, 64)
    assert code == 1 and "does not load" in message, message


def test_the_real_loader_rejects_a_real_library_that_is_not_libclang() -> None:
    """Control: dlopen succeeds here, so only the clang_getClangVersion call can say no."""
    libc = ctypes.util.find_library("c")
    assert libc, "no libc to load: the control cannot run, which is not a pass"
    with pytest.raises(AttributeError):
        probe_mod.load_clang_version(Path(libc))


# ----- wiring: the contracts job and the manifest ---------------------------------


def _contracts_steps() -> list[dict]:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]["contracts"]["steps"]


def _step_index(steps: list[dict], needle: str) -> int:
    hits = [i for i, step in enumerate(steps) if needle in step.get("run", "")]
    assert len(hits) == 1, f"{needle!r} is run by {len(hits)} contracts steps, want exactly 1"
    return hits[0]


def test_contracts_job_probes_libclang_before_any_bindgen_build() -> None:
    steps = _contracts_steps()
    probe_at = _step_index(steps, PROBE)
    for builder in (
        "cargo test --manifest-path apps/audio-engine/Cargo.toml",
        "cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml",
        "make waveform-native-release-check",
    ):
        assert probe_at < _step_index(steps, builder), f"{builder!r} runs before the probe"


def test_contracts_job_installs_only_when_the_probe_fails_then_reprobes() -> None:
    run = _contracts_steps()[_step_index(_contracts_steps(), PROBE)]["run"]
    probe_first, _, fallback = run.partition("||")
    assert PROBE in probe_first and INSTALL not in probe_first, run
    assert "scripts/ci_host_lock.sh packages sudo" in fallback and INSTALL in fallback, run
    assert fallback.index(PROBE) > fallback.index(INSTALL), "no probe after the install"


def _libclang_entry() -> dict:
    entries = [e for e in load_manifest()["entries"] if e["name"].startswith("libclang")]
    assert len(entries) == 1, [e["name"] for e in entries]
    return entries[0]


def test_manifest_entry_installs_what_ci_installs_and_verifies_with_the_probe() -> None:
    entry = _libclang_entry()
    assert entry["install"] == INSTALL
    payloads = re.findall(r"echo (\S+) \| base64 -d", expand_verify(entry["verify"]))
    assert [base64.b64decode(p) for p in payloads] == [SCRIPT.read_bytes()]


def test_the_expanded_verify_runs_the_probe() -> None:
    """Presence, not exit code: every probe outcome names libclang, an empty decode does not."""
    done = subprocess.run(
        ["bash", "-o", "pipefail", "-c", expand_verify(_libclang_entry()["verify"])],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = done.stdout + done.stderr
    assert re.match(r"(libclang \d|\[libclang\] (MISSING|UNKNOWN))", output), output


def test_expand_verify_refuses_a_missing_repo_file() -> None:
    with pytest.raises(FileNotFoundError):
        expand_verify('python3 -c "$(echo {repo_b64:scripts/no_such_probe.py} | base64 -d)"')
