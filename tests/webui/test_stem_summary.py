"""Listing hydrate stem summaries (browser Stems column)."""

from __future__ import annotations

import json
from pathlib import Path

from apps.webui.server.stem_artifacts import summarize_stem_bundle


def test_summarize_none_when_missing(tmp_path: Path) -> None:
    assert summarize_stem_bundle("no-such-track", stems_dir=tmp_path) == {
        "status": "none"
    }


def test_summarize_ready_groups_vid(tmp_path: Path) -> None:
    sid = "track-ab"
    bundle = tmp_path / sid
    bundle.mkdir()
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
                "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
                "source": {"path": "/x.mp3", "sha256": "a" * 64},
                "files": {
                    "vocals": "vocals.opus",
                    "drums": "drums.opus",
                    "bass": "bass.opus",
                    "other": "other.opus",
                },
                "preset": {
                    "tag": "hdemucs_mmi-ov0.25",
                    "model": "hdemucs_mmi",
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
    summary = summarize_stem_bundle(sid, stems_dir=tmp_path)
    assert summary["status"] == "ready"
    assert summary["model"] == "hdemucs_mmi"
    assert summary["format"] == "opus"
    assert summary["overlap"] == 0.25
    assert summary["groups"]["V"]["bytes"] == 1000
    assert summary["groups"]["D"]["bytes"] == 2000
    assert summary["groups"]["I"]["bytes"] == 1000
    assert summary["total_bytes"] == 4000
