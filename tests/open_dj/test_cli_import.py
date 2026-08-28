"""Tests for ``open-dj-tool import`` (Phase 15 CLI finisher).

Live-write paths are never exercised here; we exercise only:

* dry-run default
* typed-confirm refusal
* module-family (rekordbox / djay) live-write refusal with follow-up hint
* class-family (serato / traktor) live write via the real adapter onto a
  tmp_path target (adapter.write() is side-effect-free w.r.t. the user's
  real library because we point it at tmp_path).
* invalid input (bad JSON, schema errors)
* unknown adapter
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.open_dj.cli import main

# ---------------------------------------------------------- fixtures


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _write_minimal(tmp_path: Path) -> Path:
    """Copy the canonical minimal fixture into tmp_path for mutation."""
    src = tmp_path / "library.open-dj.json"
    src.write_bytes((_FIXTURES_DIR / "minimal.open-dj.json").read_bytes())
    return src


# ---------------------------------------------------------- dry-run


@pytest.mark.requirement("OPEN-01")
class TestImportDryRun:

    def test_dry_run_default_exits_0(self, tmp_path: Path) -> None:
        src = _write_minimal(tmp_path)
        target = tmp_path / "target.nml"
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(src),
                "--target",
                str(target),
            ]
        )
        assert rc == 0
        # Target must NOT be written in dry-run mode.
        assert not target.exists()

    def test_dry_run_module_adapter_exits_0(self, tmp_path: Path) -> None:
        src = _write_minimal(tmp_path)
        # Point --target at a file we can safely not-create.
        target = tmp_path / "master.db"
        rc = main(
            [
                "import",
                "--adapter",
                "rekordbox",
                "--source",
                str(src),
                "--target",
                str(target),
            ]
        )
        assert rc == 0
        assert not target.exists()


# ---------------------------------------------------------- safety refusal


@pytest.mark.requirement("OPEN-01")
class TestImportSafetyRefusal:

    def test_live_without_typed_confirm_refused(self, tmp_path: Path) -> None:
        src = _write_minimal(tmp_path)
        target = tmp_path / "target.nml"
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(src),
                "--target",
                str(target),
                "--live",
            ]
        )
        assert rc == 3
        assert not target.exists()

    def test_rekordbox_live_import_refused_with_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = _write_minimal(tmp_path)
        target = tmp_path / "master.db"
        target.write_bytes(b"existing db bytes")
        target_before = target.read_bytes()
        rc = main(
            [
                "import",
                "--adapter",
                "rekordbox",
                "--source",
                str(src),
                "--target",
                str(target),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        assert rc == 3
        # Never mutated the target.
        assert target.read_bytes() == target_before
        err = capsys.readouterr().err
        assert "apps.sync.apply_ratings" in err or "apps.sync.playlist_apply" in err


# ---------------------------------------------------------- validation


@pytest.mark.requirement("OPEN-01")
class TestImportInputValidation:

    def test_bad_json_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(bad),
                "--target",
                str(tmp_path / "x.nml"),
            ]
        )
        assert rc == 1

    def test_schema_invalid_exits_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "invalid.json"
        # Missing mandatory fields: validator returns errors.
        bad.write_bytes(json.dumps({"version": "0.2"}).encode())
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(bad),
                "--target",
                str(tmp_path / "x.nml"),
            ]
        )
        assert rc == 2

    def test_missing_source_exits_1(self, tmp_path: Path) -> None:
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(tmp_path / "absent.json"),
                "--target",
                str(tmp_path / "out.nml"),
            ]
        )
        assert rc == 1


# ---------------------------------------------------------- class-family live


@pytest.mark.requirement("OPEN-01")
class TestImportClassFamilyLive:
    """Exercise the real Traktor adapter.write() via the CLI, pointing at
    tmp_path. This *is* a live write, but to a throwaway temp target;
    Native Instruments' actual Traktor install is never touched.
    """

    def test_traktor_live_import_writes_nml(self, tmp_path: Path) -> None:
        # Use the canonical minimal fixture (schema-valid, provenance
        # envelopes, 40-hex track_id). The CLI unwraps those envelopes
        # before handing the library to TraktorAdapter.write().
        src = _write_minimal(tmp_path)
        target = tmp_path / "collection.nml"

        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(src),
                "--target",
                str(target),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        assert rc == 0, "live traktor import to tmp_path should succeed"
        assert target.exists()
        assert target.stat().st_size > 0


# ---------------------------------------------------------- unknown adapter


@pytest.mark.requirement("OPEN-01")
def test_import_unknown_adapter_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "import",
                "--adapter",
                "bogus",
                "--source",
                str(tmp_path / "x.json"),
                "--target",
                str(tmp_path / "y"),
            ]
        )
    assert excinfo.value.code == 2


# ---------------------------------------------------------- Codex P15-F2


@pytest.mark.requirement("OPEN-01")
def test_opendj_import_preserves_beatgrid_and_cue_color(tmp_path: Path) -> None:
    """Regression for Codex finding P15-F2.

    ``apps/open_dj/cli.py::_dict_to_library`` previously ignored the
    v0.2 wire fields ``beatgrid``, ``tracks_ordered``, and cue
    ``color``; importing a v0.2 open-dj JSON would silently strip the
    beatgrid, erase playlist membership, and blank out hot-cue colour.
    After the fix the loader is the true inverse of
    :mod:`apps.open_dj.wire` and these fields survive the JSON -> typed
    conversion.
    """
    from apps.open_dj.cli import _dict_to_library

    doc = {
        "schema_version": "0.2",
        "tracks": [
            {
                "track_id": "t1",
                "file_path": "/m/01.mp3",
                "title": "One",
                "bpm": {"value": 128.0, "source": "user", "modified_at": "2024-01-01T00:00:00Z"},
                "cue_points": [
                    {
                        "index": 0,
                        "position_ms": 1000,
                        "type": "hot",
                        "name": "Intro",
                        "color": "#ff8800",
                    },
                ],
                "beatgrid": {
                    "origin_ms": 0.0,
                    "bpm": 128.0,
                    "algorithm": "constant",
                    "beats": [0, 468, 937, 1406],
                },
            },
        ],
        "playlists": [
            {"name": "warmup", "tracks_ordered": ["t1"]},
        ],
    }

    lib = _dict_to_library(doc)
    assert len(lib.tracks) == 1
    t = lib.tracks[0]
    # cue color round-trip: "#ff8800" -> 0xFF8800
    assert len(t.cues) == 1
    assert t.cues[0].color_rgb == 0xFF8800
    assert t.cues[0].type == "hot"
    assert t.cues[0].name == "Intro"
    # beatgrid -> BeatGridPoint tuple
    assert len(t.beats) == 4
    assert [b.position_ms for b in t.beats] == [0, 468, 937, 1406]
    assert t.beats[0].bpm == 128.0
    assert t.beats[-1].terminal is True
    # tracks_ordered -> track_ids
    assert len(lib.playlists) == 1
    assert lib.playlists[0].track_ids == ("t1",)

