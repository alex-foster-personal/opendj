"""Tests for ``open-dj-tool export`` (Phase 15 CLI finisher, OPEN-01/02)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.open_dj import CuePoint, OpenDjLibrary, Playlist, Track
from apps.open_dj.cli import main

# --------------------------------------------------------------- fake module


class _FakeExportResult:
    def __init__(self, doc: dict) -> None:
        self.document = doc
        self.tracks_count = len(doc.get("tracks", []))
        self.playlists_count = len(doc.get("playlists", []))
        self.cue_points_count = 0
        self.warnings: list[str] = []


class _FakeModuleAdapter:
    """Module-family stand-in for rekordbox / djay export paths."""

    def __init__(self, doc: dict) -> None:
        self._doc = doc
        self.calls: list[dict] = []

    def export_library(self, *, source_path, out_path, include_cues):
        self.calls.append(
            {"source_path": source_path, "out_path": out_path, "include_cues": include_cues}
        )
        return _FakeExportResult(self._doc)


# ------------------------------------------------------------- helpers


def _minimal_doc() -> dict:
    return {
        "version": "0.2",
        "source_adapter": "fake",
        "tracks": [
            {
                "track_id": "ab" * 32,
                "file_path": "music/a.flac",
                "title": "A",
                "artists": ["Alice"],
                "album": "",
                "duration_ms": 100000,
                "content_hash": "sha256:" + "f" * 64,
                "vendor_ids": {"fake": "1"},
            }
        ],
        "playlists": [],
    }


def _sample_library() -> OpenDjLibrary:
    return OpenDjLibrary(
        version="0.1",
        tracks=(
            Track(
                track_id="t-cli-1",
                file_path="/Music/cli/01.mp3",
                title="CLI One",
                artists=("Alice",),
                bpm=125.0,
                rating=4,
                duration_ms=120000,
                cues=(CuePoint(index=0, position_ms=0, type="hot", name="Intro"),),
            ),
        ),
        playlists=(Playlist(name="cli-pl", track_ids=("t-cli-1",)),),
    )


# --------------------------------------------------------------- tests


@pytest.mark.requirement("OPEN-01")
class TestExportModuleFamily:
    """Rekordbox / djay adapters via the module-family path."""

    def test_export_writes_canonical_doc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeModuleAdapter(_minimal_doc())

        def _fake_load(name: str):
            assert name == "rekordbox"
            return fake

        monkeypatch.setattr("apps.open_dj.cli.load_adapter", _fake_load)

        source = tmp_path / "master.db"
        source.write_bytes(b"fake db")
        out = tmp_path / "library.open-dj.json"

        rc = main(
            [
                "export",
                "--adapter",
                "rekordbox",
                "--source",
                str(source),
                "--out",
                str(out),
            ]
        )
        assert rc == 0
        assert out.exists()
        parsed = json.loads(out.read_bytes())
        assert parsed["tracks"][0]["title"] == "A"
        # include_cues default is True.
        assert fake.calls[0]["include_cues"] is True

    def test_export_no_cues_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeModuleAdapter(_minimal_doc())
        monkeypatch.setattr(
            "apps.open_dj.cli.load_adapter", lambda name: fake
        )
        source = tmp_path / "lib.db"
        source.write_bytes(b"")
        out = tmp_path / "out.open-dj.json"
        rc = main(
            [
                "export",
                "--adapter",
                "djay",
                "--source",
                str(source),
                "--out",
                str(out),
                "--no-cues",
            ]
        )
        assert rc == 0
        assert fake.calls[0]["include_cues"] is False

    def test_export_unknown_adapter_rejected(self, tmp_path: Path) -> None:
        # argparse choices list rejects the unknown adapter before our handler
        # runs; exit code is 2 (argparse convention).
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "export",
                    "--adapter",
                    "bogus",
                    "--source",
                    str(tmp_path),
                    "--out",
                    str(tmp_path / "x.json"),
                ]
            )
        assert excinfo.value.code == 2

    def test_export_adapter_raises_returns_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            def export_library(self, *, source_path, out_path, include_cues):
                raise RuntimeError("db corrupted")

        monkeypatch.setattr("apps.open_dj.cli.load_adapter", lambda name: _Boom())
        rc = main(
            [
                "export",
                "--adapter",
                "rekordbox",
                "--source",
                str(tmp_path / "x"),
                "--out",
                str(tmp_path / "out.json"),
            ]
        )
        assert rc == 2


@pytest.mark.requirement("OPEN-01")
class TestExportClassFamily:
    """Serato / Traktor adapters via the class-family path (read/write)."""

    def test_traktor_export_roundtrip(self, tmp_path: Path) -> None:
        """Use the real Traktor adapter: write a fixture NML, export to
        open-dj, and assert a non-empty valid-looking document lands on disk."""
        from apps.adapters.traktor import TraktorAdapter

        fixture_nml = tmp_path / "collection.nml"
        adapter = TraktorAdapter()
        # Create the fixture by writing a sample library via the adapter.
        adapter.write(_sample_library(), fixture_nml)

        out = tmp_path / "out.open-dj.json"
        rc = main(
            [
                "export",
                "--adapter",
                "traktor",
                "--source",
                str(fixture_nml),
                "--out",
                str(out),
            ]
        )
        assert rc == 0
        doc = json.loads(out.read_bytes())
        assert doc["tracks"], "expected at least one track"
        assert any(t["title"] == "CLI One" for t in doc["tracks"])
