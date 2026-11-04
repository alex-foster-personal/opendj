"""build_audio_engine: only a missing toolchain is UNAVAILABLE, and only off the gate.

Regression lines:
- if a host with no libclang fails the build then broken: pytest shards not
  provisioned for this crate go red on a gap that says nothing about the change
- if MDT_REQUIRE_AUDIO_ENGINE_BUILD=1 lets that gap skip then broken: the one
  job that must run the real engine could report green without running it
- if any other build failure skips then broken: a real compile error would hide
  as a capability report
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from tests.rust_build_env import NO_LIBCLANG, REQUIRE_ENV, bindgen_env, build_audio_engine

LIBCLANG_PANIC = f'panicked at bindgen-0.70.1/lib.rs:622:27:\n{NO_LIBCLANG}: "couldn\'t find"'
# Caught together, so an outcome of the wrong kind fails the test: a stray skip
# escaping it would only skip it, and a skipped test is not a red one.
OUTCOMES = (pytest.skip.Exception, pytest.fail.Exception)


def _outcome(crate: Path) -> tuple[type[BaseException], str]:
    with pytest.raises(OUTCOMES) as caught:
        build_audio_engine(crate)
    return caught.type, str(caught.value)


def _fake_tool(bin_dir: Path, name: str, *, stdout: str = "", stderr: str = "", code: int = 0) -> None:
    """Put ``name`` in ``bin_dir``: it prints ``stdout`` and ``stderr`` and exits ``code``.

    A shell script on POSIX. Windows runs no shebang scripts, so there it is
    ``<name>.cmd`` handing over to a Python stub, found through PATHEXT the
    way the real ``<name>.exe`` is.
    """
    bin_dir.mkdir(exist_ok=True)
    if os.name == "nt":
        stub = bin_dir / f"{name}_stub.py"
        stub.write_text(
            "import sys\n"
            f"sys.stdout.write({stdout!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"raise SystemExit({code})\n",
            encoding="utf-8",
        )
        (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{stub}" %*\n', encoding="utf-8")
        return
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\ncat <<'OUT'\n{stdout}\nOUT\ncat >&2 <<'OUT'\n{stderr}\nOUT\nexit {code}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


def _fake_cargo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, code: int) -> None:
    bin_dir = tmp_path / "bin"
    _fake_tool(bin_dir, "cargo", stderr=stderr, code=code)
    monkeypatch.setenv("PATH", str(bin_dir) if os.name == "nt" else f"{bin_dir}:/usr/bin:/bin")


def test_no_libclang_is_unavailable_off_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cargo(tmp_path, monkeypatch, LIBCLANG_PANIC, 101)
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    kind, message = _outcome(tmp_path)
    assert kind is pytest.skip.Exception
    assert "UNAVAILABLE: no libclang" in message


def test_no_libclang_fails_where_the_build_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cargo(tmp_path, monkeypatch, LIBCLANG_PANIC, 101)
    monkeypatch.setenv(REQUIRE_ENV, "1")
    kind, message = _outcome(tmp_path)
    assert kind is pytest.fail.Exception
    # Both halves: a missing cargo "requires the build" too, and would pass
    # this test without the fake cargo ever having run.
    assert "no libclang" in message and "requires the build" in message


def test_any_other_build_failure_fails_even_off_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cargo(tmp_path, monkeypatch, "error[E0308]: mismatched types", 101)
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    kind, message = _outcome(tmp_path)
    assert kind is pytest.fail.Exception
    assert "E0308" in message


def test_a_good_build_returns_the_debug_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cargo(tmp_path, monkeypatch, "", 0)
    name = "odj-audio.exe" if sys.platform == "win32" else "odj-audio"
    assert build_audio_engine(tmp_path) == tmp_path / "target" / "debug" / name


def _fake_gcc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``gcc`` on PATH whose ``-print-file-name=include`` names a real directory."""
    include = tmp_path / "gcc-include"
    include.mkdir()
    _fake_tool(tmp_path / "bin", "gcc", stdout=str(include))
    monkeypatch.setenv("PATH", str(tmp_path / "bin") if os.name == "nt" else f"{tmp_path / 'bin'}:/usr/bin:/bin")
    return include


@pytest.mark.skipif(os.name == "nt", reason="the MSVC build takes no gcc headers; see the Windows test below")
def test_gccs_builtin_headers_are_added_for_bindgen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    include = _fake_gcc(tmp_path, monkeypatch)
    env = bindgen_env({"BINDGEN_EXTRA_CLANG_ARGS": "-DKEPT"})
    assert env["BINDGEN_EXTRA_CLANG_ARGS"] == f"-DKEPT -I{include}"


@pytest.mark.skipif(os.name != "nt", reason="about the MSVC target; POSIX is the test above")
def test_a_gcc_on_a_windows_path_is_not_asked_for_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Measured on Windows 11 (Thu 1 Oct 2026): an include directory in
    # BINDGEN_EXTRA_CLANG_ARGS is searched before MSVC's own headers, so a
    # MinGW gcc that happens to be on PATH would hand bindgen another
    # compiler's stddef.h. The MSVC build needs none: it finds the Visual
    # Studio headers by itself.
    include = _fake_gcc(tmp_path, monkeypatch)
    # Control: the gcc is there to be found, and names a real directory.
    assert shutil.which("gcc") is not None and include.is_dir()
    assert bindgen_env({"BINDGEN_EXTRA_CLANG_ARGS": "-DKEPT"}) == {"BINDGEN_EXTRA_CLANG_ARGS": "-DKEPT"}
    assert bindgen_env({}) == {}
