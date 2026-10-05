"""apps/stems/bundle_publish.py stays a faithful mirror of the Modal farm.

[if] the mirror drifts from the farm's bundle writer [then] fail, [else stop].

The relay worker writes bundles through ``apps.stems.bundle_publish`` so the
installed app never imports modal (issue #3421). The farm keeps its own
copies because Modal imports that file in a container with no ``apps``. A
mirror rots silently, so this reads the farm's SOURCE (no import, no modal,
same approach and reasoning as ``tests/test_stem_tiers.py``) and fails on
drift:

  - [if] a preset differs in tag, model, overlap, shifts or rung [then] fail
  - [if] a schema constant differs [then] fail
  - [if] the two manifest builders or bundle writers produce different bytes
    for the same input [then] fail

-Claude
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

import pytest

from apps.stems import bundle_publish

FARM = Path(__file__).resolve().parents[2] / "scripts" / "modal_vocal_farm.py"
MIRRORED_LITERALS = ("BAKED_MODELS", "DEMUCS_VERSION", "STEM_PARTS", "STEM_BUNDLE_SCHEMA")

pytestmark = pytest.mark.requirement("STEM-52")


def _farm_tree() -> ast.Module:
    return ast.parse(FARM.read_text(encoding="utf-8"))


def _assigned(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return node.value
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    raise AssertionError(f"{name} not found in {FARM}")


def _farm_function(tree: ast.Module, name: str, namespace: dict[str, Any]) -> Any:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(FARM), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"def {name} not found in {FARM}")


def _farm_namespace(tree: ast.Module) -> dict[str, Any]:
    namespace: dict[str, Any] = {"json": json, "time": time, "Path": Path, "Any": Any}
    for name in MIRRORED_LITERALS:
        namespace[name] = ast.literal_eval(_assigned(tree, name))
    return namespace


def test_presets_match_the_farm() -> None:
    value = _assigned(_farm_tree(), "PRESETS")
    assert isinstance(value, ast.Dict)
    farm = {
        ast.literal_eval(key): tuple(ast.literal_eval(arg) for arg in call.args)
        for key, call in zip(value.keys, value.values, strict=True)
        if key is not None and isinstance(call, ast.Call)
    }
    mirror = {
        tag: (p.tag, p.model, p.overlap, p.shifts, p.rung)
        for tag, p in bundle_publish.PRESETS.items()
    }
    assert farm, "parsed no presets from the farm; the probe is blind"
    assert mirror == farm


@pytest.mark.parametrize("name", MIRRORED_LITERALS)
def test_constants_match_the_farm(name: str) -> None:
    assert getattr(bundle_publish, name) == ast.literal_eval(
        _assigned(_farm_tree(), name)
    )


def _result() -> dict[str, Any]:
    return {
        "source_sha256": "0" * 64,
        "stem_ext": "flac",
        "audio": {"sample_rate": 44100, "frame_count": 88200, "channels": 2},
        "stems": {part: f"{part}-bytes".encode() for part in bundle_publish.STEM_PARTS},
    }


def test_manifest_matches_the_farm() -> None:
    tree = _farm_tree()
    farm_manifest = _farm_function(tree, "_stem_manifest", _farm_namespace(tree))
    stamp = bundle_publish.PRESETS["hdemucs_mmi-ov0.25"].stamp()
    args = ("sid-1", "/music/a.mp3", _result(), stamp)
    assert bundle_publish.stem_manifest(*args) == farm_manifest(*args)


def test_bundle_bytes_match_the_farm(tmp_path: Path) -> None:
    tree = _farm_tree()
    namespace = _farm_namespace(tree)
    _farm_function(tree, "_stem_manifest", namespace)
    farm_publish = _farm_function(tree, "_publish_stems_local", namespace)
    preset = bundle_publish.PRESETS["htdemucs-ov0.25"]
    audio = Path("/music/a.mp3")

    farm_written, _ = farm_publish("sid-1", audio, _result(), preset, tmp_path / "farm")
    ours_written, _ = bundle_publish.publish_stems_local(
        "sid-1", audio, _result(), preset, tmp_path / "ours"
    )
    assert ours_written == farm_written > 0
    farm_bundle = tmp_path / "farm" / "state" / "stems" / "sid-1"
    ours_bundle = tmp_path / "ours" / "state" / "stems" / "sid-1"
    names = sorted(p.name for p in farm_bundle.iterdir())
    assert names == sorted(p.name for p in ours_bundle.iterdir())
    for name in names:
        assert (ours_bundle / name).read_bytes() == (farm_bundle / name).read_bytes()


def test_negative_control_a_drifted_mirror_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checks can say no: one changed rung, or one changed schema, fails."""
    tag = "htdemucs-ov0.25"
    original = bundle_publish.PRESETS[tag]
    drifted = dict(bundle_publish.PRESETS)
    drifted[tag] = bundle_publish.Preset(
        original.tag, original.model, original.overlap, original.shifts, original.rung + 1
    )
    monkeypatch.setattr(bundle_publish, "PRESETS", drifted)
    with pytest.raises(AssertionError):
        test_presets_match_the_farm()
    monkeypatch.setattr(bundle_publish, "STEM_BUNDLE_SCHEMA", 99)
    with pytest.raises(AssertionError):
        test_manifest_matches_the_farm()
