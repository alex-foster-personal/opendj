"""The local stem publisher refuses a stem-set that breaks STEM-8231 (#9664).

[if] a staged set breaks STEM-8231 [then] publish raises StemQualityError and leaves no bundle, [else stop].

Both publishers (the app's ``bundle_publish.publish_stems_local`` and the
farm's ``_publish_stems_local``) measure every staged part from its bytes just
before the swap. Real parts from the real encoders; no mocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.stems import bundle_publish
from apps.stems.stem_size_policy import StemQualityError, stem_set_size_ratio
from tests.stems.stem_set_support import (
    PARTS,
    compliant_mp3_parts,
    lossless_parts,
    mp3_parts_at,
    mp3_source,
    publish_result,
)

pytestmark = [pytest.mark.requirement("STEM-8231"), pytest.mark.requires_audio_stack]

SID = "c" * 40
PRESET = bundle_publish.PRESETS["htdemucs-ov0.25"]


def _farm_publisher() -> Any:
    from tests.stems.test_bundle_publish_mirror import _farm_function, _farm_namespace, _farm_tree

    tree = _farm_tree()
    namespace = _farm_namespace(tree)
    _farm_function(tree, "_stem_manifest", namespace)
    return _farm_function(tree, "_publish_stems_local", namespace)


PUBLISHERS = {"mirror": lambda: bundle_publish.publish_stems_local, "farm": _farm_publisher}


@pytest.fixture(params=sorted(PUBLISHERS))
def publish(request: pytest.FixtureRequest) -> Any:
    return PUBLISHERS[request.param]()


def _bundle(data: Path) -> Path:
    return data / "state" / "stems" / SID


def _staging_entries(data: Path) -> list[Path]:
    from apps.stems.bundle_lock import PUBLISH_DIR_NAME, beside_stem_root

    root = beside_stem_root(data / "state" / "stems") / PUBLISH_DIR_NAME
    return sorted(root.glob(f"{SID}*")) if root.exists() else []


def _snapshot(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir()) if p.is_file()}


def test_flac_parts_for_an_mp3_source_are_refused_and_leave_no_bundle(publish: Any, tmp_path: Path) -> None:
    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    data = tmp_path / "data"
    with pytest.raises(StemQualityError, match="flac"):
        publish(
            SID,
            source,
            publish_result(lossless_parts("flac", seed=1), "flac"),
            PRESET,
            data,
            requested_codec="by-source",
        )
    assert not _bundle(data).exists()
    assert _staging_entries(data) == []


@pytest.mark.parametrize("codec", ["flac", "wav"])
def test_lossless_parts_publish_only_when_asked_for_by_name(publish: Any, tmp_path: Path, codec: str) -> None:
    import json

    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    result = publish_result(lossless_parts(codec, seed=1), codec)
    refused = tmp_path / "refused"
    with pytest.raises(StemQualityError):
        publish(SID, source, result, PRESET, refused, requested_codec="by-source")
    assert not _bundle(refused).exists()

    publish(SID, source, result, PRESET, tmp_path / "asked", requested_codec=codec)
    bundle = _bundle(tmp_path / "asked")
    assert sorted(p.name for p in bundle.glob(f"*.{codec}")) == sorted(f"{p}.{codec}" for p in PARTS)
    assert json.loads((bundle / "manifest.json").read_text())["codec"] == codec


def test_a_compliant_mp3_set_still_publishes(publish: Any, tmp_path: Path) -> None:
    from apps.stems.artifacts import load_stem_bundle

    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    parts = compliant_mp3_parts(source, seed=1, tmp=tmp_path / "parts")
    data = tmp_path / "data"
    publish(SID, source, publish_result(parts, "mp3"), PRESET, data, requested_codec="by-source")
    for part in PARTS:
        assert (_bundle(data) / f"{part}.mp3").read_bytes() == parts[part]
    assert load_stem_bundle(SID, stems_dir=data / "state" / "stems").media_type == "audio/mpeg"


def test_a_floor_forced_overage_still_publishes(publish: Any, tmp_path: Path) -> None:
    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=128)
    parts = compliant_mp3_parts(source, seed=1, tmp=tmp_path / "parts")
    ratio = stem_set_size_ratio(source_bytes=source.stat().st_size, part_sizes={p: len(b) for p, b in parts.items()})
    assert ratio > 1.0, "the set must really be over the source, or this test passes vacuously"
    publish(SID, source, publish_result(parts, "mp3"), PRESET, tmp_path / "data", requested_codec="by-source")
    assert (_bundle(tmp_path / "data") / "manifest.json").is_file()


@pytest.mark.parametrize(("low_part", "kbps", "floor"), [("vocals", 48, 64), ("drums", 64, 96)])
def test_a_part_under_its_floor_is_refused(publish: Any, tmp_path: Path, low_part: str, kbps: int, floor: int) -> None:
    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    rates = {"vocals": 80, "other": 80, "bass": 96, "drums": 96} | {low_part: kbps}
    parts = mp3_parts_at(rates, seed=1, tmp=tmp_path / "parts")
    with pytest.raises(StemQualityError, match=rf"{low_part} measures {kbps} kbps, under its {floor} kbps floor"):
        publish(SID, source, publish_result(parts, "mp3"), PRESET, tmp_path / "data", requested_codec="by-source")
    assert not _bundle(tmp_path / "data").exists()


def test_a_set_over_the_source_above_its_floors_is_refused(publish: Any, tmp_path: Path) -> None:
    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    parts = mp3_parts_at(dict.fromkeys(PARTS, 192), seed=1, tmp=tmp_path / "parts")
    with pytest.raises(StemQualityError, match="budget"):
        publish(SID, source, publish_result(parts, "mp3"), PRESET, tmp_path / "data", requested_codec="by-source")
    assert not _bundle(tmp_path / "data").exists()


def test_a_refused_republish_keeps_the_previous_bundle(publish: Any, tmp_path: Path) -> None:
    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    data = tmp_path / "data"
    good = compliant_mp3_parts(source, seed=1, tmp=tmp_path / "parts")
    publish(SID, source, publish_result(good, "mp3"), PRESET, data, requested_codec="by-source")
    before = _snapshot(_bundle(data))
    with pytest.raises(StemQualityError):
        publish(
            SID,
            source,
            publish_result(lossless_parts("flac", seed=2), "flac"),
            PRESET,
            data,
            requested_codec="by-source",
        )
    assert _snapshot(_bundle(data)) == before


def test_parts_whose_bytes_are_not_their_container_are_refused(publish: Any, tmp_path: Path) -> None:
    source = mp3_source(tmp_path / "music" / "a.mp3", kbps=320)
    result = publish_result(lossless_parts("flac", seed=1), "mp3")  # .mp3 names, FLAC bytes
    with pytest.raises(StemQualityError):
        publish(SID, source, result, PRESET, tmp_path / "data", requested_codec="by-source")
    assert not _bundle(tmp_path / "data").exists()
