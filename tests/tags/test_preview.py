"""Tests for ``apps.tags.preview``."""
from __future__ import annotations

import csv
import hashlib
import shutil
from pathlib import Path

import pytest

from apps.shared.tag_writer import TagRead
from apps.tags.preview import run_preview

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.requirement("META-03")
def test_preview_shape(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)

    out = tmp_path / "preview.csv"
    run_preview(paths_arg=[dst], out=out)
    assert out.exists()
    with open(out, encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
    assert header == [
        "path",
        "field",
        "current_file_value",
        "unified_value",
        "source",
        "confidence",
        "would_change_file",
    ]


@pytest.mark.requirement("META-03")
def test_preview_does_not_mutate_file(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    before = _sha(dst)

    run_preview(paths_arg=[dst], out=tmp_path / "preview.csv")
    assert _sha(dst) == before


@pytest.mark.requirement("META-03")
def test_preview_idempotent(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)

    out1 = tmp_path / "p1.csv"
    out2 = tmp_path / "p2.csv"
    run_preview(paths_arg=[dst], out=out1)
    run_preview(paths_arg=[dst], out=out2)
    assert out1.read_text() == out2.read_text()


@pytest.mark.requirement("META-03")
def test_preview_uses_rb_source(tmp_path: Path) -> None:
    src = FIXTURE_ROOT / "src-320.mp3"
    dst = tmp_path / "Artist - Title.mp3"
    shutil.copy2(src, dst)
    out = tmp_path / "p.csv"
    run_preview(
        paths_arg=[dst],
        out=out,
        fetch_rb=lambda p: TagRead(genre="Techno", bpm=128.0),
    )
    with open(out, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    sources = {r["field"]: r["source"] for r in rows}
    # RB supplied genre + bpm; unify should pick RB for both.
    assert sources.get("genre") == "rb"
    assert sources.get("bpm") == "rb"
