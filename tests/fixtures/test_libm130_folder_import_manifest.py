"""Manifest-locked folder-import corpus for LIBM-130/131 guards (issue #11330).

[if] a listed file's sha256 does not match the manifest [then] fail, [else stop].
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

import apps.sync.usb.pioneer.fixture_resolver as _resolver
from apps.shared.hashing import sha256_file
from apps.sync.usb.pioneer.fixture_resolver import (
    FixtureContractMismatch,
    FixtureNotAvailable,
    FIXTURES_ROOT,
    copy_verified_manifest_files,
    fixture_manifest_path,
    fixture_path,
    verify_fixture_manifest,
)

pytestmark = [pytest.mark.requirement("LIBM-12650")]

LIBM130_SLUG = "libm130-folder-import"
IMPORT_AUDIO_DIRS = (
    FIXTURES_ROOT / "phase7-dedup",
    FIXTURES_ROOT / "app-acceptance/library",
)


def test_libm130_manifest_pins_committed_audio_bytes() -> None:
    """[if] the libm130 folder-import manifest is present [then] every pin matches on disk, [else stop]."""
    manifest = verify_fixture_manifest(LIBM130_SLUG)
    assert manifest["contract_version"] == 1
    assert len(manifest["files"]) == 14


def test_libm130_manifest_rejects_checksum_mismatch(tmp_path: Path) -> None:
    """[if] a listed file's sha256 does not match the manifest [then] verify fails closed, [else stop]."""
    slug = f"zz-scratch-libm130-digest-{uuid.uuid4().hex[:8]}"
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"canonical bytes")
    good_digest = sha256_file(audio)
    manifest_path = _resolver.FIXTURES_ROOT / f"{slug}.manifest.json"
    rel = f"scratch-libm130/{slug}/track.mp3"
    pinned_dir = _resolver.FIXTURES_ROOT / "scratch-libm130" / slug
    pinned_dir.mkdir(parents=True)
    pinned = pinned_dir / "track.mp3"
    pinned.write_bytes(b"canonical bytes")
    manifest_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "fixture": slug,
                "files": {rel: {"sha256": "sha256:" + "0" * 64, "bytes": pinned.stat().st_size}},
            }
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(FixtureContractMismatch, match="does not match its manifest checksum"):
            verify_fixture_manifest(slug)
        pinned.write_bytes(b"tampered")
        manifest_path.write_text(
            json.dumps(
                {
                    "contract_version": 1,
                    "fixture": slug,
                    "files": {rel: {"sha256": good_digest, "bytes": pinned.stat().st_size}},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(FixtureContractMismatch, match="does not match its manifest checksum"):
            verify_fixture_manifest(slug)
    finally:
        manifest_path.unlink(missing_ok=True)
        shutil.rmtree(pinned_dir.parent, ignore_errors=True)


def test_libm130_manifest_sits_outside_folder_import_scan_roots() -> None:
    """[if] the pin manifest sits inside a folder import would scan [then] fail, [else stop]."""
    manifest = fixture_manifest_path(LIBM130_SLUG).resolve()
    for scan_root in IMPORT_AUDIO_DIRS:
        scan_root = scan_root.resolve()
        assert manifest != scan_root / manifest.name
        assert scan_root not in manifest.parents


def test_libm130_manifest_missing_host_file_fails_closed() -> None:
    """[if] the canonical fixture host is missing [then] verify fails closed, [else stop]."""
    slug = f"zz-scratch-libm130-missing-{uuid.uuid4().hex[:8]}"
    rel = f"scratch-libm130/{slug}/absent.mp3"
    manifest_path = _resolver.FIXTURES_ROOT / f"{slug}.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "fixture": slug,
                "files": {rel: {"sha256": "sha256:" + "a" * 64, "bytes": 1}},
            }
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(FixtureContractMismatch, match="pins missing file"):
            verify_fixture_manifest(slug)
    finally:
        manifest_path.unlink(missing_ok=True)


def test_libm130_copy_never_serves_unverified_bytes(tmp_path: Path) -> None:
    """[if] a test would copy pinned audio before checksum verification [then] fail, [else stop]."""
    music = tmp_path / "music"
    copied = copy_verified_manifest_files(
        LIBM130_SLUG,
        music,
        prefixes=("app-acceptance/library/",),
        flatten=True,
    )
    assert len(copied) == 7
    for path in copied:
        source = FIXTURES_ROOT / "app-acceptance/library" / path.name
        assert sha256_file(path) == sha256_file(source)


@pytest.fixture
def stub_fixtures_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tests" / "fixtures"
    root.mkdir(parents=True)
    monkeypatch.setattr(_resolver, "FIXTURES_ROOT", root)
    return root


def test_libm130_extern_fixture_host_missing_raises(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] fixture_path needs an extern host that is not mounted [then] FixtureNotAvailable, [else stop]."""
    missing_host = tmp_path / "missing-host"
    monkeypatch.setenv("MUX_FIXTURE_HOST", str(missing_host))
    name = "libm130-extern-corpus"
    (stub_fixtures_root / f"{name}.extern").write_text(f"{name}\n", encoding="utf-8")
    with pytest.raises(FixtureNotAvailable, match="not mounted"):
        fixture_path(name)
