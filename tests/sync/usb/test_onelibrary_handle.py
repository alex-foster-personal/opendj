"""Own OneLibrary handle and writer against a synthetic encrypted export.

[if] OneLibrary code opens an encrypted export [then] it works through our own sqlcipher3 handle, no rbox, [else stop].

Runs on every host with sqlcipher3 (CI included): the export is built by
``tests/sync/usb/onelibrary_synth.py`` from invented tracks, so no fixture
host is needed. Real-stick parity stays with the fixture-backed modules
(test_pioneer_writer_onelibrary.py, test_pioneer_export_workflow.py).
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.sync.usb.pioneer import differ
from apps.sync.usb.pioneer.onelibrary import (
    SQLCIPHER_AVAILABLE,
    SQLCIPHER_IMPORT_ERROR,
    OneLibrary,
    OneLibraryError,
    onelibrary_key,
)
from apps.sync.usb.pioneer.value_verify_sidecar import (
    read_scalar_sidecar_from_onelibrary,
)
from apps.sync.usb.pioneer.writer_onelibrary import (
    BACKEND,
    OneLibraryWriteError,
    PlaylistSpec,
    TrackUpdate,
    read_playlist_roundtrip,
    write_onelibrary,
)
from tests.sync.usb.onelibrary_synth import SYNTH_TRACKS, build_synthetic_onelibrary

pytestmark = [
    pytest.mark.requirement("CAT-06"),
    pytest.mark.skipif(
        not SQLCIPHER_AVAILABLE, reason=f"sqlcipher3 unavailable: {SQLCIPHER_IMPORT_ERROR}"
    ),
]


@pytest.fixture
def synth(tmp_path: Path) -> Path:
    return build_synthetic_onelibrary(tmp_path / "template" / "exportLibrary.db")


def _raw(path: Path) -> sqlite3.Connection:
    from sqlcipher3 import dbapi2 as sqlcipher

    conn = sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = '{onelibrary_key()}'")
    return conn


def _sibling_seqs(path: Path, sql: str, arg: int) -> list[int]:
    conn = _raw(path)
    try:
        return [int(r[0]) for r in conn.execute(sql, (arg,))]
    finally:
        conn.close()


# ---------------------------------------------------------------- open/read


def test_no_production_code_imports_rbox() -> None:
    """rbox 0.1.6+ is GPL-3.0-only; nothing under apps/ may import it again."""
    import re

    pattern = re.compile(r"^\s*(import rbox\b|from rbox\b)", re.MULTILINE)
    apps_root = Path(__file__).resolve().parents[3] / "apps"
    sources = [p for p in apps_root.rglob("*.py") if "node_modules" not in p.parts]
    assert len(sources) > 100, "control: the scan must actually see the apps tree"
    hits = [str(p) for p in sources if pattern.search(p.read_text("utf-8", "replace"))]
    assert hits == []
    # Positive control: the pattern does fire on an import line.
    assert pattern.search("    from rbox import OneLibrary\n")


def test_export_is_encrypted_and_opens_only_with_the_key(synth: Path) -> None:
    # Negative control: plain sqlite cannot read it, so the fixture really
    # exercises SQLCipher rather than an unencrypted look-alike.
    plain = sqlite3.connect(str(synth))
    with pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT count(*) FROM sqlite_master").fetchone()
    plain.close()
    with pytest.raises(OneLibraryError, match="cannot open"):
        OneLibrary(synth, key="r8gd-not-the-key")
    with OneLibrary(synth) as db:
        assert len(db.get_contents()) == len(SYNTH_TRACKS)


def test_rows_use_short_snake_case_keys(synth: Path) -> None:
    with OneLibrary(synth) as db:
        track = db.get_content_by_id(3)
        playlist = db.get_playlist_by_id(3)
    assert track is not None and playlist is not None
    assert track["id"] == 3
    assert track["title"] == "Synth Three 日本"
    assert track["file_name"] == "three.mp3"
    assert track["artist_id"] == 1 and "original_artist_id" in track
    assert "dj_comment" in track and "djComment" not in track
    assert playlist == {
        "id": 3, "seq": 1, "name": "Child", "image_id": 0, "attribute": 0, "parent_id": 2
    }


def test_playlist_contents_follow_sequence_order(synth: Path) -> None:
    with OneLibrary(synth) as db:
        assert [c["id"] for c in db.get_playlist_contents(1)] == [3, 1]


def test_non_onelibrary_sqlcipher_file_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    conn = _raw(other)
    conn.execute("CREATE TABLE content (content_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(OneLibraryError, match="playlist"):
        OneLibrary(other)


# ------------------------------------------------------------------ writes


def test_append_never_repeats_a_sibling_sequence_number(synth: Path) -> None:
    """rbox 0.1.7 appended at ``count``, duplicating the last sibling's seq."""
    with OneLibrary(synth) as db:
        top = db.create_playlist("Appended")
        child = db.create_playlist("Appended child", parent_id=2)
        db.create_playlist_content(1, 5)
    assert (top["seq"], child["seq"]) == (3, 2)
    top_seqs = _sibling_seqs(
        synth, 'SELECT "sequenceNo" FROM playlist WHERE playlist_id_parent = ? ORDER BY 1', 0
    )
    assert top_seqs == [1, 2, 3]
    member_seqs = _sibling_seqs(
        synth, 'SELECT "sequenceNo" FROM playlist_content WHERE playlist_id = ? ORDER BY 1', 1
    )
    assert member_seqs == [1, 2, 3]


