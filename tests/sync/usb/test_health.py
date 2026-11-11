"""Tests for apps.sync.usb.health (USB-12008 export health rules v0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.health import (
    V0_DECK_RULES,
    evaluate_volume_health,
    scan_mount,
)


@pytest.fixture
def rbx_fat32_stick(tmp_path: Path) -> Path:
    root = tmp_path / "GIG-A"
    root.mkdir()
    pioneer = root / "PIONEER" / "rekordbox"
    pioneer.mkdir(parents=True)
    (pioneer / "export.pdb").write_bytes(b"pdb")
    (pioneer / "exportLibrary.db").write_bytes(b"one")
    (root / "PIONEER" / "USBANLZ").mkdir(parents=True)
    (root / "PIONEER" / "USBANLZ" / "Artist - Good Track.mp3").write_bytes(b"mp3")
    return root


@pytest.mark.requirement("USB-12008")
def test_mounted_stick_returns_pass_fail_per_deck_class(rbx_fat32_stick: Path) -> None:
    """[if] health runs on a mount [then] each v0 deck class is pass/fail, [else stop]."""
    out = evaluate_volume_health(volume_id="vol:TEST", mount_root=rbx_fat32_stick, filesystem="fat32")
    assert out.volume_id == "vol:TEST"
    assert len(out.deck_classes) == len(V0_DECK_RULES)
    assert {c.deck_class for c in out.deck_classes} == {r.deck_class for r in V0_DECK_RULES}
    nxs2 = next(c for c in out.deck_classes if c.deck_class == "cdj_nxs2")
    assert nxs2.ok is True


@pytest.mark.requirement("USB-12008")
def test_exfat_fails_nxs2_with_format_named(tmp_path: Path) -> None:
    """[if] exFAT is on the stick [then] cdj_nxs2 fails naming format:exfat, [else stop]."""
    root = tmp_path / "STICK"
    pioneer = root / "PIONEER" / "rekordbox"
    pioneer.mkdir(parents=True)
    (pioneer / "export.pdb").write_bytes(b"pdb")
    out = evaluate_volume_health(volume_id="vol:EXFAT", mount_root=root, filesystem="exfat")
    nxs2 = next(c for c in out.deck_classes if c.deck_class == "cdj_nxs2")
    assert nxs2.ok is False
    assert any(f.startswith("format:exfat") for f in nxs2.failures)


@pytest.mark.requirement("USB-12008")
def test_missing_onelibrary_fails_cdj_3000x_with_path_named(tmp_path: Path) -> None:
    """[if] exportLibrary.db is absent [then] cdj_3000x fails naming that path, [else stop]."""
    root = tmp_path / "STICK"
    pioneer = root / "PIONEER" / "rekordbox"
    pioneer.mkdir(parents=True)
    (pioneer / "export.pdb").write_bytes(b"pdb")
    out = evaluate_volume_health(volume_id="vol:NO-ONE", mount_root=root, filesystem="fat32")
    modern = next(c for c in out.deck_classes if c.deck_class == "cdj_3000x")
    assert modern.ok is False
    assert "path:PIONEER/rekordbox/exportLibrary.db" in modern.failures


@pytest.mark.requirement("USB-12008")
def test_two_sticks_get_distinct_reports_keyed_by_volume_id(tmp_path: Path) -> None:
    """[if] two sticks are scanned [then] each report carries its own volume_id, [else stop]."""
    a = tmp_path / "A"
    b = tmp_path / "B"
    for root in (a, b):
        pioneer = root / "PIONEER" / "rekordbox"
        pioneer.mkdir(parents=True)
        (pioneer / "export.pdb").write_bytes(b"pdb")
    only_b = tmp_path / "B" / "PIONEER" / "rekordbox" / "exportLibrary.db"
    only_b.write_bytes(b"one")
    out_a = evaluate_volume_health(volume_id="vol:AAAA", mount_root=a, filesystem="fat32")
    out_b = evaluate_volume_health(volume_id="vol:BBBB", mount_root=b, filesystem="fat32")
    assert out_a.volume_id != out_b.volume_id
    a_3000x = next(c for c in out_a.deck_classes if c.deck_class == "cdj_3000x")
    b_3000x = next(c for c in out_b.deck_classes if c.deck_class == "cdj_3000x")
    assert a_3000x.ok is False
    assert b_3000x.ok is True


def test_forbidden_path_segment_is_named(tmp_path: Path) -> None:
    """[if] a path violates v0 limits [then] failures name the path."""
    root = tmp_path / "STICK"
    pioneer = root / "PIONEER" / "rekordbox"
    pioneer.mkdir(parents=True)
    (pioneer / "export.pdb").write_bytes(b"pdb")
    (root / "bad|name.mp3").write_bytes(b"x")
    scan = scan_mount(root, filesystem="fat32")
    assert any(item.startswith("path:") and "forbidden" in item for item in scan.path_violations)
