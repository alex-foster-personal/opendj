"""The Windows payload stager fails loudly on what it cannot stage and says what it left out.

[if] the stager accepts a runtime without python.exe, or a manifest hides what was not staged [then] a half-built payload looks complete, [else stop]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from apps.engine_core.build_info import REQUIRED_IDENTITY_KEYS
from scripts import build_engine_payload_win as stager
from scripts.build_engine_payload import (
    _WIN_DEFAULT_PATHEXT,
    PayloadBuildError,
    _program_names,
)


def test_a_runtime_without_python_exe_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").write_text("", encoding="utf-8")  # a macOS-shaped runtime
    with pytest.raises(PayloadBuildError, match=r"python\.exe"):
        stager.stage_windows_runtime(tmp_path, tmp_path / "out" / "runtime")


def test_a_windows_runtime_is_copied_with_its_interpreter_at_the_root(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "Lib" / "__pycache__").mkdir(parents=True)
    (source / "python.exe").write_bytes(b"MZ")
    (source / "Lib" / "os.py").write_text("x = 1\n", encoding="utf-8")
    (source / "Lib" / "__pycache__" / "os.cpython-311.pyc").write_bytes(b"\0")
    destination = tmp_path / "out" / "runtime"
    stager.stage_windows_runtime(source, destination)
    assert (destination / "python.exe").read_bytes() == b"MZ"
    assert (destination / "Lib" / "os.py").exists()
    assert not list(destination.rglob("*.pyc")), "bytecode must not be shipped; the payload is read-only at run time"


def test_the_manifest_names_exactly_what_was_not_staged(tmp_path: Path) -> None:
    manifest = stager.windows_manifest(
        identity={"product_name": "Open DJ", "bundle_identifier": "com.opendj.desktop", "app_version": "1.2.3"},
        python_version="3.11.15",
        audio_engine_hello={"type": "hello", "protocol": 1, "device": True},
        git={"git_sha": "abc", "git_sha_full": "abcdef", "git_branch": "b", "git_dirty": False},
    )
    assert manifest["identity"]["app_version"] == "1.2.3", "the engine reads its version from identity.app_version"
    assert manifest["platform"] == "win32"
    assert not [key for key in REQUIRED_IDENTITY_KEYS if key not in manifest["identity"]], "the engine's reader needs them all"
    assert manifest["identity"]["lane_label"] == "", "a plain product build has an empty lane label"
    assert set(manifest["not_staged"]) == set(stager.NOT_STAGED)
    assert all(reason.strip() for reason in manifest["not_staged"].values()), "every omission needs a stated reason"
    json.dumps(manifest)  # serializable


def test_a_system_tool_comes_from_under_the_system_root_or_stops_the_build() -> None:
    env = {"SystemRoot": r"C:\Windows", "Path": r"C:\evil"}
    assert stager.windows_system_tool("cmd.exe", env, lambda _: True) == r"C:\Windows\System32\cmd.exe"
    with pytest.raises(PayloadBuildError, match="missing"):
        stager.windows_system_tool("cmd.exe", env, lambda _: False)
    with pytest.raises(PayloadBuildError, match="no absolute SystemRoot"):
        stager.windows_system_tool("cmd.exe", {"SystemRoot": "Windows"}, lambda _: True)


def test_a_manifest_the_engine_would_refuse_stops_the_build() -> None:
    with pytest.raises(PayloadBuildError, match="identity lacks"):
        stager.windows_manifest(identity={}, python_version="3.11.15", audio_engine_hello={}, git={})


def test_the_stager_refuses_to_run_off_windows_instead_of_staging_the_wrong_wheels(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("this guard only fires on a non-Windows host, so there is nothing to refuse here")
    with pytest.raises(PayloadBuildError, match="Windows"):
        stager.build_windows_payload(repo_root=tmp_path, output_dir=tmp_path / "out", runtime_source=tmp_path)


@pytest.mark.skipif(
    not os.environ.get("ODJ_WINDOWS_PAYLOAD_DIR"),
    reason="needs a payload built by `python -m scripts.build_engine_payload_win`; set ODJ_WINDOWS_PAYLOAD_DIR to its path",
)
def test_a_built_windows_payload_boots_its_engine_and_answers_health(tmp_path: Path) -> None:
    report = stager.boot_proof(Path(os.environ["ODJ_WINDOWS_PAYLOAD_DIR"]), tmp_path / "data")
    assert report["health_status"] == 200
    assert report["lock_holder_pid"] > 0


def test_a_missing_program_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] cargo or uv is not on PATH [then] the stager refuses naming it, [else stop]."""
    monkeypatch.setenv("PATH", "")
    with pytest.raises(stager.PayloadBuildError, match="cargo is not on PATH"):
        stager.resolve_program("cargo", "cannot build the audio engine")


