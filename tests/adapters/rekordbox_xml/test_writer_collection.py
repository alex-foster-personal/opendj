"""Whole-collection rekordbox XML export round-trips through the reader (LIBORG-10051)."""

from __future__ import annotations

import tracemalloc
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest

from apps.adapters.rekordbox_xml.library_export import ExportRefused, plan_library_export
from apps.adapters.rekordbox_xml.reader import location_to_path, read_rekordbox_xml
from apps.adapters.rekordbox_xml.writer import XmlNode, XmlTrack, iter_dj_playlists_xml, path_to_location
from apps.shared.state.db import open_ro, open_rw
from apps.webui.server.analysis_overlay import lane_owned_fields
from tests.adapters.rekordbox_xml.export_fixtures import (
    MUSIC,
    add_members,
    add_node,
    add_projection,
    add_raw_track,
    add_tracks,
    delete_tracks,
    make_db,
    make_tree_library,
    set_field,
)


def _plan(db: Path):
    return plan_library_export(open_ro(db), lane_fields=partial(lane_owned_fields, state_db_path=db))


def _export(db: Path, out: Path) -> Path:
    export = _plan(db)
    with out.open("wb") as fh:
        for chunk in export.iter_bytes():
            fh.write(chunk)
    return out


def test_tree_library_round_trips_with_same_tracks_and_nesting(tmp_path: Path) -> None:
    db, ids = make_tree_library(tmp_path)
    parsed = read_rekordbox_xml(_export(db, tmp_path / "out.xml"))
    tracks = parsed.library.tracks
    assert len(tracks) == 5, "if export writes a track twice or drops one then broken"
    assert sorted(t.file_path for t in tracks) == sorted(f"{MUSIC}/{sid} #1 %20.mp3" for sid in ids)
    first = next(t for t in tracks if t.file_path.endswith(f"{ids[0]} #1 %20.mp3"))
    assert (first.title, first.artists, first.album) == (f"Song {ids[0]} & co", (f"Artist {ids[0]}",), "Album <1>")
    assert (first.bpm, first.key_camelot, first.rating, first.duration_ms) == (120.5, "8A", 0, 200_000)
    by_id = {t.track_id: t.file_path.rsplit("/", 1)[1].split(" ")[0] for t in tracks}
    (crates,) = parsed.library.playlists
    assert crates.name == "Crates"
    (house,) = crates.children
    assert [p.name for p in house.children] == ["Warmup", "Peak"], "if sort_index order is lost then broken"
    warmup, peak = house.children
    assert [by_id[t] for t in warmup.track_ids] == [ids[2], ids[0], ids[1]]
    assert [by_id[t] for t in peak.track_ids] == [ids[4], ids[3]]


def test_deleted_tracks_are_in_no_collection_or_node(tmp_path: Path) -> None:
    db, ids = make_tree_library(tmp_path)
    delete_tracks(db, {ids[3]: "user", ids[4]: "missing"})
    out = _export(db, tmp_path / "out.xml")
    parsed = read_rekordbox_xml(out)
    assert len(parsed.library.tracks) == 3, "if deleted tracks reach COLLECTION then broken"
    peak = parsed.library.playlists[0].children[0].children[1]
    assert peak.name == "Peak" and peak.track_ids == ()
    raw_peak = next(n for n in ET.parse(out).iter("NODE") if n.get("Name") == "Peak")
    assert raw_peak.get("Entries") == "0"


def test_every_entries_and_count_equals_real_children(tmp_path: Path) -> None:
    db, _ = make_tree_library(tmp_path)
    root = ET.parse(_export(db, tmp_path / "out.xml")).getroot()
    collection = root.find("COLLECTION")
    assert collection is not None and collection.get("Entries") == str(len(collection.findall("TRACK")))
    for node in root.iter("NODE"):
        if node.get("Type") == "0":
            assert node.get("Count") == str(len(node.findall("NODE")))
        elif node.get("Type") == "1":
            assert node.get("Entries") == str(len(node.findall("TRACK")))


def _tracks(n: int, consumed: list[int]) -> Iterator[XmlTrack]:
    for i in range(n):
        consumed[0] += 1
        yield XmlTrack(i + 1, path_to_location(f"/m/{i}.mp3"), f"t{i}", "a", "b", 1000, 120.0, "8A", 3)


def test_writer_yields_first_chunk_before_tracks_are_exhausted() -> None:
    consumed = [0]
    stream = iter_dj_playlists_xml(
        track_count=10_000, tracks=_tracks(10_000, consumed), tree=(), entries_of=lambda k: ()
    )
    first = next(stream)
    assert first.startswith(b"<?xml")
    assert consumed[0] < 10_000, "if the writer buffers the whole collection then broken"


def test_10k_track_export_peak_memory_under_100mb(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    ids = add_tracks(db, 10_000)
    add_node(db, "p", "All", "playlist", None, 0)
    add_members(db, "p", ids)
    tracemalloc.start()
    try:
        out = _export(db, tmp_path / "big.xml")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"10k export peak traced memory: {peak / 1e6:.1f} MB")
    assert peak < 100e6
    assert len(read_rekordbox_xml(out).library.tracks) == 10_000


