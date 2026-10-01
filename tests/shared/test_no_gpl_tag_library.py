"""TAGIO-04: the GPL ``mutagen`` cannot come back into the shipped code path.

[if] mutagen re-enters code, deps or payload [then] this fails, [else stop].

Regression one-liners:
  - if any module under apps/ or scripts/ imports mutagen then broken
  - if pyproject.toml declares mutagen in any dependency list or extra then broken
  - if a mutagen line in the locked export does not fail the payload build then broken
  - if tags / artwork / Serato GEOB need mutagen importable to be read then broken
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    PayloadBuildError,
    _assert_never_ship_absent,
    parse_locked_export,
)
from tests.fixtures import tagged_audio as ta

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.requirement("TAGIO-04")


def _imports_mutagen(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "mutagen" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "mutagen":
            return True
    return False


def test_no_shipped_or_script_module_imports_mutagen() -> None:
    """[if] any apps/ or scripts/ module imports mutagen [then] this fails, [else stop]."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for root in ("apps", "scripts")
        for path in (REPO_ROOT / root).rglob("*.py")
        if "node_modules" not in path.parts and _imports_mutagen(path)
    ]
    assert offenders == []


def test_the_import_scan_detects_a_mutagen_import(tmp_path: Path) -> None:
    """[if] a file does import mutagen [then] the scanner reports it, [else stop]."""
    probe = tmp_path / "probe.py"
    probe.write_text("def f():\n    from mutagen.id3 import ID3\n    return ID3\n")
    assert _imports_mutagen(probe) is True


def test_pyproject_declares_no_mutagen_anywhere() -> None:
    """[if] pyproject lists mutagen as a dependency or extra [then] fail, [else stop]."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    declared = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        declared += extra
    assert [req for req in declared if req.lower().startswith("mutagen")] == []
    assert any(req.startswith("tinytag") for req in project["dependencies"])


def test_a_mutagen_line_in_the_locked_export_fails_the_build() -> None:
    """[if] mutagen enters the locked export [then] the build fails on licence, [else stop]."""
    lock = "numpy==2.0.0\n    # via music-dj-tools\nmutagen==1.48.1\n    # via mediafile\n"
    with pytest.raises(PayloadBuildError) as excinfo:
        _assert_never_ship_absent(parse_locked_export(lock))
    assert "mutagen" in str(excinfo.value)
    assert "GPL" in str(excinfo.value)


@pytest.mark.requires_ffmpeg
def test_tags_artwork_and_geob_read_with_mutagen_unimportable(tmp_path: Path) -> None:
    """[if] mutagen cannot be imported [then] tags, art and GEOB still read, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    ta.add_apic(mp3, b"\xff\xd8\xff\xe0" + b"\x00" * 16, mime="image/jpeg")
    script = f"""
import sys
sys.modules["mutagen"] = None  # any import of it now raises ImportError
from pathlib import Path
from apps.shared import audio_files, id3v2
from apps.adapters.serato import geob
meta = audio_files.read_metadata(Path({str(flac)!r}))
assert meta is not None and meta.artist and meta.genre and meta.bpm, meta
assert audio_files.read_embedded_artwork(Path({str(mp3)!r})) is not None
tag = id3v2.load_or_new(Path({str(mp3)!r}))
tag.set_geob("Serato Overview", b"abc")
id3v2.save(Path({str(mp3)!r}), tag)
assert geob.read_geob_frames(Path({str(mp3)!r})).opaque_frames == {{"Serato Overview": b"abc"}}
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO_ROOT, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
