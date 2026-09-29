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

import stat
from pathlib import Path

import pytest

from tests.rust_build_env import NO_LIBCLANG, REQUIRE_ENV, build_audio_engine

LIBCLANG_PANIC = f'panicked at bindgen-0.70.1/lib.rs:622:27:\n{NO_LIBCLANG}: "couldn\'t find"'
# Caught together, so an outcome of the wrong kind fails the test: a stray skip
# escaping it would only skip it, and a skipped test is not a red one.
OUTCOMES = (pytest.skip.Exception, pytest.fail.Exception)


def _outcome(crate: Path) -> tuple[type[BaseException], str]:
    with pytest.raises(OUTCOMES) as caught:
        build_audio_engine(crate)
    return caught.type, str(caught.value)


def _fake_cargo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, code: int) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text(f"#!/bin/sh\ncat >&2 <<'OUT'\n{stderr}\nOUT\nexit {code}\n")
    cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


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
    assert "requires the build" in message


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
    assert build_audio_engine(tmp_path) == tmp_path / "target" / "debug" / "odj-audio"