def test_insert_at_position_shifts_later_siblings(synth: Path) -> None:
    """Overshoot control: explicit positions still insert, not append."""
    with OneLibrary(synth) as db:
        db.create_playlist_content(1, 5, seq=1)
        assert [c["id"] for c in db.get_playlist_contents(1)] == [5, 3, 1]
        first = db.create_playlist("First", seq=1)
        assert first["seq"] == 1
        names = [p["name"] for p in sorted(
            (p for p in db.get_playlists() if p["parent_id"] == 0), key=lambda p: p["seq"]
        )]
    assert names == ["First", "Existing", "Folder"]


@pytest.mark.parametrize("seq", [-1, 4, 99])
def test_out_of_range_sequence_is_refused(synth: Path, seq: int) -> None:
    with OneLibrary(synth) as db, pytest.raises(OneLibraryError, match="invalid sequence"):
        db.create_playlist("Bad", seq=seq)


def test_zero_based_file_appends_from_its_own_base(synth: Path) -> None:
    conn = _raw(synth)
    conn.execute('UPDATE playlist_content SET "sequenceNo" = "sequenceNo" - 1')
    conn.commit()
    conn.close()
    with OneLibrary(synth) as db:
        assert db.create_playlist_content(1, 4)["seq"] == 2
        assert [c["id"] for c in db.get_playlist_contents(1)] == [3, 1, 4]


def test_unknown_ids_are_refused(synth: Path) -> None:
    with OneLibrary(synth) as db:
        with pytest.raises(OneLibraryError, match="content id=99"):
            db.create_playlist_content(1, 99)
        with pytest.raises(OneLibraryError, match="playlist id=99"):
            db.create_playlist_content(99, 1)
        with pytest.raises(OneLibraryError, match="parent playlist id=99"):
            db.create_playlist("Orphan", parent_id=99)
        with pytest.raises(OneLibraryError, match="unknown content field"):
            db.update_content({"id": 1, "not_a_column": 1})


