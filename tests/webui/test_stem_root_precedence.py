"""H7: a corrupt stem bundle must raise, never fall through to a lesser store.

A stable_id can legitimately have a bundle in both the Demucs 4-part store and
the RoFormer 2-part store. They are searched in precedence order and the FIRST
directory that EXISTS wins outright: a bundle that exists but fails validation
raises rather than quietly degrading the deck from four controls to two.

Mini-PRD
========
* [if] a track has both a Demucs and a RoFormer bundle [then] the API serves
  the 4-part one [else -] the deck silently loses DRUMS and BASS.
* [if] the Demucs bundle directory exists but its manifest is invalid [then]
  the request errors [else -] a working-but-wrong 2-part deck ships.
* [if] a third model store is added [then] only the roots tuple changes [else -]
  "extendable if we add the live-band one" is false.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server import stem_artifacts
from apps.webui.server.routes.stems import router

STABLE_ID = "track-both-stores"


def _write_wav(path: Path, *, frames: int = 12, sample_rate: int = 44_100, channels: int = 2) -> None:
    """A tiny but real PCM WAV so alignment validation runs for real."""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames * channels)


def _manifest(stable_id: str, layout: str) -> dict:
    """A real manifest per store: v1 for the Demucs four, v3 for RoFormer.

    v1 and v2 hard-code the Demucs four parts; v3 exists precisely so a bundle
    can DECLARE a two-part split, which is what the RoFormer farm writes.
    """
    parts = stem_artifacts.STEM_LAYOUTS[layout]
    if layout == "demucs4":
        return {
            "schema_version": 1,
            "stable_id": stable_id,
            "layout": layout,
            "model": {"name": "htdemucs", "version": "4.0.1"},
            "source": {"path": "C:/Music/Example.wav", "sha256": "a" * 64},
            "files": {part: f"{part}.wav" for part in parts},
        }
    return {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": layout,
        "model": {"name": "mel-band-roformer", "version": "1.0.0"},
        "source": {"path": "C:/Music/Example.wav", "sha256": "a" * 64},
        "audio": {"sample_rate": 44_100, "frame_count": 12, "channels": 2},
        "files": {part: f"{part}.wav" for part in parts},
    }


def _bundle(root: Path, layout: str, *, stable_id: str = STABLE_ID, manifest_text: str | None = None) -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in stem_artifacts.STEM_LAYOUTS[layout]:
        _write_wav(bundle / f"{part}.wav")
    (bundle / "manifest.json").write_text(
        manifest_text if manifest_text is not None else json.dumps(_manifest(stable_id, layout)),
        encoding="utf-8",
    )
    return bundle


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A Demucs store and a RoFormer store, both real directories on disk."""
    demucs = tmp_path / "stems"
    roformer = tmp_path / "stems-roformer-spike"
    demucs.mkdir()
    roformer.mkdir()
    monkeypatch.setattr(stem_artifacts, "ROFORMER_STEMS_DIR", roformer)
    return demucs, roformer


