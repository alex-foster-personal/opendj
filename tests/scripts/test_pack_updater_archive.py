"""scripts/pack_updater_archive.sh writes what tauri-plugin-updater can install."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pack_updater_archive.sh"


def _fake_app(tmp_path: Path) -> Path:
    app = tmp_path / "Open DJ.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "opendj-desktop").write_bytes(b"\x00binary")
    (app / "Contents" / "Info.plist").write_text("<plist/>", encoding="utf-8")
    return app


def test_archive_is_gzip_with_the_app_at_the_top_level(tmp_path: Path) -> None:
    """if the archive is not a gzip tarball rooted at the .app then the updater refuses it."""
    app = _fake_app(tmp_path)
    out = tmp_path / "Open DJ.app.tar.gz"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(app), str(out)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert out.read_bytes()[:2] == b"\x1f\x8b"
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert names[0] == "Open DJ.app"
    assert "Open DJ.app/Contents/MacOS/opendj-desktop" in names
    assert not [n for n in names if "/._" in n or n.startswith("._")], names


def test_packer_refuses_a_missing_app_or_a_wrong_name(tmp_path: Path) -> None:
    """if the input is not an app bundle, or the name is not .app.tar.gz, then exit nonzero."""
    app = _fake_app(tmp_path)
    missing = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "nope.app"), str(tmp_path / "x.app.tar.gz")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    wrong = subprocess.run(
        ["bash", str(SCRIPT), str(app), str(tmp_path / "x.zip")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong.returncode == 2


@pytest.mark.skipif(not Path("/usr/bin/ditto").exists(), reason="ditto is macOS only")
def test_a_ditto_zip_under_the_tar_gz_name_is_what_the_guard_rejects(tmp_path: Path) -> None:
    """control: the old recipe's output fails the gzip magic check, so the guard can fire."""
    app = _fake_app(tmp_path)
    zipped = tmp_path / "Open DJ.app.tar.gz"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(zipped)], check=True)
    assert zipped.read_bytes()[:2] == b"PK"