def test_a_program_resolves_to_the_absolute_path_that_is_launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a program is on PATH [then] the stager launches its absolute path, not the bare name, [else stop]."""
    program = tmp_path / ("uv.exe" if sys.platform == "win32" else "uv")
    program.write_text("")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = stager.resolve_program("uv", "cannot resolve the repo interpreter")
    assert Path(resolved).is_absolute()
    assert Path(resolved).resolve() == program.resolve()


def test_a_cwd_shadow_on_path_is_not_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] PATH lists a relative entry before a trusted dir [then] the trusted program wins, not cwd, [else stop]."""
    program_name = "uv.exe" if sys.platform == "win32" else "uv"
    cwd_dir = tmp_path / "cwd"
    trusted_dir = tmp_path / "trusted"
    cwd_dir.mkdir()
    trusted_dir.mkdir()
    shadow = cwd_dir / program_name
    trusted = trusted_dir / program_name
    shadow.write_text("")
    trusted.write_text("")
    shadow.chmod(0o755)
    trusted.chmod(0o755)
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setenv("PATH", f".{os.pathsep}{trusted_dir}")
    resolved = stager.resolve_program("uv", "cannot resolve the repo interpreter")
    assert Path(resolved).is_absolute()
    assert Path(resolved).resolve() == trusted.resolve()
    assert Path(resolved).resolve() != shadow.resolve()


def test_a_relative_only_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] PATH has only a relative entry [then] resolve_program refuses, [else stop]."""
    program_name = "uv.exe" if sys.platform == "win32" else "uv"
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    program = shadow_dir / program_name
    program.write_text("")
    program.chmod(0o755)
    monkeypatch.chdir(shadow_dir)
    monkeypatch.setenv("PATH", ".")
    with pytest.raises(PayloadBuildError, match="uv is not on PATH"):
        stager.resolve_program("uv", "cannot resolve the repo interpreter")


def test_an_absolute_path_entry_with_a_space_still_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] an absolute PATH dir contains a space [then] the program inside still resolves, [else stop]."""
    tool_dir = tmp_path / "tool bin"
    tool_dir.mkdir()
    program_name = "uv.exe" if sys.platform == "win32" else "uv"
    program = tool_dir / program_name
    program.write_text("")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_dir))
    resolved = stager.resolve_program("uv", "cannot resolve the repo interpreter")
    assert Path(resolved).is_absolute()
    assert Path(resolved).resolve() == program.resolve()


def test_program_names_follows_pathext_on_windows() -> None:
    """[if] _program_names runs on win32 [then] PATHEXT extensions are appended unless already present, [else stop]."""
    default = _WIN_DEFAULT_PATHEXT
    assert _program_names("uv", platform="linux", pathext=default) == ("uv",)
    assert _program_names("uv", platform="darwin", pathext=default) == ("uv",)
    win_names = _program_names("uv", platform="win32", pathext=default)
    assert win_names[0] == "uv"
    assert "uv.EXE" in win_names
    assert _program_names("uv.exe", platform="win32", pathext=default) == ("uv.exe",)
