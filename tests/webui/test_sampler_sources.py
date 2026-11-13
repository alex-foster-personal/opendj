"""Edge sampler slot sources: the sources seam, library predicates, state.db and stem-set reads.

[if] the sampler reads a track's sources wrongly [then] these tests fail, [else stop].

Regression one-liners:
  - if an injected app.state.sampler_sources is not the one used then broken
  - if a library flag disagrees with the library filter's predicate then broken
  - if a track without duration_ms gets a guessed duration instead of a ValueError then broken
  - if a stem-set manifest's layout does not map to sampler part names then broken
  - if crates do not follow the playlists pane's order, or the Inbox counts as a crate, then broken
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request

from apps.shared.state import db as state_db
from apps.webui.server import playlist_tree_order as tree_order
from apps.webui.server import sampler_sources as sources
from apps.webui.server.backend import InMemoryBackend, Playlist
from tests.health_lights.fixtures import write_stem_bundle

SID = "a" * 40


def _request(**app_state: Any) -> Request:
    """A real Starlette request on an app carrying ``app_state``."""
    app = FastAPI()
    for name, value in app_state.items():
        setattr(app.state, name, value)
    return Request({"type": "http", "app": app, "headers": []})


def _state(path: Path, tracks: dict[str, int | None]) -> Path:
    """A migrated state.db holding ``tracks`` (stable_id -> duration_ms)."""
    conn = state_db.open_rw(path)
    try:
        for sid, duration in tracks.items():
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, '2026-01-01', '2026-01-01')",
                (sid, duration),
            )
        conn.commit()
    finally:
        conn.close()
    return path


# ---------- the sources seam ---------------------------------------------------------


def test_if_the_app_injects_sampler_sources_then_they_are_the_ones_used() -> None:
    """[if] app.state.sampler_sources is set [then] sources_for returns it, else production, [else stop]."""
    injected = object()
    assert sources.sources_for(_request(sampler_sources=injected)) is injected
    assert isinstance(sources.sources_for(_request()), sources.ProductionSources)


# ---------- library predicates -------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "stable_id", "expected"),
    [
        ({"file_exists": True}, SID, {"broken": False, "remix": False, "vocals": False, "offline": True}),
        (
            {"file_exists": False, "is_remix": True},
            SID,
            {"broken": True, "remix": True, "vocals": False, "offline": False},
        ),
        (
            {"file_exists": True, "is_streaming": True},
            SID,
            {"broken": False, "remix": False, "vocals": False, "offline": False},
        ),
        (
            {"file_exists": True},
            "spotify-pending:x",
            {"broken": False, "remix": False, "vocals": False, "offline": False},
        ),
    ],
)
def test_if_a_row_is_broken_remix_streaming_or_pending_then_the_flags_say_so(
    row: dict[str, Any], stable_id: str, expected: dict[str, bool]
) -> None:
    """[if] a row is missing, a remix, streaming or spotify-pending [then] flags match the library's, [else stop]."""
    assert sources.library_flags(row, stable_id) == expected


@pytest.mark.parametrize(
    ("vocals", "lyrics", "expected"),
    [
        ({"status": "rekordbox", "regions": [[0, 1]]}, None, True),
        ({"status": "demucs", "regions": []}, None, False),
        ({"status": "unknown", "regions": [[0, 1]]}, None, False),
        (None, {"effective": "lyrics", "n_lines": 6}, True),
        (None, {"effective": "lyrics", "n_lines": 5}, False),
        (None, {"effective": "no-lyrics", "n_lines": 40}, False),
        (None, {"effective": "vocal", "n_lines": None}, True),
    ],
)
def test_if_a_row_has_vocal_regions_or_six_lyric_lines_then_its_vocals_flag_is_set(
    vocals: dict[str, Any] | None, lyrics: dict[str, Any] | None, expected: bool
) -> None:
    """[if] a row has found vocal regions, 6+ lyric lines or a vocal verdict [then] vocals is true, [else stop]."""
    assert sources.library_flags({"vocals": vocals, "lyrics": lyrics}, SID)["vocals"] is expected


# ---------- state.db and stem-set reads ----------------------------------------------


def test_if_a_track_has_a_duration_then_it_is_read_from_the_apps_state_db(tmp_path: Path) -> None:
    """[if] the app's state.db holds the track's duration_ms [then] duration_ms returns it, [else stop]."""
    state = _state(tmp_path / "state" / "state.db", {SID: 240_000})
    assert sources.duration_ms(_request(state_db_path=state), SID) == 240_000.0


@pytest.mark.parametrize("tracks", [{SID: None}, {SID: 0}, {}])
def test_if_a_track_has_no_duration_then_reading_it_refuses_instead_of_guessing(
    tmp_path: Path, tracks: dict[str, int | None]
) -> None:
    """[if] a track's duration_ms is null, zero or the track is absent [then] ValueError, [else stop]."""
    state = _state(tmp_path / "state" / "state.db", tracks)
    with pytest.raises(ValueError, match="has no duration_ms"):
        sources.duration_ms(_request(state_db_path=state), SID)


def test_if_a_track_has_a_stem_set_then_its_parts_and_files_are_in_sampler_names(tmp_path: Path) -> None:
    """[if] a roformer2 stem-set exists [then] parts are vocals, inst and inst resolves to its file, [else stop]."""
    root = tmp_path / "stems"
    bundle = write_stem_bundle(root, SID)
    request = _request(stem_roots=[root])
    prod = sources.ProductionSources()
    assert prod.stem_parts(request, SID) == ("vocals", "inst")
    assert prod.stem_part_path(request, SID, "inst") == bundle / "instrumental.wav"
    assert prod.stem_part_path(request, SID, "drums") is None


def test_if_a_track_has_no_stem_set_then_it_has_no_parts_and_no_part_files(tmp_path: Path) -> None:
    """[if] no stem root holds the track [then] stem_parts and stem_part_path are None, [else stop]."""
    request = _request(stem_roots=[tmp_path / "stems"])
    prod = sources.ProductionSources()
    assert (prod.stem_parts(request, SID), prod.stem_part_path(request, SID, "vocals")) == (None, None)


@pytest.mark.parametrize(
    ("layout", "expected"),
    [("demucs4", ("vocals", "drums", "bass", "inst")), (None, ("vocals", "drums", "bass", "inst")), ("other9", None)],
)
def test_if_a_manifest_names_a_layout_then_its_parts_map_to_sampler_names(
    tmp_path: Path, layout: str | None, expected: tuple[str, ...] | None
) -> None:
    """[if] a manifest says demucs4, nothing, or an unknown layout [then] parts are the four or None, [else stop]."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}" if layout is None else f'{{"layout": "{layout}"}}', encoding="utf-8")
    assert sources.stem_parts_of_manifest(manifest) == expected


# ---------- crates -------------------------------------------------------------------


def test_if_the_user_ordered_a_crate_first_then_crates_follow_the_pane_order_without_the_inbox(tmp_path: Path) -> None:
    """[if] the user put crate C first [then] crates are C then the rest by name, Inbox left out, [else stop]."""
    backend = InMemoryBackend()
    for pid, name, items in (("p-b", "beta", ["s1"]), ("p-a", "Alpha", ["s2", "s1"]), ("p-c", "Cee", ["s3"])):
        backend.seed_playlist(Playlist(playlist_id=pid, name=name, items=items))
    tree_order.save_order(tree_order.path_for(tmp_path), ["p-c"])
    request = _request(state_db_path=tmp_path / "state" / "state.db")
    assert sources.ProductionSources().crates(request, backend) == [("s3",), ("s2", "s1"), ("s1",)]
