"""scripts/ci_oss_audit_kernel.py: the one entry point both CI workflows call to build the kernel.

Every real case runs the actual cargo into a disposable CARGO_TARGET_DIR. The success cases build
the kernel itself and run it, one of them under a configured build target, the layout that puts
the executable in a triple directory. The failure cases build small throwaway crates whose real
cargo outcome is the thing under test: a compile error, and a build that succeeds but leaves no
kernel. No stub cargo is put on PATH. Without cargo the real-build cases report UNAVAILABLE as a
skip, and the missing-toolchain case still runs, since it needs no cargo. The executable is read
from cargo's own JSON report, which is checked on report text in both directions; the torn-target
classifier is checked on log text in both directions. A real torn target cannot be produced on
demand, so the clean-and-retry itself is covered by the shell helper's own tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import ci_oss_audit_kernel as kernel

# Cargo writes the kernel as <name>.exe on Windows; the expected path must carry the same suffix.
KERNEL_FILE = kernel.BINARY_NAME + (".exe" if os.name == "nt" else "")

needs_cargo = pytest.mark.skipif(
    shutil.which("cargo") is None, reason="UNAVAILABLE: cargo is not on PATH, so the real build cannot run here"
)

E0786_LOG = "   Compiling time_macros v0.1.0\nerror[E0786]: found invalid metadata files for crate `time_macros`\n"
E0463_LOCKED_LOG = "error[E0463]: can't find crate for `proc_macro_error`\n"
E0463_UNLOCKED_LOG = "error[E0463]: can't find crate for `not_a_dependency`\n"
E0425_LOG = "error[E0425]: cannot find value `missing` in this scope\n"
LOCK_TEXT = 'version = 3\n\n[[package]]\nname = "proc_macro_error"\nversion = "1.0.4"\n'


def _crate(root: Path, *, main_rs: str, bin_name: str = kernel.BINARY_NAME) -> Path:
    """A throwaway crate with its own lockfile, so `--locked` has nothing to update."""
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n\n'
        f'[[bin]]\nname = "{bin_name}"\npath = "src/main.rs"\n',
        encoding="utf-8",
        newline="\n",
    )
    (root / "Cargo.lock").write_text(
        'version = 3\n\n[[package]]\nname = "probe"\nversion = "0.1.0"\n', encoding="utf-8", newline="\n"
    )
    (root / "src" / "main.rs").write_text(main_rs, encoding="utf-8", newline="\n")
    return root / "Cargo.toml"


def _host_triple() -> str | None:
    """The host triple rustc reports, or None when rustc is not on PATH."""
    rustc = shutil.which("rustc")
    if rustc is None:
        return None
    verbose = subprocess.run([rustc, "-vV"], capture_output=True, text=True, check=False).stdout
    return next((line.split(": ", 1)[1] for line in verbose.splitlines() if line.startswith("host: ")), None)


def _github_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


@pytest.fixture
def throwaway(monkeypatch, tmp_path):
    """The entry point pointed at a disposable target dir and a throwaway manifest."""
    target = tmp_path / "target"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    env = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env))
    return {"tmp": tmp_path, "env": env, "target": target}


@needs_cargo
def test_success_builds_the_real_kernel_and_exports_where_it_is(tmp_path, monkeypatch):
    target = tmp_path / "target"
    env = tmp_path / "github_env"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    monkeypatch.setenv("GITHUB_ENV", str(env))
    assert kernel.main() == 0
    exported = _github_env(env)
    binary = Path(exported.pop("ODJ_OSS_AUDIT_NATIVE_BIN"))
    assert exported == {"ODJ_OSS_AUDIT_BACKEND": "auto", "ODJ_REQUIRE_OSS_AUDIT_NATIVE": "1"}
    assert binary == target / "release" / KERNEL_FILE
    assert binary.is_file() and binary.stat().st_mode & 0o111
    answer = subprocess.run([str(binary)], input=b"QUIT\n", capture_output=True, check=False, timeout=30)
    assert answer.returncode == 0, answer.stderr


@needs_cargo
@pytest.mark.skipif(_host_triple() is None, reason="UNAVAILABLE: rustc is not on PATH, so no host triple")
def test_a_configured_build_target_is_found_where_cargo_put_it(throwaway, monkeypatch):
    """The layout the CI helper must handle: cargo writes target/<triple>/release/<kernel>."""
    triple = _host_triple()
    monkeypatch.setenv("CARGO_BUILD_TARGET", triple)
    monkeypatch.setattr(kernel, "MANIFEST", str(_crate(throwaway["tmp"] / "triple", main_rs="fn main() {}\n")))
    assert kernel.main() == 0
    binary = Path(_github_env(throwaway["env"])["ODJ_OSS_AUDIT_NATIVE_BIN"])
    assert binary == throwaway["target"] / triple / "release" / KERNEL_FILE
    assert binary.is_file()


@needs_cargo
def test_a_real_compile_error_fails_with_cargos_exit_code_and_exports_nothing(throwaway, monkeypatch):
    monkeypatch.setattr(
        kernel, "MANIFEST", str(_crate(throwaway["tmp"] / "bad", main_rs='fn main() { let x: u8 = "no"; }\n'))
    )
    assert kernel.main() == 101
    assert not throwaway["env"].exists()


@needs_cargo
def test_a_build_that_succeeds_but_reports_no_kernel_is_missing_not_ready(throwaway, monkeypatch, capsys):
    crate = _crate(throwaway["tmp"] / "other", main_rs="fn main() {}\n", bin_name="not-the-kernel")
    monkeypatch.setattr(kernel, "MANIFEST", str(crate))
    assert kernel.main() == 1
    assert "oss audit kernel missing" in capsys.readouterr().out
    assert not throwaway["env"].exists()


def test_missing_toolchain_fails_by_name_before_any_build(throwaway, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(throwaway["tmp"] / "empty"))  # no cargo on PATH
    assert kernel.main() == 2
    assert "toolchain missing" in capsys.readouterr().out
    assert not throwaway["env"].exists()


def _artifact(name: str, executable: str | None) -> str:
    message: dict = {"reason": "compiler-artifact", "target": {"name": name}}
    if executable is not None:
        message["executable"] = executable
    return json.dumps(message)


def test_the_executable_is_the_one_cargo_reports_for_the_kernel():
    report = "\n".join(
        [
            json.dumps({"reason": "compiler-message", "message": {"rendered": "warning"}}),
            _artifact("some-dependency", "/x/some-dependency"),
            _artifact(kernel.BINARY_NAME, "/cargo/out/aarch64/release/oss-tip-audit-native"),
        ]
    )
    assert kernel.executable_from_cargo(report) == Path("/cargo/out/aarch64/release/oss-tip-audit-native")


def test_a_malformed_cargo_line_is_rejected_not_skipped_beside_a_valid_artifact():
    """What could satisfy this without satisfying its intent? A parser that skips bad lines still
    returns the artifact, so the malformed line itself must raise, whatever record sits beside it."""
    report = "\n".join([_artifact(kernel.BINARY_NAME, "/x/oss-tip-audit-native"), "not json at all"])
    with pytest.raises(ValueError, match="not JSON"):
        kernel.executable_from_cargo(report)


def test_a_json_line_that_is_not_an_object_is_rejected():
    with pytest.raises(TypeError, match="not a JSON object"):
        kernel.executable_from_cargo(_artifact(kernel.BINARY_NAME, "/x/oss-tip-audit-native") + "\n[1, 2]")


@pytest.mark.parametrize(
    "report",
    [
        "",
        _artifact("some-dependency", "/x/some-dependency"),
        _artifact(kernel.BINARY_NAME, None),
        json.dumps({"reason": "build-finished", "success": True}),
    ],
)
def test_no_kernel_in_cargos_report_means_no_executable(report):
    assert kernel.executable_from_cargo(report) is None


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        (E0786_LOG, True),
        (E0463_LOCKED_LOG, True),
        (E0463_UNLOCKED_LOG, False),  # a real misconfiguration, not corruption
        (E0425_LOG, False),  # a genuine compile error is never retried
        ("   error[E0786]: indented text from a test's own output\n", False),  # not cargo's line
        ("", False),
    ],
)
def test_only_the_torn_target_signatures_are_retried(tmp_path, log, expected):
    lock = tmp_path / "Cargo.lock"
    lock.write_text(LOCK_TEXT, encoding="utf-8", newline="\n")
    assert kernel.is_corrupted_target(log, lock) is expected


def test_a_hyphenated_locked_crate_is_found_under_rustcs_underscore_name(tmp_path):
    """What could satisfy this without satisfying its intent? A matcher that compares the raw
    names would still pass the unhyphenated case above, so the lock here names `fancy-regex` and
    the log names the crate the way rustc does, with an underscore."""
    lock = tmp_path / "Cargo.lock"
    lock.write_text(
        'version = 3\n\n[[package]]\nname = "fancy-regex"\nversion = "0.14.0"\n', encoding="utf-8", newline="\n"
    )
    assert kernel.is_corrupted_target("error[E0463]: can't find crate for `fancy_regex`\n", lock) is True
    assert kernel.is_corrupted_target("error[E0463]: can't find crate for `fancy_missing`\n", lock) is False


def test_e0463_is_not_corruption_when_there_is_no_lockfile(tmp_path):
    assert kernel.is_corrupted_target(E0463_LOCKED_LOG, tmp_path / "absent.lock") is False


@needs_cargo
def test_json_render_diagnostics_puts_compile_errors_on_stderr_not_in_the_report(tmp_path):
    """The torn-target classifier reads stderr, which is where cargo renders rustc's errors.

    What could satisfy this without satisfying its intent? A cargo that also printed the errors
    as JSON would still pass the stderr check, so the report is asserted to hold no compiler
    message at all. The error code must appear on stderr, so the check cannot pass on an empty run.
    """
    manifest = _crate(tmp_path / "bad", main_rs='fn main() { let x: u8 = "no"; }\n')
    status, report, diagnostics = kernel._cargo(
        shutil.which("cargo"),
        "build",
        "--release",
        "--locked",
        "--message-format=json-render-diagnostics",
        "--manifest-path",
        str(manifest),
    )
    assert status != 0
    assert "error[E0308]" in diagnostics
    assert "compiler-message" not in report
