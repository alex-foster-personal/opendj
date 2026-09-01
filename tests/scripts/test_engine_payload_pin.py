"""The bundled interpreter must match the repo's .python-version pin.

Split out of test_engine_payload.py, which crossed the 600-line file-size
ratchet when this section landed and turned trunk red. Registered in
macos-packaging.yml's PACKAGING_TESTS alongside it; the ubuntu fast lane
collects all of tests/ and needs no registration.

- if the payload embeds a runtime off the pinned minor line, testers run a
  different Python from the one the lockfile resolved against -> broken.
- if a missing or unparseable pin is tolerated, the build silently stops
  checking the thing this gate exists for -> broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    PayloadBuildError,
    assert_runtime_matches_pin,
    read_python_pin,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def test_the_pin_is_read_verbatim(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.11.15\n", encoding="utf-8")
    assert read_python_pin(tmp_path) == "3.11.15"


def test_a_missing_pin_stops_the_build(tmp_path: Path) -> None:
    with pytest.raises(PayloadBuildError) as excinfo:
        read_python_pin(tmp_path)
    assert ".python-version" in str(excinfo.value)


def test_an_unparseable_pin_stops_the_build(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("cpython@3.11\n", encoding="utf-8")
    with pytest.raises(PayloadBuildError):
        read_python_pin(tmp_path)


def test_a_runtime_off_the_pinned_line_is_refused(tmp_path: Path) -> None:
    """The Tue 1 Sep 2026 failure: uv resolved 3.14.7, the pin meant 3.11."""
    with pytest.raises(PayloadBuildError) as excinfo:
        assert_runtime_matches_pin(
            runtime_version="3.14.7",
            pin="3.11.15",
            runtime_source=tmp_path,
        )
    message = str(excinfo.value)
    assert "3.14.7" in message
    assert "3.11.15" in message


def test_a_runtime_on_the_pinned_line_passes(tmp_path: Path) -> None:
    assert_runtime_matches_pin(
        runtime_version="3.11.15", pin="3.11.15", runtime_source=tmp_path
    )


def test_patch_drift_within_the_pinned_line_passes(tmp_path: Path) -> None:
    """The contract is major.minor: 3.11.16 against a 3.11.15 pin ships."""
    assert_runtime_matches_pin(
        runtime_version="3.11.16", pin="3.11.15", runtime_source=tmp_path
    )


def test_the_repo_pin_is_itself_parseable() -> None:
    """The committed .python-version must satisfy the reader that gates dmg."""
    read_python_pin(REPO_ROOT)

