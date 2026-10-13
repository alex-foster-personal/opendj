"""Writing the chosen genre into audio files, with backup and undo (GENRE-05).

- [if] write_file_tags is off or the file is outside write_roots [then] the write is refused and the file bytes are unchanged, [else stop].
- [if] a write is allowed [then] the file's genre tag reads back as the chosen genre and every other byte of audio is kept, [else stop].
- [if] undo runs [then] the exact original bytes come back; if the file changed since, undo refuses, [else stop].
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.genre_infer import findings, service, tag_write
from apps.shared.flac_meta import FlacError
from apps.shared.tag_writer import read_tags
from tests.genre_infer.pipeline_support import library

pytestmark = pytest.mark.requirement("GENRE-05")

SOURCES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup(tmp_path: Path, name: str) -> tuple[sqlite3.Connection, Path, Path]:
    copy_dir = tmp_path / "copies"
    copy_dir.mkdir()
    audio = copy_dir / name
    shutil.copy2(SOURCES / name, audio)
    data, state = library(tmp_path, audio=audio)
    conn = sqlite3.connect(str(state))
    findings.ensure_schema(conn)
    service.attach_finding(
        conn,
        stable_id="t-copy",
        provider="beatport",
        genre="Tech House",
        confidence=1.0,
        source="https://example.test/1",
    )
    return conn, data, audio


@pytest.mark.parametrize("name", ["src-320.mp3", "src.flac", "src.m4a"])
def test_write_reads_back_and_undo_restores_the_exact_bytes(tmp_path: Path, name: str) -> None:
    conn, data, audio = _setup(tmp_path, name)
    before = _sha(audio)
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    out = service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    assert read_tags(audio).genre == "Tech House"
    assert out["written_genre"] == "Tech House" and Path(str(out["backup_path"])).is_file()
    assert _sha(Path(str(out["backup_path"]))) == before
    entry = tag_write.journal(conn, "t-copy")[0]
    assert entry["provider"] == "beatport" and entry["undone_at"] is None
    tag_write.undo(conn, int(entry["id"]))
    assert _sha(audio) == before
    with pytest.raises(tag_write.TagWriteRefused, match="already undone"):
        tag_write.undo(conn, int(entry["id"]))


def test_refused_when_off_and_when_outside_the_roots(tmp_path: Path) -> None:
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    before = _sha(audio)
    with pytest.raises(tag_write.TagWriteRefused, match="write_file_tags is off"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    findings.update_config(conn, {"write_file_tags": True})
    with pytest.raises(tag_write.TagWriteRefused, match="write_roots is empty"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    findings.update_config(conn, {"write_roots": [str(tmp_path / "elsewhere")]})
    with pytest.raises(tag_write.TagWriteRefused, match="outside every configured write root"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    assert _sha(audio) == before
    assert tag_write.journal(conn) == []


def test_undo_refuses_when_the_file_changed_after_the_write(tmp_path: Path) -> None:
    conn, data, audio = _setup(tmp_path, "src.flac")
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    with audio.open("ab") as fh:
        fh.write(b"\0")
    entry = tag_write.journal(conn)[0]
    with pytest.raises(tag_write.TagWriteRefused, match="changed after write"):
        tag_write.undo(conn, int(entry["id"]))


def test_the_library_tag_is_never_written_back(tmp_path: Path) -> None:
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    findings.update_config(
        conn, {"write_file_tags": True, "write_roots": [str(audio.parent)], "precedence": ["library-tag"]}
    )
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES ('t-copy', 'genre', '\"Disco\"', 'inferred', 0.7, '2026-10-12T00:00:00Z')"
    )
    with pytest.raises(tag_write.TagWriteRefused, match="already the library's own tag"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")


def test_a_journal_that_cannot_be_written_puts_the_file_back(tmp_path: Path) -> None:
    """[if] the undo journal row cannot be stored [then] the audio file gets its original bytes back, [else stop]."""
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    before = _sha(audio)
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    tag_write._ensure_journal_columns(conn)
    conn.execute(
        "CREATE TRIGGER refuse_journal BEFORE INSERT ON genre_tag_writes BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    assert _sha(audio) == before


def test_the_cli_writes_backups_under_a_flat_layout_data_dir(tmp_path: Path) -> None:
    """[if] the CLI targets a flat-layout library [then] backups land under that data dir, [else stop]."""
    from apps.genre_infer import __main__ as cli

    conn, _data, audio = _setup(tmp_path, "src-320.mp3")
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    conn.commit()
    conn.close()
    nested = tmp_path / "data" / "state" / "state.db"
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "library.db").write_bytes(nested.read_bytes())
    rc = cli.main(["--data-dir", str(flat), "--state-db", str(flat / "library.db"), "write-tag", "t-copy"])
    assert rc == 0
    assert list((flat / "state" / "genre" / "tag-backups").rglob("*.mp3")), (
        "backup was not written under the flat data dir"
    )
    assert read_tags(audio).genre == "Tech House"


def test_an_undo_that_cannot_be_journaled_leaves_the_tagged_file(tmp_path: Path) -> None:
    """[if] the undo cannot be recorded [then] the file keeps its tagged bytes and undo can be retried, [else stop]."""
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    original = _sha(audio)
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    tagged = _sha(audio)
    entry = tag_write.journal(conn)[0]
    conn.execute("CREATE TRIGGER refuse_undo BEFORE UPDATE ON genre_tag_writes BEGIN SELECT RAISE(ABORT, 'busy'); END")
    with pytest.raises(sqlite3.IntegrityError, match="busy"):
        tag_write.undo(conn, int(entry["id"]))
    assert _sha(audio) == tagged
    assert sorted(p.name for p in audio.parent.iterdir()) == [audio.name], "a staging file was left behind"
    conn.execute("DROP TRIGGER refuse_undo")
    tag_write.undo(conn, int(entry["id"]))
    assert _sha(audio) == original


def test_a_truncated_backup_is_rewritten_not_trusted(tmp_path: Path) -> None:
    """[if] a backup at the content-hash path is incomplete [then] it is rewritten before the write, [else stop]."""
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    original = _sha(audio)
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    from datetime import UTC, datetime

    backup_dir = data / "state" / "genre" / "tag-backups" / datetime.now(UTC).strftime("%Y-%m-%d")
    backup_dir.mkdir(parents=True)
    (backup_dir / f"{original}.mp3").write_bytes(b"half")
    out = service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    assert _sha(Path(str(out["backup_path"]))) == original


def test_write_and_undo_are_refused_while_a_blocking_app_runs(tmp_path: Path) -> None:
    """[if] a blocking app is running [then] the write and the undo are both refused and the file is untouched, [else stop]."""
    import psutil

    # A real running process, found by the real scan: this test's own interpreter,
    # named as the blocking app for these calls only (a fake "rekordbox" process
    # would make every concurrent tag write on the host refuse).
    running = (psutil.Process().name().lower(),)
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    original = _sha(audio)
    assert tag_write.BLOCKING_APPS == ("rekordbox", "djay")
    with pytest.raises(tag_write.TagWriteRefused, match="is running"):
        tag_write.write_genre(
            conn,
            data_dir=data,
            stable_id="t-copy",
            path=audio,
            genre="House",
            provider="beatport",
            blocking_apps=running,
        )
    assert _sha(audio) == original
    service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    tagged = _sha(audio)
    with pytest.raises(tag_write.TagWriteRefused, match="is running"):
        tag_write.undo(conn, int(tag_write.journal(conn)[0]["id"]), blocking_apps=running)
    assert _sha(audio) == tagged


def test_raw_aac_is_refused_not_guessed(tmp_path: Path) -> None:
    """[if] the file is a raw .aac [then] the write is refused as unsupported, [else stop]."""
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    raw = audio.with_suffix(".aac")
    audio.rename(raw)
    conn.execute("UPDATE tracks SET file_path = ? WHERE stable_id = 't-copy'", (str(raw),))
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(raw.parent)]})
    with pytest.raises(tag_write.TagWriteRefused, match="not supported"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")


def test_a_write_the_container_writer_rejects_leaves_the_original_file(tmp_path: Path) -> None:
    """[if] the real FLAC writer fails on a damaged file [then] its bytes stay, no staging file is left and nothing is journaled, [else stop]."""
    conn, data, audio = _setup(tmp_path, "src.flac")
    audio.write_bytes((SOURCES / "src.flac").read_bytes()[:200])  # readable header, damaged body
    before = _sha(audio)
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    with pytest.raises(FlacError):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    assert _sha(audio) == before
    assert sorted(p.name for p in audio.parent.iterdir()) == [audio.name]
    assert tag_write.journal(conn) == []


def test_a_journaled_write_that_never_landed_is_undone_without_touching_the_file(tmp_path: Path) -> None:
    """[if] a crash left a journal row but the original bytes (no rename) [then] undo marks it undone and leaves the file, [else stop]."""
    conn, data, audio = _setup(tmp_path, "src-320.mp3")
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(audio.parent)]})
    original = audio.read_bytes()
    service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    audio.write_bytes(original)  # the state a crash between journal commit and rename leaves
    entry = tag_write.journal(conn)[0]
    tag_write.undo(conn, int(entry["id"]))
    assert audio.read_bytes() == original
    assert tag_write.journal(conn)[0]["undone_at"] is not None