def _client(stems_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


# --------------------------------------------------------------- precedence


def test_roots_put_the_richer_demucs_store_first(stores: tuple[Path, Path]) -> None:
    """Demucs drives more controls, so where a track has both it wins."""
    demucs, roformer = stores
    assert stem_artifacts.stem_roots(demucs) == (demucs, roformer)


def test_the_roformer_store_is_never_searched_twice(stores: tuple[Path, Path]) -> None:
    """Asking for the RoFormer root directly yields one root, not a duplicate."""
    _, roformer = stores
    assert stem_artifacts.stem_roots(roformer) == (roformer,)


def test_a_track_in_both_stores_loads_as_four_parts(stores: tuple[Path, Path]) -> None:
    """The deck must keep DRUMS and BASS when a Demucs bundle exists."""
    demucs, roformer = stores
    _bundle(demucs, "demucs4")
    _bundle(roformer, "roformer2")

    bundle = stem_artifacts.load_stem_bundle(STABLE_ID, stems_dir=demucs)

    assert bundle.layout == "demucs4"
    assert tuple(bundle.files) == stem_artifacts.STEM_PARTS
    assert "drums" in bundle.files, "a 4-part bundle exists; the deck must not lose DRUMS"


def test_the_api_serves_the_four_part_bundle_when_both_exist(stores: tuple[Path, Path]) -> None:
    """Same precedence, proven through the mounted router."""
    demucs, roformer = stores
    _bundle(demucs, "demucs4")
    _bundle(roformer, "roformer2")

    with _client(demucs) as client:
        response = client.get(f"/api/v1/tracks/{STABLE_ID}/stems")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert sorted(payload["parts"]) == sorted(stem_artifacts.STEM_PARTS), payload

        drums = client.get(f"/api/v1/tracks/{STABLE_ID}/stems/drums")
        assert drums.status_code == 200, drums.text


def test_a_roformer_only_track_still_loads_as_a_first_class_two_part_bundle(
    stores: tuple[Path, Path],
) -> None:
    """The fallback store is a different split, not a broken Demucs bundle."""
    demucs, roformer = stores
    _bundle(roformer, "roformer2")

    bundle = stem_artifacts.load_stem_bundle(STABLE_ID, stems_dir=demucs)

    assert bundle.layout == "roformer2"
    assert tuple(bundle.files) == stem_artifacts.ROFORMER_PARTS
    assert "drums" not in bundle.files


# ---------------------------------------------------- raise, never degrade


def test_a_corrupt_demucs_bundle_raises_instead_of_degrading_to_two_parts(
    stores: tuple[Path, Path],
) -> None:
    """The exact silent-degradation failure this precedence rule exists to stop."""
    demucs, roformer = stores
    _bundle(demucs, "demucs4", manifest_text="{ this is not json")
    _bundle(roformer, "roformer2")

    with pytest.raises(stem_artifacts.StemArtifactError):
        stem_artifacts.load_stem_bundle(STABLE_ID, stems_dir=demucs)


def test_a_demucs_bundle_missing_a_part_raises_rather_than_falling_through(
    stores: tuple[Path, Path],
) -> None:
    """A three-part manifest is corruption, not a licence to use the 2-part store."""
    demucs, roformer = stores
    broken = _manifest(STABLE_ID, "demucs4")
    del broken["files"]["bass"]
    _bundle(demucs, "demucs4", manifest_text=json.dumps(broken))
    _bundle(roformer, "roformer2")

    with pytest.raises(stem_artifacts.StemArtifactError):
        stem_artifacts.load_stem_bundle(STABLE_ID, stems_dir=demucs)


def test_the_api_errors_on_a_corrupt_demucs_bundle_shadowing_a_valid_roformer_one(
    stores: tuple[Path, Path],
) -> None:
    """A working-but-wrong deck is worse than a loud failure."""
    demucs, roformer = stores
    _bundle(demucs, "demucs4", manifest_text="{ this is not json")
    _bundle(roformer, "roformer2")

    with _client(demucs) as client:
        response = client.get(f"/api/v1/tracks/{STABLE_ID}/stems")

    assert response.status_code != 200, (
        "the corrupt Demucs bundle fell through to the RoFormer store and the "
        f"API published a 2-part deck: {response.text}"
    )
    assert response.status_code == 422, response.text


def test_no_bundle_in_any_store_names_every_root_it_searched(stores: tuple[Path, Path]) -> None:
    """A miss must be distinguishable from corruption, and say where it looked."""
    demucs, roformer = stores

    with pytest.raises(stem_artifacts.StemBundleNotFoundError) as excinfo:
        stem_artifacts.load_stem_bundle(STABLE_ID, stems_dir=demucs)

    message = str(excinfo.value)
    assert str(demucs) in message
    assert str(roformer) in message


# ---------------------------------------------------------- extensibility


def test_adding_a_third_model_store_needs_only_the_roots_tuple(
    stores: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live-band model becomes reachable by adding a root, not a schema change."""
    demucs, roformer = stores
    third = tmp_path / "stems-live-band"
    third.mkdir()
    _bundle(third, "roformer2")

    monkeypatch.setattr(
        stem_artifacts, "stem_roots", lambda primary=None: (demucs, roformer, third)
    )

    bundle = stem_artifacts.load_stem_bundle(STABLE_ID, stems_dir=demucs)

    assert bundle.layout == "roformer2"
    assert tuple(bundle.files) == stem_artifacts.ROFORMER_PARTS
