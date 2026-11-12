"""Concurrent manifest reads during ``publish_bundle`` (STEM-58).

[if] a manifest is replaced while readers open it [then] reads stay on gen A or error, [else stop].
"""

from __future__ import annotations

import io
import json
import threading
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.stems.artifacts import StemArtifactError, load_stem_bundle
from apps.stems.bundle_lock import publish_bundle, replace_bundle_dir
from apps.stems.manifest import read_manifest_document

pytestmark = pytest.mark.requirement("STEM-58")

_STABLE_ID = "manifest-race-sid"
_PARTS = ("vocals", "drums", "bass", "other")


def _wav_bytes(*, frames: int = 8) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(b"\x00\x00" * frames * 2)
    return buf.getvalue()


def _manifest_dict(stable_id: str, generation: str) -> dict:
    return {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": generation},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {part: f"{part}.wav" for part in _PARTS},
    }


def _write_bundle(bundle_dir: Path, stable_id: str, generation: str) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for part in _PARTS:
        (bundle_dir / f"{part}.wav").write_bytes(_wav_bytes())
    manifest_text = json.dumps(_manifest_dict(stable_id, generation), separators=(",", ":"))
    (bundle_dir / "manifest.json").write_text(manifest_text + "\n", encoding="utf-8")


def _replaced(entry: Path) -> bool:
    return entry.name == "manifest.json" or entry.stem in _PARTS


def test_a_concurrent_manifest_read_never_sees_the_next_generation_mid_publish(tmp_path: Path) -> None:
    """[if] publish swaps the bundle [then] manifest reads see gen A or error, never gen B alone, [else stop].

    MUTATION TARGET: drop ``readers_held_off`` in ``replace_bundle_dir``.
    """
    data_dir = tmp_path / "data"
    bundle = data_dir / "state" / "stems" / _STABLE_ID
    _write_bundle(bundle, _STABLE_ID, "generation-a")

    swap_reached = threading.Event()
    allow_swap = threading.Event()
    publish_error: list[BaseException] = []

    real_replace = replace_bundle_dir

    def gated_replace(new_dir: Path, dest: Path) -> None:
        swap_reached.set()
        assert allow_swap.wait(timeout=30), "publish swap timed out waiting for reader gate"
        real_replace(new_dir, dest)

    def write_staging(staging: Path) -> None:
        _write_bundle(staging, _STABLE_ID, "generation-b")

    def publish_worker() -> None:
        try:
            with patch("apps.stems.bundle_lock.replace_bundle_dir", gated_replace):
                publish_bundle(bundle, write_staging, _replaced)
        except BaseException as exc:  # noqa: BLE001 - surfaced on the main thread
            publish_error.append(exc)

    worker = threading.Thread(target=publish_worker, name="publish")
    worker.start()
    assert swap_reached.wait(timeout=30), "publish never reached replace_bundle_dir"

    saw: list[str | None] = []
    for _ in range(200):
        try:
            raw = read_manifest_document(data_dir, _STABLE_ID, bundle)
        except StemArtifactError:
            saw.append(None)
            continue
        version = raw.get("model", {}).get("version")
        assert version != "generation-b", "reader saw the staged generation before the swap finished"
        if version == "generation-a":
            saw.append("generation-a")
        else:
            saw.append(version)

    assert any(entry == "generation-a" for entry in saw), "no successful read returned the live generation"
    allow_swap.set()
    worker.join(timeout=60)
    assert not publish_error, publish_error

    live = read_manifest_document(data_dir, _STABLE_ID, bundle)
    assert live["model"]["version"] == "generation-b"
    load_stem_bundle(_STABLE_ID, stems_dir=data_dir / "state" / "stems")


def test_control_publish_without_a_concurrent_reader_lands_generation_b(tmp_path: Path) -> None:
    """[if] publish runs alone [then] the live manifest is generation B and loads, [else stop]."""
    data_dir = tmp_path / "data"
    bundle = data_dir / "state" / "stems" / _STABLE_ID
    _write_bundle(bundle, _STABLE_ID, "generation-a")

    def write_staging(staging: Path) -> None:
        _write_bundle(staging, _STABLE_ID, "generation-b")

    publish_bundle(bundle, write_staging, _replaced)

    live = read_manifest_document(data_dir, _STABLE_ID, bundle)
    assert live["model"]["version"] == "generation-b"
    load_stem_bundle(_STABLE_ID, stems_dir=data_dir / "state" / "stems")
