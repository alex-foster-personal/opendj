"""Guards for STEMS-12730: one production RoFormer root constant."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.lyrics import batch as lyrics_batch
from apps.lyrics import library_verdicts
from apps.lyrics import stems_sync
from apps.stems import artifacts as stem_artifacts
from apps.stems import recompress
from apps.webui import library_assets
from apps.webui.server import stem_artifacts as stem_artifacts_shim

_APPS = Path(__file__).resolve().parents[2] / "apps"
_RUNTIME_PY_PATHS: tuple[Path, ...] = tuple(
    _APPS / rel
    for rel in (
        "stems/artifacts.py",
        "stems/recompress.py",
        "stems/inventory.py",
        "webui/library_assets.py",
        "webui/server/stem_artifacts.py",
        "lyrics/batch.py",
        "lyrics/library_verdicts.py",
        "lyrics/register_stems.py",
        "lyrics/stems_sync.py",
        "cloud/stem_index.py",
        "sync_hub/data_classes_files.py",
    )
)


@pytest.mark.requirement("STEMS-12730")
def test_runtime_tree_does_not_import_roformer_stems_dir() -> None:
    """[if] runtime still names ROFORMER_STEMS_DIR [then] fail, [else stop]."""
    offenders: list[str] = []
    for path in _RUNTIME_PY_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "ROFORMER_STEMS_DIR":
                offenders.append(str(path.relative_to(_APPS.parent)))
    assert offenders == [], offenders


@pytest.mark.requirement("STEMS-12730")
def test_stem_roots_includes_production_roformer_path() -> None:
    """[if] stem_roots omits production dir [then] fail, [else stop]."""
    roots = stem_artifacts.stem_roots()
    assert stem_artifacts.PRODUCTION_ROFORMER_STEMS_DIR in roots


@pytest.mark.requirement("STEMS-12730")
def test_stem_storage_local_mode_lists_production_roformer_root(tmp_path: Path) -> None:
    """[if] stem_storage drops production RoFormer root [then] fail, [else stop]."""
    state = tmp_path / "state"
    storage = library_assets.stem_storage(
        environ={"MDT_LIBRARY_MODE": "local"},
        state_dir=state,
    )
    assert state / stem_artifacts.PRODUCTION_ROFORMER_STEMS_DIR.name in storage.roots


@pytest.mark.requirement("STEMS-12730")
def test_local_stem_root_names_and_recompress_keep_production_dir(tmp_path: Path) -> None:
    """[if] index or recompress drops stems-roformer-spike [then] fail, [else stop]."""
    name = stem_artifacts.PRODUCTION_ROFORMER_STEMS_DIR.name
    assert name in stem_index.LOCAL_STEM_ROOT_NAMES
    data_dir = tmp_path / "data"
    assert name in {p.name for p in recompress.stem_roots_for(data_dir)}


@pytest.mark.requirement("STEMS-12730")
def test_lyrics_modules_resolve_production_root_from_constant(tmp_path: Path) -> None:
    """[if] lyrics hardcodes spike path not the constant [then] fail, [else stop]."""
    data_dir = tmp_path / "data"
    expected = data_dir / "state" / stem_artifacts.PRODUCTION_ROFORMER_STEMS_DIR.name
    assert stems_sync._stems_root(data_dir) == expected
    paths = lyrics_batch.batch_paths_for(data_dir / "state")
    assert paths.stems_root == expected
    demucs, roformer = library_verdicts._bundle_roots(data_dir)
    assert roformer == expected
    assert demucs == data_dir / "state" / "stems"


@pytest.mark.requirement("STEMS-12730")
def test_shim_reexports_production_constant_only() -> None:
    """[if] webui shim still exports ROFORMER_STEMS_DIR [then] fail, [else stop]."""
    assert "PRODUCTION_ROFORMER_STEMS_DIR" in stem_artifacts_shim.__all__
    assert "ROFORMER_STEMS_DIR" not in stem_artifacts_shim.__all__
    assert stem_artifacts_shim.PRODUCTION_ROFORMER_STEMS_DIR is stem_artifacts.PRODUCTION_ROFORMER_STEMS_DIR


@pytest.mark.requirement("STEMS-12730")
def test_production_constant_comment_names_production_not_spike_only() -> None:
    """[if] artifacts still call the tree only a spike [then] fail, [else stop]."""
    source = (Path(__file__).resolve().parents[2] / "apps/stems/artifacts.py").read_text(encoding="utf-8")
    marker = "PRODUCTION_ROFORMER_STEMS_DIR"
    start = source.index(marker)
    snippet = source[start : start + 500]
    assert "Production RoFormer" in snippet
    assert "Where the RoFormer farm lands" not in snippet
