"""Tests for ``apps.shared.tag_writer`` (Phase 7 Plan 02)."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from apps.shared.tag_writer import (
    POPM_BUCKETS,
    TagWriteRemoved,
    UnifiedTags,
    UnsupportedContainer,
    read_tags,
    write_tags,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    src = FIXTURE_ROOT / name
    dst = tmp_path / name
    shutil.copy2(src, dst)
    return dst


@pytest.mark.requirement("META-01")
def test_popm_bucket_mapping_pinned() -> None:
    """Locked per the Rekordbox + Windows conventions."""
    assert POPM_BUCKETS == {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}


@pytest.mark.requirement("META-01")
def test_unsupported_aiff_raises(tmp_path: Path) -> None:
    p = tmp_path / "foo.aiff"
    p.write_bytes(b"fake")
    with pytest.raises(UnsupportedContainer):
        read_tags(p)
    with pytest.raises(TagWriteRemoved):
        write_tags(p, UnifiedTags(title="x"), dry_run=True)


@pytest.mark.requirement("META-01")
@pytest.mark.parametrize("name", ["src-320.mp3", "src.m4a", "src.flac"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_write_refuses_and_leaves_bytes_unchanged(
    tmp_path: Path, name: str, dry_run: bool
) -> None:
    dst = _copy_fixture(tmp_path, name)
    before = _sha(dst)
    with pytest.raises(TagWriteRemoved):
        write_tags(
            dst,
            UnifiedTags(title="Unified", artist="X", rating=3, bpm=128.0, energy=7),
            dry_run=dry_run,
        )
    assert _sha(dst) == before
