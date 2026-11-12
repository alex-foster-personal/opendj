"""default_roots must list only live stem stores (issue #11402).

[if] a spent or bench-only stem root is still in default_roots [then] fail, [else stop].
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.stems import inventory as stem_inventory

pytestmark = pytest.mark.requirement("STEMS-12731")


def _roots(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, ...]:
    external = data_dir.parent / "external-stems"
    external.mkdir()
    monkeypatch.setenv(stem_inventory.EXTERNAL_ROOTS_ENV, str(external))
    return stem_inventory.default_roots(data_dir)


def test_default_roots_omits_spent_roformer_verify_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] no reader for stems-roformer-spike-verify [then] default_roots omits it, [else stop]."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    roots = _roots(data_dir, monkeypatch)
    verify = data_dir / "state/stems-roformer-spike-verify"
    assert verify not in roots
    assert not any("stems-roformer-spike-verify" in str(r) for r in roots)


def test_default_roots_omits_demucs_ab_bench_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] stems-demucs-ab is bench-only [then] default_roots omits it, [else stop]."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    roots = _roots(data_dir, monkeypatch)
    demucs_ab = data_dir / "state/stems-demucs-ab"
    assert demucs_ab not in roots
    assert not any("stems-demucs-ab" in str(r) for r in roots)


def test_default_roots_keeps_live_stem_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] live stores exist [then] default_roots lists stems and spike dirs, [else stop]."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    roots = _roots(data_dir, monkeypatch)
    assert data_dir / "state/stems" in roots
    assert data_dir / "state/stems-roformer-spike" in roots


def test_modal_demucs_ab_still_writes_stems_demucs_ab() -> None:
    """[if] modal_demucs_ab DEFAULT_OUT_DIR changes [then] fail, [else stop]."""
    demucs_ab = Path(__file__).resolve().parents[2] / "scripts" / "modal_demucs_ab.py"
    text = demucs_ab.read_text()
    assert 'DEFAULT_OUT_DIR: Path = REPO_ROOT / "data" / "state" / "stems-demucs-ab"' in text


def test_retired_roots_absent_on_disk_not_in_default_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] retired dirs are absent on disk [then] default_roots omits them, [else stop]."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state = data_dir / "state"
    state.mkdir()
    (state / "stems").mkdir()
    for name in ("stems-roformer-spike-verify", "stems-demucs-ab"):
        assert not (state / name).is_dir()
    roots = _roots(data_dir, monkeypatch)
    names = {p.name for p in roots if p.parent == state}
    assert "stems-roformer-spike-verify" not in names
    assert "stems-demucs-ab" not in names
    assert os.sep + "stems-demucs-ab" not in os.sep.join(str(r) for r in roots)
