"""Virtual edits are bounded metadata and never source mutations.

[if] an edit changes source truth [then] the acceptance contract fails, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import library_edits, library_org
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("LIBEDIT-9179")


@pytest.fixture
def edit_store(tmp_path: Path) -> tuple[StateWriter, Path]:
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"source-audio-must-not-change")
    conn = state_db.open_rw(tmp_path / "state.db")
    (tmp_path / "machine-id").write_text("a" * 32, encoding="utf-8")
    writer = StateWriter(conn, FakeEventBus(), actor="test")
    writer.upsert_track(
        stable_id="source-1",
        stable_id_tier="inferred",
        title="Source",
        artists=["Artist"],
        album=None,
        isrc=None,
        duration_ms=10_000,
        file_path=str(audio),
    )
    yield writer, audio
    writer.close()
    conn.close()


def _source_row(writer: StateWriter) -> tuple[object, ...]:
    return tuple(
        writer.raw_conn.execute(
            "SELECT stable_id, updated_at, file_path, duration_ms FROM tracks WHERE stable_id = 'source-1'"
        ).fetchone()
    )


def test_create_and_delete_never_touch_the_source_row_or_audio(
    edit_store: tuple[StateWriter, Path],
) -> None:
    writer, audio = edit_store
    before_row = _source_row(writer)
    before_bytes = audio.read_bytes()
    before_stat = audio.stat()

    made = library_edits.create_edit(
        writer,
        "test-session",
        source_track_id="source-1",
        label="Radio cut",
        in_ms=1_000,
        out_ms=8_000,
        fx={"echo": 0.2},
        created_by="tester",
    )

    assert made.row["fx"] == {"echo": 0.2}
    assert made.row["valid"] is True
    assert _source_row(writer) == before_row
    assert audio.read_bytes() == before_bytes
    assert audio.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert audio.stat().st_size == before_stat.st_size

    library_edits.delete_edit(writer, "test-session", str(made.row["id"]))
    assert _source_row(writer) == before_row
    assert audio.read_bytes() == before_bytes
    assert library_edits.list_edits(writer.raw_conn) == []


@pytest.mark.parametrize(
    ("source", "in_ms", "out_ms", "code"),
    [
        ("source-1", 2_000, 2_000, "VALIDATION"),
        ("source-1", 3_000, 2_000, "VALIDATION"),
        ("source-1", -1, 2_000, "VALIDATION"),
        ("source-1", 0, 10_001, "VALIDATION"),
        ("missing", 0, 1_000, "NOT_FOUND"),
    ],
)
def test_invalid_windows_and_missing_sources_are_refused(
    edit_store: tuple[StateWriter, Path],
    source: str,
    in_ms: int,
    out_ms: int,
    code: str,
) -> None:
    writer, _audio = edit_store
    with pytest.raises(library_org.OrgError) as caught:
        library_edits.create_edit(
            writer,
            "test-session",
            source_track_id=source,
            label="No",
            in_ms=in_ms,
            out_ms=out_ms,
            created_by="tester",
        )
    assert caught.value.code == code


def test_reanalysis_marks_an_edit_invalid_without_rewriting_it(
    edit_store: tuple[StateWriter, Path],
) -> None:
    writer, audio = edit_store
    made = library_edits.create_edit(
        writer,
        "test-session",
        source_track_id="source-1",
        label="Long cut",
        in_ms=1_000,
        out_ms=9_000,
        created_by="tester",
    )
    before = writer.raw_conn.execute(
        "SELECT updated_at, in_ms, out_ms FROM track_edit WHERE id = ?",
        (made.row["id"],),
    ).fetchone()
    writer.upsert_track(
        stable_id="source-1",
        stable_id_tier="inferred",
        title="Source",
        artists=["Artist"],
        album=None,
        isrc=None,
        duration_ms=7_500,
        file_path=str(audio),
    )
    after = writer.raw_conn.execute(
        "SELECT updated_at, in_ms, out_ms FROM track_edit WHERE id = ?",
        (made.row["id"],),
    ).fetchone()
    assert after == before
    assert library_edits.list_edits(writer.raw_conn)[0]["valid"] is False


def test_playlist_creation_inserts_the_edit_membership_after_its_source(
    edit_store: tuple[StateWriter, Path],
) -> None:
    writer, _audio = edit_store
    writer.insert_playlist(
        playlist_id="playlist-1",
        name="Set",
        vendor="local",
        vendor_pl_id="playlist-1",
    )
    writer.set_playlist_memberships("playlist-1", ["source-1"])
    made = library_edits.create_edit(
        writer,
        "test-session",
        source_track_id="source-1",
        label="Set cut",
        in_ms=1_000,
        out_ms=8_000,
        created_by="tester",
        playlist_id="playlist-1",
    )
    rows = writer.raw_conn.execute(
        "SELECT stable_id, edit_id FROM playlist_memberships "
        "WHERE playlist_id = 'playlist-1' AND deleted_at IS NULL "
        "ORDER BY order_key, position"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("source-1", None),
        ("source-1", made.row["id"]),
    ]


def test_relocating_a_copy_preserves_the_edit_and_source_cue(
    edit_store: tuple[StateWriter, Path], tmp_path: Path
) -> None:
    writer, _audio = edit_store
    made = library_edits.create_edit(
        writer,
        "test-session",
        source_track_id="source-1",
        label="Cue cut",
        in_ms=500,
        out_ms=4_000,
        created_by="tester",
    )
    writer.set_field(
        "source-1",
        "notes",
        {"cue": 500},
        source="webui",
        modified_at="2026-01-01T00:00:00Z",
    )
    writer.upsert_track_location(
        stable_id="source-1",
        kind="local",
        file_path=str(tmp_path / "relocated.wav"),
        role="primary",
    )
    row = library_edits.list_edits(writer.raw_conn)[0]
    assert (row["id"], row["in_ms"], row["out_ms"]) == (
        made.row["id"],
        500,
        4_000,
    )
    cue = writer.raw_conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = 'source-1' AND field_name = 'notes'"
    ).fetchone()[0]
    assert json.loads(cue) == {"cue": 500}
