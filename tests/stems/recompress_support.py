"""Shared fixtures for the stem recompression tests (STEM-58).

Real audio: FLAC bundles written with soundfile and real MP3 sources, so the
tests in ``test_recompress.py`` and ``test_recompress_swap.py`` run the pass's
own decode, encode and swap code against bundles of the shape the farm writes.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:
    import numpy as np
    import soundfile as sf
except ImportError:
    # The test modules mark themselves requires_audio_stack, which
    # tests/conftest.py turns into an UNAVAILABLE report; never a skip here.
    np = sf = None  # type: ignore[assignment]

from apps.cloud.stem_index import MANIFEST_FILENAME
from apps.stems.stem_encode import encode_mp3

RATE = 44100

SECONDS = 3.0

FRAMES = int(RATE * SECONDS)

PARTS = ("vocals", "drums", "bass", "other")


def _noise(seed: int) -> Any:
    rng = np.random.default_rng(seed)
    return (0.3 * rng.uniform(-1.0, 1.0, size=(FRAMES, 2))).astype(np.float32)


def _data_dir(tmp_path: Path, *, jobs: Sequence[tuple[str, str]] | None = ()) -> Path:
    data = tmp_path / "data"
    (data / "state" / "stems").mkdir(parents=True)
    if jobs is not None:
        db = sqlite3.connect(data / "state" / "jobs.db")
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT, status TEXT)")
        db.executemany(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            [(f"job-{i}", kind, status) for i, (kind, status) in enumerate(jobs)],
        )
        db.commit()
        db.close()
    return data


def _source(tmp_path: Path, name: str, *, kbps: int | None = 192) -> Path:
    """The track's own audio: a real MP3 at ``kbps``, or a WAV when None."""
    mix = sum(_noise(i) for i in range(4)) / 4
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    if kbps is None:
        path = music / f"{name}.wav"
        sf.write(path, mix, RATE)
        return path
    path = music / f"{name}.mp3"
    encode_mp3(mix, RATE, path, cbr_kbps=kbps)
    return path


def _flac_bundle(data: Path, stable_id: str, source: Path) -> Path:
    """A farm schema v2 FLAC bundle, the shape the Modal/relay routes write."""
    bundle = data / "state" / "stems" / stable_id
    bundle.mkdir()
    for i, part in enumerate(PARTS):
        sf.write(bundle / f"{part}.flac", _noise(i), RATE, format="FLAC", subtype="PCM_16")
    manifest = {
        "schema_version": 2,
        "stable_id": stable_id,
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {"path": str(source), "sha256": "0" * 64},
        "files": {part: f"{part}.flac" for part in PARTS},
        "preset": {"tag": "hdemucs_mmi-ov0.25"},
        "audio": {"sample_rate": RATE, "frame_count": FRAMES, "channels": 2},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def _snapshot(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir())}


def _stale_flac_index_entry(_stable_id: str) -> dict[str, str]:
    """Index row naming FLAC parts and a pre-conversion manifest digest."""
    flac = "b" * 64
    return {
        MANIFEST_FILENAME: "a" * 64,
        **{f"{part}.flac": flac for part in PARTS},
    }


def _fresh_parts(tmp_path: Path, seed: int) -> Path:
    """Real FLAC parts a new separation would publish (different audio)."""
    out = tmp_path / f"fresh-{seed}"
    out.mkdir()
    for i, part in enumerate(PARTS):
        sf.write(out / f"{part}.flac", _noise(100 * seed + i), RATE, format="FLAC", subtype="PCM_16")
    return out


def _publish_result(parts: Path) -> dict[str, Any]:
    return {
        "stems": {p: (parts / f"{p}.flac").read_bytes() for p in PARTS},
        "stem_ext": "flac",
        "stem_codec": "flac",  # read by the schema v3 manifest (STEM-75); ignored by v2
        "size_policy": None,
        "source_sha256": "1" * 64,
        "audio": {"sample_rate": RATE, "frame_count": FRAMES, "channels": 2},
    }


def _the_two_flac_publishers() -> dict[str, Any]:
    from apps.stems import bundle_publish
    from tests.stems.test_bundle_publish_mirror import _farm_function, _farm_namespace, _farm_tree

    tree = _farm_tree()
    namespace = _farm_namespace(tree)
    _farm_function(tree, "_stem_manifest", namespace)
    return {
        "bundle_publish": functools.partial(bundle_publish.publish_stems_local, requested_codec="flac"),
        "modal_vocal_farm": functools.partial(
            _farm_function(tree, "_publish_stems_local", namespace), requested_codec="flac"
        ),
    }
