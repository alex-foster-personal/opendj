"""Listing hydrate stem summaries (browser Stems column).

Regression one-liners:
  - if a second listing page re-reads a manifest.json already summarized then broken
  - if a cached summary outlives STEM_SUMMARY_TTL_S then broken
  - if two stem roots share one cache entry for the same stable_id then broken
  - if mutating a returned summary poisons the next caller then broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.webui.server import stem_artifacts
from apps.webui.server.stem_artifacts import (
    bulk_stem_summaries,
    summarize_stem_bundle,
)


@pytest.fixture(autouse=True)
def _clear_stem_summary_cache() -> None:
    """The cache is process-wide; every test starts cold."""
    stem_artifacts._STEM_SUMMARY_CACHE.clear()


def test_summarize_none_when_missing(tmp_path: Path) -> None:
    assert summarize_stem_bundle("no-such-track", stems_dir=tmp_path) == {
        "status": "none"
    }


def _write_bundle(root: Path, sid: str, *, model: str = "hdemucs_mmi") -> Path:
    """A farm-shaped v2 bundle: four opus parts plus its manifest."""
    bundle = root / sid
    bundle.mkdir(parents=True)
    for part, size in (
        ("vocals", 1000),
        ("drums", 2000),
        ("bass", 300),
        ("other", 700),
    ):
        (bundle / f"{part}.opus").write_bytes(b"OggS" + b"\x00" * (size - 4))
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "stable_id": sid,
                "model": {"name": model, "version": "4.0.1"},
                "source": {"path": "/x.mp3", "sha256": "a" * 64},
                "files": {
                    "vocals": "vocals.opus",
                    "drums": "drums.opus",
                    "bass": "bass.opus",
                    "other": "other.opus",
                },
                "preset": {
                    "tag": f"{model}-ov0.25",
                    "model": model,
                    "overlap": 0.25,
                    "shifts": 0,
                },
                "audio": {
                    "sample_rate": 44100,
                    "channels": 2,
                    "frame_count": 100,
                },
            }
        ),
        encoding="utf-8",
    )
    return bundle


def test_summarize_ready_groups_vid(tmp_path: Path) -> None:
    sid = "track-ab"
    _write_bundle(tmp_path, sid)
    summary = summarize_stem_bundle(sid, stems_dir=tmp_path)
    assert summary["status"] == "ready"
    assert summary["model"] == "hdemucs_mmi"
    assert summary["format"] == "opus"
    assert summary["overlap"] == 0.25
    assert summary["groups"]["V"]["bytes"] == 1000
    assert summary["groups"]["D"]["bytes"] == 2000
    assert summary["groups"]["I"]["bytes"] == 1000
    assert summary["total_bytes"] == 4000


# ----- listing cache -------------------------------------------------------
#
# build_track_rows summarizes every row of every listing page, and the All
# Tracks pane re-walks the library every LIBRARY_FALLBACK_POLL_MS. Uncached
# that is one manifest read plus up to four stats per row per poll.


def _count_manifest_reads(
    monkeypatch: pytest.MonkeyPatch, manifest: Path
) -> list[int]:
    """Count ``read_text`` calls against ``manifest`` in a one-slot list."""
    calls = [0]
    real_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == manifest:
            calls[0] += 1
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    return calls


def test_bulk_summaries_read_each_manifest_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "track-cached"
    bundle = _write_bundle(tmp_path, sid)
    reads = _count_manifest_reads(monkeypatch, bundle / "manifest.json")

    cold = bulk_stem_summaries([sid], stems_dir=tmp_path)
    assert cold[sid]["status"] == "ready"
    assert reads[0] == 1, "the cold page must read the manifest exactly once"

    warm = bulk_stem_summaries([sid], stems_dir=tmp_path)
    assert warm == cold
    assert reads[0] == 1, "a re-polled page must serve from cache, not re-read"


def test_stem_summary_cache_expires_after_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staleness is bounded by STEM_SUMMARY_TTL_S and nothing longer."""
    sid = "track-ttl"
    bundle = _write_bundle(tmp_path, sid)
    clock = [1000.0]
    monkeypatch.setattr(stem_artifacts.time, "monotonic", lambda: clock[0])

    assert summarize_stem_bundle(sid, stems_dir=tmp_path)["status"] == "ready"
    for name in ("vocals", "drums", "bass", "other"):
        (bundle / f"{name}.opus").unlink()
    (bundle / "manifest.json").unlink()

    clock[0] += stem_artifacts.STEM_SUMMARY_TTL_S - 0.1
    assert summarize_stem_bundle(sid, stems_dir=tmp_path)["status"] == "ready", (
        "inside the TTL the cached answer stands"
    )
    clock[0] += 0.2
    assert summarize_stem_bundle(sid, stems_dir=tmp_path) == {"status": "none"}, (
        "past the TTL the bundle must be re-read from disk"
    )


def test_stem_summary_cache_keys_on_root(tmp_path: Path) -> None:
    """Two roots holding the same stable_id must not share one entry."""
    sid = "track-two-roots"
    demucs = tmp_path / "stems"
    roformer = tmp_path / "stems-roformer"
    _write_bundle(demucs, sid, model="htdemucs_ft")
    _write_bundle(roformer, sid, model="mel_band_roformer")

    assert summarize_stem_bundle(sid, stems_dir=demucs)["model"] == "htdemucs_ft"
    assert (
        summarize_stem_bundle(sid, stems_dir=roformer)["model"]
        == "mel_band_roformer"
    )


def test_stem_summary_cache_hands_out_isolated_summaries(tmp_path: Path) -> None:
    sid = "track-isolated"
    _write_bundle(tmp_path, sid)

    first = summarize_stem_bundle(sid, stems_dir=tmp_path)
    first["status"] = "mutated"
    first["groups"]["V"]["bytes"] = 0

    second = summarize_stem_bundle(sid, stems_dir=tmp_path)
    assert second["status"] == "ready"
    assert second["groups"]["V"]["bytes"] == 1000

pytestmark = pytest.mark.rb_parity
