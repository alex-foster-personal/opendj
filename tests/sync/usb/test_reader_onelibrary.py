"""OneLibrary ``exportLibrary.db`` import into :class:`OpenDjLibrary` (issue #10740).

[if] OneLibrary SQLCipher import is required [then] reader_onelibrary round-trips fixture fields, [else stop].

CI-Tests: tests/sync/usb/test_reader_onelibrary.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.open_dj import BeatGridPoint, CuePoint, Track
from apps.sync.usb.pioneer.onelibrary import SQLCIPHER_AVAILABLE, SQLCIPHER_IMPORT_ERROR
from apps.sync.usb.pioneer.reader_onelibrary import READER_AVAILABLE, read_onelibrary
from apps.sync.usb.pioneer.writer_onelibrary import (
    PlaylistSpec,
    TrackUpdate,
    WRITER_AVAILABLE,
    WRITER_IMPORT_ERROR,
    write_onelibrary,
)
from tests.sync.usb.onelibrary_synth import SYNTH_TRACKS, build_synthetic_onelibrary
from tests.sync.usb.test_onelibrary_handle import _raw

pytestmark = [
    pytest.mark.requirement("DJIMPORT-10742"),
    pytest.mark.skipif(
        not (READER_AVAILABLE and WRITER_AVAILABLE),
        reason=f"sqlcipher3 unavailable: {SQLCIPHER_IMPORT_ERROR or WRITER_IMPORT_ERROR}",
    ),
]

CI_TESTS_MODULE = "tests/sync/usb/test_reader_onelibrary.py"
REQ_ID = "DJIMPORT-10742"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _enrich_fixture_for_cues_and_loudness(path: Path) -> None:
    conn = _raw(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cue (
            cue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL,
            kind INTEGER,
            cueComment TEXT,
            isActiveLoop INTEGER,
            inUsec INTEGER,
            outUsec INTEGER
        )
        """
    )
    conn.execute("INSERT INTO cue (content_id, kind, cueComment, inUsec) VALUES (2, 2, 'drop', 32000000)")
    conn.execute("CREATE TABLE IF NOT EXISTS odjAnalysisScalar (ContentID INTEGER, field TEXT, value TEXT)")
    conn.execute("INSERT INTO odjAnalysisScalar VALUES (2, 'loudness_lufs', '-9.25')")
    conn.execute("INSERT INTO odjAnalysisScalar VALUES (2, 'loudness_dbtp', '0.5')")
    conn.commit()
    conn.close()


def _track_map(library) -> dict[str, Track]:
    return {t.track_id: t for t in library.tracks}


def test_round_trip_fixture_library_is_lossless_for_tracks_playlists_cues_grid_loudness(
    tmp_path: Path,
) -> None:
    """[if] writer overlays a synth OneLibrary copy [then] reader round-trips tracks, playlists, cues, beats and loudness, [else stop]."""
    template = build_synthetic_onelibrary(tmp_path / "template" / "exportLibrary.db")
    _enrich_fixture_for_cues_and_loudness(template)
    before = read_onelibrary(template).library

    output = tmp_path / "out" / "exportLibrary.db"
    write_onelibrary(
        template_path=template,
        output_path=output,
        track_updates=[
            TrackUpdate(id=2, title="Round Trip é", rating=4, bpmx100=12300, dj_comment="drop tag"),
        ],
        playlists=[
            PlaylistSpec(name="Import Me", track_ids=(5, 2, 1)),
            PlaylistSpec(name="Nested", track_ids=(3,), parent_id=2),
        ],
    )
    after = read_onelibrary(output).library

    before_by_id = _track_map(before)
    after_by_id = _track_map(after)

    assert len(after.tracks) == len(SYNTH_TRACKS)
    untouched = after_by_id["onelibrary:1"]
    assert untouched.title == before_by_id["onelibrary:1"].title
    assert untouched.bpm == before_by_id["onelibrary:1"].bpm
    assert untouched.cues == before_by_id["onelibrary:1"].cues
    assert untouched.beats == before_by_id["onelibrary:1"].beats

    edited = after_by_id["onelibrary:2"]
    assert edited.title == "Round Trip é"
    assert edited.rating == 4
    assert edited.bpm == 123.0
    assert edited.beats == (BeatGridPoint(position_ms=0, bpm=123.0, terminal=True),)
    assert edited.extensions.get("x_loudness_lufs") == -9.25
    assert edited.extensions.get("x_loudness_dbtp") == 0.5
    assert edited.cues == (
        CuePoint(index=1, position_ms=32000, type="hot", name="drop", color_rgb=None, length_ms=None),
    )

    names = {p.name for p in after.playlists}
    assert "Import Me" in names
    import_pl = next(p for p in after.playlists if p.name == "Import Me")
    assert import_pl.track_ids == ("onelibrary:5", "onelibrary:2", "onelibrary:1")

    folder = next(p for p in after.playlists if p.name == "Folder")
    nested = next(c for c in folder.children if c.name == "Nested")
    assert nested.track_ids == ("onelibrary:3",)

    root_order = [p.name for p in sorted(after.playlists, key=lambda p: p.name)]
    assert "Existing" in root_order and "Folder" in root_order


def test_ci_tests_line_names_this_module() -> None:
    """[if] issue #10740 ships [then] CI-Tests names tests/sync/usb/test_reader_onelibrary.py, [else stop]."""
    assert CI_TESTS_MODULE == "tests/sync/usb/test_reader_onelibrary.py"
    assert (REPO_ROOT / CI_TESTS_MODULE).is_file()


def test_requirement_id_is_declared_in_reqs_json() -> None:
    """[if] DJIMPORT-10742 is added [then] reqs.json lists it, [else stop]."""
    raw = json.loads((REPO_ROOT / "reqs.json").read_text(encoding="utf-8"))

    def walk(node: object) -> list[str]:
        if isinstance(node, dict):
            ids: list[str] = []
            if "id" in node and isinstance(node["id"], str):
                ids.append(node["id"])
            for value in node.values():
                ids.extend(walk(value))
            return ids
        if isinstance(node, list):
            out: list[str] = []
            for item in node:
                out.extend(walk(item))
            return out
        return []

    assert REQ_ID in walk(raw)
