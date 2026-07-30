"""Residency helper: materialised bytes vs Darwin dataless stubs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apps.shared.fs_residency import (
    is_dataless_stub,
    is_materialised,
    materialised_size,
)


def test_materialised_reports_real_file_and_size(tmp_path: Path) -> None:
    sample = tmp_path / "t.mp3"
    sample.write_bytes(b"x" * 4096)
    assert is_materialised(sample) is True
    assert materialised_size(sample) == 4096


def test_missing_path_is_not_materialised(tmp_path: Path) -> None:
    missing = tmp_path / "nope.mp3"
    assert is_materialised(missing) is False
    assert materialised_size(missing) is None


def test_empty_regular_file_is_materialised(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    assert is_materialised(empty) is True
    assert materialised_size(empty) == 0


def test_sparse_nonzero_zero_blocks_is_dataless_on_darwin(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse.mp3"
    with open(sparse, "wb") as handle:
        handle.truncate(8_240_691)
    st = os.stat(sparse)
    if st.st_blocks != 0:
        pytest.skip("filesystem does not support sparse files; cannot mimic a placeholder")
    if sys.platform == "darwin":
        assert is_dataless_stub(st) is True
        assert is_materialised(sparse) is False
        assert materialised_size(sparse) is None
    else:
        assert is_dataless_stub(st) is False
        assert is_materialised(sparse) is True
        assert materialised_size(sparse) == 8_240_691


def test_directory_is_not_materialised(tmp_path: Path) -> None:
    assert is_materialised(tmp_path) is False
    assert materialised_size(tmp_path) is None
