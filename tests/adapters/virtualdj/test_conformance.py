"""Read-side VirtualDJ conformance (OPEN-03c, DJIMPORT-10056).

[if] a conformance fixture is rendered to VirtualDJ XML [then] read-back matches masked expectations, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.adapters.virtualdj import read_virtualdj_library
from apps.open_dj import OpenDjLibrary, Playlist, Track, serialize_jcs
from tests.test_conformance import ALL_FIXTURES, FIXTURE_ROOT, _dict_to_library, _library_to_dict, _mask

pytestmark = [pytest.mark.conformance, pytest.mark.requirement("OPEN-03c"), pytest.mark.requirement("DJIMPORT-10056")]


def _render_song_xml(track: Track) -> str | None:
    if "://" in track.file_path:
        return None
    tags = [
        f'Author="{" & ".join(track.artists)}"',
        f'Title="{track.title}"',
        f'Album="{track.album}"',
    ]
    if track.rating is not None:
        tags.append(f'Stars="{track.rating}"')
    if track.bpm is not None and track.bpm > 0:
        period = 60.0 / track.bpm
        tags.append(f'Bpm="{period:.9f}"')
    if track.key_camelot:
        tags.append(f'Key="{track.key_camelot}"')
    pois: list[str] = []
    for cue in track.cues:
        if cue.type == "hot":
            pois.append(f'<Poi Pos="{cue.position_ms / 1000:.3f}" Type="cue" Num="{cue.index + 1}" Name="{cue.name}"/>')
        elif cue.type == "memory":
            pois.append(f'<Poi Pos="{cue.position_ms / 1000:.3f}" Type="cue" Name="{cue.name}"/>')
        elif cue.type == "loop" and cue.length_ms:
            pois.append(
                f'<Poi Pos="{cue.position_ms / 1000:.3f}" Type="loop" Size="{cue.length_ms / 1000:.3f}" Name="{cue.name}"/>'
            )
    length = (track.duration_ms or 0) / 1000
    return (
        f'<Song FilePath="{track.file_path}"><Tags {" ".join(tags)}/>'
        f'<Infos SongLength="{length:.3f}" PlayCount="{track.play_count}"/>'
        f'{"".join(pois)}</Song>'
    )


def _write_vdj_playlists(
    playlists: tuple[Playlist, ...],
    folder: Path,
    id_to_path: dict[str, str],
) -> None:
    for playlist in playlists:
        if playlist.children:
            sub = folder / playlist.name
            sub.mkdir(exist_ok=True)
            _write_vdj_playlists(playlist.children, sub, id_to_path)
        if playlist.track_ids or not playlist.children:
            members = "".join(f' <song path="{id_to_path.get(tid, tid)}"/>' for tid in playlist.track_ids)
            (folder / f"{playlist.name}.vdjfolder").write_text(
                f'<?xml version="1.0"?><VirtualFolder>{members}</VirtualFolder>',
                encoding="utf-8",
            )


def _render(library: OpenDjLibrary, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    mylists = root / "MyLists"
    mylists.mkdir(exist_ok=True)
    songs = [xml for t in library.tracks if (xml := _render_song_xml(t)) is not None]
    (root / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database Version="8.2">' + "".join(songs) + "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    id_to_path = {t.track_id: t.file_path for t in library.tracks}
    _write_vdj_playlists(library.playlists, mylists, id_to_path)


def _normalize_track(track: Track) -> Track:
    artists = (" & ".join(track.artists),) if track.artists else ()
    cues = tuple(
        c
        for c in track.cues
        if c.type in ("hot", "memory", "loop")
    )
    bpm = round(track.bpm, 2) if track.bpm is not None else None
    return Track(
        track_id="masked",
        file_path="masked",
        title=track.title,
        artists=artists,
        album=track.album,
        bpm=bpm,
        key_camelot=track.key_camelot,
        rating=track.rating,
        duration_ms=track.duration_ms,
        play_count=track.play_count,
        cues=cues,
        beats=(),
        extensions={},
    )


def _normalize_library(library: OpenDjLibrary) -> OpenDjLibrary:
    tracks = tuple(_normalize_track(t) for t in library.tracks if "://" not in t.file_path)
    return OpenDjLibrary(version=library.version, tracks=tracks, playlists=library.playlists)


def _playlist_signature(library: OpenDjLibrary) -> list[tuple[str, list[str]]]:
    id_to_path = {t.track_id: t.file_path for t in library.tracks}

    def walk(playlists: tuple[Playlist, ...], prefix: tuple[str, ...] = ()) -> list[tuple[str, list[str]]]:
        out: list[tuple[str, list[str]]] = []
        for pl in playlists:
            path = "/".join((*prefix, pl.name))
            if pl.track_ids or not pl.children:
                members = [id_to_path.get(tid, tid) for tid in pl.track_ids]
                out.append((path, members))
            out.extend(walk(pl.children, (*prefix, pl.name)))
        return out

    return walk(library.playlists)


@pytest.mark.parametrize("fixture_id", ALL_FIXTURES)
def test_virtualdj_read_conformance(fixture_id: str, tmp_path: Path) -> None:
    """[if] a conformance fixture is rendered to VirtualDJ XML [then] read-back matches masked expectations, [else stop]."""
    root = FIXTURE_ROOT / fixture_id
    expected_doc = json.loads((root / "expected.opendj.json").read_text(encoding="utf-8"))
    # VirtualDJ drops streaming FilePaths entirely; exclude them from the comparison set.
    expected_doc = {
        **expected_doc,
        "tracks": [t for t in expected_doc.get("tracks", []) if "://" not in t.get("file_path", "")],
    }
    drops_compare = ["color_rgb", "cue_points.color", "bpm", "track_id", "file_path"]
    drops_render = ["color_rgb", "cue_points.color"]
    expected_lib = _normalize_library(_dict_to_library(_mask(expected_doc, drops_compare)))
    render_root = tmp_path / "vdj"
    _render(_dict_to_library(_mask(expected_doc, drops_render)), render_root)
    read = read_virtualdj_library(render_root, windows=False)
    actual_lib = _normalize_library(read.library)
    assert serialize_jcs(_library_to_dict(actual_lib)) == serialize_jcs(_library_to_dict(expected_lib))
    assert _playlist_signature(actual_lib) == _playlist_signature(expected_lib)
