"""Explicit library residency tests using real directories and env mappings."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared import library_mode


pytestmark = pytest.mark.requirement("INFRA-01")


def test_explicit_local_needs_no_crate() -> None:
    assert library_mode.library_mode(environ={"MDT_LIBRARY_MODE": "local"}) == "local"
    library_mode.assert_ready(environ={"MDT_LIBRARY_MODE": "local"})


def test_remote_requires_absolute_crate_root() -> None:
    env = {"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": "relative/crate"}
    with pytest.raises(RuntimeError, match="must be an absolute path"):
        library_mode.assert_ready(environ=env)


def test_remote_requires_materialised_crate_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    env = {"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": str(missing)}
    with pytest.raises(RuntimeError, match="is not a directory"):
        library_mode.assert_ready(environ=env)


def test_remote_accepts_real_crate_root(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    crate.mkdir()
    env = {"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": str(crate)}
    assert library_mode.library_mode(environ=env) == "remote"
    assert library_mode.crate_root(environ=env) == crate
    library_mode.assert_ready(environ=env)


def test_dotenv_only_fills_missing_values(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"MDT_LIBRARY_MODE=remote\nMDT_CRATE_ROOT={crate}\n",
        encoding="utf-8",
    )
    env = {"MDT_LIBRARY_MODE": "local"}
    library_mode.apply_library_env(environ=env, dotenv_path=dotenv)
    assert env == {
        "MDT_LIBRARY_MODE": "local",
        "MDT_CRATE_ROOT": str(crate),
    }