def test_writer_round_trips_overlays_and_playlists(synth: Path, tmp_path: Path) -> None:
    output = tmp_path / "PIONEER" / "rekordbox" / "exportLibrary.db"
    result = write_onelibrary(
        template_path=synth,
        output_path=output,
        track_updates=[
            TrackUpdate(id=2, title="Edited é 🎧", rating=4, bpmx100=12345,
                        dj_comment="cue at drop", color_id=1),
        ],
        playlists=[
            PlaylistSpec(name="Set A", track_ids=(5, 2, 4)),
            PlaylistSpec(name="Set B", track_ids=(1,)),
            PlaylistSpec(name="In folder", track_ids=(3,), parent_id=2),
        ],
    )
    assert result.backend == BACKEND
    assert result.tracks_updated == 1 and result.playlists_written == 3

    set_a = read_playlist_roundtrip(onelibrary_path=output, playlist_id=result.playlist_ids[0])
    assert set_a["name"] == "Set A" and set_a["seq"] == 3
    assert [t["id"] for t in set_a["tracks"]] == [5, 2, 4]
    assert set_a["tracks"][1]["title"] == "Edited é 🎧"
    assert set_a["tracks"][1]["bpmx100"] == 12345
    with OneLibrary(output) as db:
        edited = db.get_content_by_id(2)
        untouched = db.get_content_by_id(1)
        folder_child = db.get_playlist_by_id(result.playlist_ids[2])
    assert edited is not None and untouched is not None and folder_child is not None
    assert (edited["rating"], edited["dj_comment"], edited["color_id"]) == (4, "cue at drop", 1)
    assert untouched["title"] == "Synth One"
    assert (folder_child["parent_id"], folder_child["seq"]) == (2, 2)

    # The template is never modified.
    with OneLibrary(synth) as db:
        assert len(db.get_playlists()) == 3
        assert db.get_content_by_id(2)["title"] == "Synth Two ü"  # type: ignore[index]


def test_writer_carries_pending_wal_pages(synth: Path, tmp_path: Path) -> None:
    """A template whose newest rows live only in -wal keeps them in the output."""
    conn = _raw(synth)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute(
        'INSERT INTO playlist ("sequenceNo", name, attribute, playlist_id_parent) '
        "VALUES (3, 'Only in WAL', 0, 0)"
    )
    conn.commit()
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    for suffix in ("", "-wal"):
        shutil.copyfile(f"{synth}{suffix}", snapshot / f"exportLibrary.db{suffix}")
    conn.close()
    template = snapshot / "exportLibrary.db"
    assert (snapshot / "exportLibrary.db-wal").stat().st_size > 0

    output = tmp_path / "out" / "exportLibrary.db"
    write_onelibrary(template_path=template, output_path=output)
    with OneLibrary(output) as db:
        assert "Only in WAL" in {p["name"] for p in db.get_playlists()}


def test_writer_refuses_template_equal_to_output(synth: Path) -> None:
    with pytest.raises(OneLibraryWriteError, match="must not equal"):
        write_onelibrary(template_path=synth, output_path=synth)


# ------------------------------------------------------- differ and verify


def test_differ_snapshot_counts_tables(synth: Path) -> None:
    snap = differ.snapshot_onelibrary(synth)
    assert snap.error is None
    counts = {t.table_name: t.row_count for t in snap.tables}
    assert counts["content"] == len(SYNTH_TRACKS)
    assert counts["playlist"] == 3
    assert counts["artist"] == 1 and counts["genre"] == 1
    assert sorted(snap.playlist_names) == ["Child", "Existing", "Folder"]


def test_loudness_sidecar_joins_by_file_name(synth: Path) -> None:
    """With rbox this probe always failed (its row .get() needed a default)."""
    conn = _raw(synth)
    conn.execute(
        "CREATE TABLE odjAnalysisScalar (ContentID INTEGER, field TEXT, value TEXT)"
    )
    conn.execute("INSERT INTO odjAnalysisScalar VALUES (3, 'loudness_lufs', '-8.5')")
    conn.commit()
    conn.close()
    sidecar, by_name, unread = read_scalar_sidecar_from_onelibrary(synth)
    assert unread == []
    assert by_name["three.mp3"]["id"] == 3
    assert sidecar == {3: {"loudness_lufs": "-8.5"}}