def test_entry_count_drift_raises() -> None:
    node = XmlNode(key="p", name="P", is_folder=False, entry_count=2)
    with pytest.raises(ValueError, match="declared 2 entries but 1"):
        b"".join(iter_dj_playlists_xml(track_count=0, tracks=(), tree=(node,), entries_of=lambda k: [1]))


def test_track_count_drift_raises() -> None:
    with pytest.raises(ValueError, match="declared 1 tracks but more"):
        b"".join(iter_dj_playlists_xml(track_count=1, tracks=_tracks(2, [0]), tree=(), entries_of=lambda k: ()))


def test_duplicate_sibling_names_refused_before_any_byte(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_node(db, "a", "Same", "playlist", None, 0)
    add_node(db, "b", "Same", "playlist", None, 1)
    with pytest.raises(ExportRefused, match="share a name"):
        _plan(db)


def test_orphaned_parent_refused(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_node(db, "a", "Lost", "playlist", "gone", 0)
    with pytest.raises(ExportRefused, match="parent is not a live folder"):
        _plan(db)


def test_sets_not_exported_and_pathless_track_counted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    ids = add_tracks(db, 2)
    add_node(db, "s", "Friday set", "set", None, 0)
    add_members(db, "s", ids)
    conn = open_rw(db, apply_schema=False)
    with conn:
        conn.execute("UPDATE tracks SET file_path = NULL WHERE stable_id = ?", (ids[1],))
    conn.close()
    export = _plan(db)
    assert (export.track_count, export.skipped_no_location, export.tree) == (1, 1, ())
    export.close()
    parsed = read_rekordbox_xml(_export(db, tmp_path / "o.xml"))
    assert len(parsed.library.tracks) == 1 and parsed.library.playlists == ()


@pytest.mark.parametrize("path", ["/Volumes/Invented DJ/a b%c#d é.mp3", "C:/Music/a b.mp3", "//nas/share/a.mp3"])
def test_path_to_location_round_trips(path: str) -> None:
    assert location_to_path(path_to_location(path)) == path


def test_empty_library_is_read_back(tmp_path: Path) -> None:
    out = _export(make_db(tmp_path), tmp_path / "e.xml")
    root = ET.parse(out).getroot()
    assert root.find("COLLECTION").get("Entries") == "0"  # type: ignore[union-attr]
    assert root.find("PLAYLISTS/NODE").get("Count") == "0"  # type: ignore[union-attr]
    assert read_rekordbox_xml(out).library.tracks == ()


def test_zero_bpm_sentinel_exports_as_unknown_tempo(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    (sid,) = add_tracks(db, 1)
    set_field(db, sid, "bpm", "0.0")
    (track,) = read_rekordbox_xml(_export(db, tmp_path / "z.xml")).library.tracks
    assert track.bpm is None, "if a stored bpm 0.0 aborts or exports a tempo then broken"


def test_negative_bpm_still_refused(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    (sid,) = add_tracks(db, 1)
    set_field(db, sid, "bpm", "-1")
    with pytest.raises(ValueError, match="non-negative"):
        _export(db, tmp_path / "n.xml")


def test_streaming_tracks_are_skipped_and_counted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    ids = add_tracks(db, 2)
    add_raw_track(db, "sp", "spotify:track:x")
    add_raw_track(db, "web", "https://example.invalid/a.mp3")
    add_node(db, "p", "Mixed", "playlist", None, 0)
    add_members(db, "p", [ids[0], "sp", "web", ids[1]])
    export = _plan(db)
    assert (export.track_count, export.skipped_streaming) == (2, 2), "if streaming tracks are counted then broken"
    export.close()
    out = _export(db, tmp_path / "s.xml")
    parsed = read_rekordbox_xml(out)
    assert len(parsed.library.tracks) == 2
    (mixed,) = parsed.library.playlists
    assert len(mixed.track_ids) == 2
    raw = next(n for n in ET.parse(out).iter("NODE") if n.get("Name") == "Mixed")
    assert raw.get("Entries") == "2", "if a streaming track is counted in Entries then broken"


def test_own_analysis_overrides_track_fields(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    (sid,) = add_tracks(db, 1)
    set_field(db, sid, "bpm", "120")
    set_field(db, sid, "key", '"8A"')
    add_projection(db, sid, "bpm", 128.0, "ok")
    add_projection(db, sid, "key", "5A", "ok")
    (track,) = read_rekordbox_xml(_export(db, tmp_path / "o.xml")).library.tracks
    assert (track.bpm, track.key_camelot) == (128.0, "5A"), "if export ignores the effective own values then broken"


@pytest.mark.parametrize("status", ["failed", "missing"])
def test_failed_own_lane_is_omitted_not_backfilled(tmp_path: Path, status: str) -> None:
    db = make_db(tmp_path)
    (sid,) = add_tracks(db, 1)
    add_projection(db, sid, "bpm", None, status)
    add_projection(db, sid, "key", None, status)
    (track,) = read_rekordbox_xml(_export(db, tmp_path / "f.xml")).library.tracks
    assert (track.bpm, track.key_camelot) == (None, None), "if a failed own lane exports the stored value then broken"
