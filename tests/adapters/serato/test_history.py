"""Serato history sessions -> Open DJ play log (DJIMPORT-10066).

[if] a session holds 2 played entries and 1 loaded-not-played [then] 2 plays at their start times, 1 skipped not played, [else stop].
[if] a played entry's path matches no library track [then] it is listed as unmatched and not written, [else stop].
[if] the import runs twice [then] the event count is unchanged, [else stop].
[if] an entry carries an unknown field id [then] it is skipped by length and the rest is read, [else stop].
[if] an entry is truncated [then] the read stops with the file name and offset, [else stop].
[if] a committed real Serato capture is parsed [then] every entry's path, start time and played flag equal its independent oracle, [else stop].

Regression one-liners:
  - if parse_session misreads a field then imported plays carry wrong tracks or times
  - if an unknown field id stops the entry then newer Serato files import nothing
  - if a truncation hides its file and offset then a corrupt session cannot be found
  - if play ids change between runs then a rerun doubles every Serato play
  - if an NFD session path misses an NFC library path then macOS plays go unmatched
  - if a dry run writes rows then the default run is not safe
  - if import_plays accepts source opendj then imports pose as Open DJ's own plays
  - if the parser misreads Serato's real layout then the committed capture disagrees with Serato's own export
  - if every entry is unplayed then import writes no plays
  - if a session field has bad utf-16 then read.error names the file and malformed offset
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.adapters import library_import
from apps.adapters.serato import history
from apps.shared.state import db as state_db
from apps.shared.state import play_log
from apps.shared.state.sync_stamp import canonical_from

pytestmark = [pytest.mark.requirement("DJIMPORT-10066")]

START_A, START_B, START_C = 1_740_000_000, 1_740_000_400, 1_740_000_800

CAPTURES = Path(__file__).resolve().parents[2] / "fixtures" / "serato_history"

_REQUIRED_CAPTURE_KEYS = frozenset(
    {"file", "sha256", "serato_version", "os", "captured", "oracle", "entries"}
)
_REQUIRED_ENTRY_KEYS = frozenset({"index", "path", "start_s", "played"})
_FORBIDDEN_PATH_MARKERS = ("/Users/", "/home/", "\\Users\\")


def _manifest() -> list[dict[str, object]]:
    data = json.loads((CAPTURES / "manifest.json").read_text(encoding="utf-8"))
    return list(data["captures"])


def _expect_type(value: object, typ: type[object], label: str) -> None:
    if not isinstance(value, typ):
        raise TypeError(f"{label} must be {typ.__name__}")


def _validate_capture_entry(ent: object, index: int) -> None:
    if not isinstance(ent, dict):
        raise TypeError(f"entries[{index}] must be a dict")
    for key in _REQUIRED_ENTRY_KEYS:
        if key not in ent:
            raise ValueError(f"entries[{index}] missing key {key!r}")
    _expect_type(ent["index"], int, f"entries[{index}].index")
    _expect_type(ent["path"], str, f"entries[{index}].path")
    _expect_type(ent["start_s"], int, f"entries[{index}].start_s")
    _expect_type(ent["played"], bool, f"entries[{index}].played")


def _validate_capture_row(row: dict[str, object]) -> None:
    file = row["file"]
    _expect_type(file, str, "capture file")
    if not file.endswith(".session"):
        raise ValueError(f"capture file must be a .session name, got {file!r}")
    digest = row["sha256"]
    _expect_type(digest, str, "capture sha256")
    if len(digest) != 64:
        raise ValueError(f"capture sha256 must be a 64-character string, got {digest!r}")
    if digest != digest.lower() or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"capture sha256 must be lowercase hex, got {digest!r}")
    entries = row["entries"]
    _expect_type(entries, list, "capture entries")
    for i, ent in enumerate(entries):
        _validate_capture_entry(ent, i)


# Synthesized sessions: built with the parser's own field ids and framing, so they cover
# edge cases only; Serato's real format is checked against committed captures in
# tests/fixtures/serato_history/.


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "big")


def _u16(text: str) -> bytes:
    return text.encode("utf-16-be") + b"\x00\x00"


def _tag(kind: bytes, payload: bytes) -> bytes:
    return kind + _u32(len(payload)) + payload


def _field(fid: int, value: bytes) -> bytes:
    return _u32(fid) + _u32(len(value)) + value


def _oent(
    path: str | None,
    start: int | None,
    *,
    played: bool,
    extra: bytes = b"",
    include_path: bool = True,
    include_start: bool = True,
) -> bytes:
    adat = extra
    if include_path and path is not None:
        adat += _field(history.FIELD_PATH, _u16(path))
    adat += (
        _field(history.FIELD_TITLE, _u16("Title"))
        + _field(history.FIELD_ARTIST, _u16("Artist"))
    )
    if include_start and start is not None:
        adat += _field(history.FIELD_START, _u32(start))
        adat += _field(history.FIELD_END, _u32(start + 300))
    adat += (
        _field(history.FIELD_DECK, _u32(2))
        + _field(history.FIELD_PLAYED, b"\x01" if played else b"\x00")
    )
    return _tag(b"oent", _tag(b"adat", adat))


def _session(*entries: bytes) -> bytes:
    return _tag(b"vrsn", _u16("1.0/Serato Scratch LIVE Review")) + b"".join(entries)


def _serato(tmp_path: Path, files: dict[str, bytes]) -> Path:
    sessions = tmp_path / "_Serato_" / "History" / "Sessions"
    sessions.mkdir(parents=True)
    for name, data in files.items():
        (sessions / name).write_bytes(data)
    return tmp_path / "_Serato_"


def _db(tmp_path: Path, paths: dict[str, str]) -> Path:
    db = tmp_path / "state.db"
    conn = state_db.open_rw(db)
    try:
        for sid, file_path in paths.items():
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, "
                "created_at, updated_at) VALUES (?, 'inferred', 't', ?, '2026-01-01', '2026-01-01')",
                (sid, file_path),
            )
    finally:
        conn.close()
    return db


def _plays(db: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT stable_id, ts, json_extract(payload_json, '$.source') FROM events "
            "WHERE kind = 'track_played' ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def lib(tmp_path: Path) -> tuple[Path, Path, str, str]:
    a, b = str(tmp_path / "Music" / "a.mp3"), str(tmp_path / "Music" / "b.mp3")
    serato = _serato(
        tmp_path,
        {"12.session": _session(
            _oent(a, START_A, played=True),
            _oent(b, START_B, played=False),
            _oent(b, START_C, played=True),
        )},
    )
    return serato, _db(tmp_path, {"a" * 40: a, "b" * 40: b}), a, b


def test_parser_decodes_three_entries(lib: tuple[Path, Path, str, str]) -> None:
    serato, _, a, _ = lib
    read = history.read_session(history.session_files(serato)[0])
    assert read.error is None
    assert [e.played for e in read.entries] == [True, False, True]
    first = read.entries[0]
    assert (first.path, first.title, first.artist) == (a, "Title", "Artist")
    assert (first.start_s, first.end_s, first.deck) == (START_A, START_A + 300, 2)
    assert first.play_id == f"serato-12-0-{START_A}"


def test_unknown_field_is_skipped_by_length() -> None:
    data = _session(_oent("Music/x.mp3", START_A, played=True, extra=_field(99, b"\x01\x02\x03\x04\x05")))
    (entry,) = history.parse_session(data, "1.session")
    assert (entry.path, entry.start_s, entry.played) == ("Music/x.mp3", START_A, True)


def test_truncated_entry_names_file_and_offset(tmp_path: Path) -> None:
    first = _oent("Music/x.mp3", START_A, played=True)
    second = _oent("Music/y.mp3", START_B, played=True)
    head = _session(first)
    cut = head + second[:30]
    serato = _serato(tmp_path, {"7.session": cut})
    read = history.read_session(history.session_files(serato)[0])
    assert [e.path for e in read.entries] == ["Music/x.mp3"]
    assert read.error == f"7.session: truncated at offset {len(head)}: tag of {len(second) - 8} bytes"


def test_import_writes_played_entries_at_their_start_times(lib: tuple[Path, Path, str, str]) -> None:
    serato, db, _, _ = lib
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert report.skipped == {"not played": 1}
    assert report.plays_written == 2
    stamp = lambda s: canonical_from(datetime.fromtimestamp(s, UTC))  # noqa: E731
    assert _plays(db) == [("a" * 40, stamp(START_A), "serato"), ("b" * 40, stamp(START_C), "serato")]


def test_unmatched_path_is_listed_not_written(tmp_path: Path) -> None:
    ghost = str(tmp_path / "Music" / "ghost.mp3")
    serato = _serato(tmp_path, {"1.session": _session(_oent(ghost, START_A, played=True))})
    db = _db(tmp_path, {})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert report.unmatched == [ghost]
    assert _plays(db) == []


def test_nfd_session_path_matches_nfc_library_path(tmp_path: Path) -> None:
    nfc_path = unicodedata.normalize("NFC", str(tmp_path / "Café.mp3"))
    serato = _serato(
        tmp_path, {"1.session": _session(_oent(unicodedata.normalize("NFD", nfc_path), START_A, played=True))}
    )
    db = _db(tmp_path, {"c" * 40: nfc_path})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert (report.plays_written, report.unmatched) == (1, [])


def test_rerun_adds_nothing_and_dry_run_writes_nothing(lib: tuple[Path, Path, str, str]) -> None:
    serato, db, _, _ = lib
    conn = state_db.open_rw(db)
    try:
        dry = library_import.import_serato_history(conn, serato, dry_run=True)
        assert (dry.plays_written, _plays(db)) == (2, [])
        library_import.import_serato_history(conn, serato, dry_run=False)
        again = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert (again.plays_written, again.plays_already_logged) == (0, 2)
    assert len(_plays(db)) == 2


def test_cli_writes_and_reports_truncation(
    lib: tuple[Path, Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    serato, db, _, _ = lib
    argv = ["serato-history", "--source", str(serato), "--db", str(db), "--write"]
    assert library_import.main(argv) == 0
    assert len(_plays(db)) == 2
    (serato / "History" / "Sessions" / "13.session").write_bytes(_session() + b"oent\x00")
    assert library_import.main(argv) == 1
    assert "13.session: truncated at offset" in capsys.readouterr().err


def test_import_multiple_played_entries_in_file_order(tmp_path: Path) -> None:
    """[if] a session has 3 played entries [then] 3 plays import in file order, [else stop]."""
    a, b, c = (
        str(tmp_path / "Music" / "a.mp3"),
        str(tmp_path / "Music" / "b.mp3"),
        str(tmp_path / "Music" / "c.mp3"),
    )
    serato = _serato(
        tmp_path,
        {
            "multi.session": _session(
                _oent(a, START_A, played=True),
                _oent(b, START_B, played=True),
                _oent(c, START_C, played=True),
            )
        },
    )
    db = _db(tmp_path, {"a" * 40: a, "b" * 40: b, "c" * 40: c})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert report.plays_written == 3
    stamp = lambda s: canonical_from(datetime.fromtimestamp(s, UTC))  # noqa: E731
    assert _plays(db) == [
        ("a" * 40, stamp(START_A), "serato"),
        ("b" * 40, stamp(START_B), "serato"),
        ("c" * 40, stamp(START_C), "serato"),
    ]


def test_import_skips_all_unplayed_entries(tmp_path: Path) -> None:
    """[if] every session entry is unplayed [then] import writes zero plays, [else stop]."""
    paths = [str(tmp_path / "Music" / f"{i}.mp3") for i in range(3)]
    serato = _serato(
        tmp_path,
        {
            "unplayed.session": _session(
                *(_oent(p, START_A + i * 100, played=False) for i, p in enumerate(paths))
            )
        },
    )
    db = _db(tmp_path, {f"{i:040x}": p for i, p in enumerate(paths)})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert (report.plays_written, report.skipped, report.errors) == (0, {"not played": 3}, [])
    assert _plays(db) == []


def test_import_skips_played_entry_without_start_time(tmp_path: Path) -> None:
    """[if] a played entry has no start field [then] skip as no start time, [else stop]."""
    track = str(tmp_path / "Music" / "a.mp3")
    serato = _serato(
        tmp_path,
        {"nostart.session": _session(_oent(track, None, played=True, include_start=False))},
    )
    db = _db(tmp_path, {"a" * 40: track})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert (report.plays_written, report.skipped) == (0, {"no start time": 1})


def test_import_skips_played_entry_without_path(tmp_path: Path) -> None:
    """[if] a played entry has no path field [then] skip as no path, [else stop]."""
    serato = _serato(
        tmp_path,
        {"nopath.session": _session(_oent(None, START_A, played=True, include_path=False))},
    )
    db = _db(tmp_path, {})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert (report.plays_written, report.skipped) == (0, {"no path": 1})


def test_import_empty_session_writes_nothing(tmp_path: Path) -> None:
    """[if] a session file has zero oent tags [then] import sees no entries, [else stop]."""
    serato = _serato(tmp_path, {"empty.session": _session()})
    db = _db(tmp_path, {})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert (report.entries_seen, report.plays_written, report.errors) == (0, 0, [])


def test_malformed_utf16_path_reports_read_error(tmp_path: Path) -> None:
    """[if] a path field has invalid utf-16 [then] read.error names malformed, [else stop]."""
    adat = (
        _field(history.FIELD_PATH, b"\x00a\x00")
        + _field(history.FIELD_START, _u32(START_A))
        + _field(history.FIELD_PLAYED, b"\x01")
    )
    bad = _session(_tag(b"oent", _tag(b"adat", adat)))
    serato = _serato(tmp_path, {"bad.session": bad})
    session_path = history.session_files(serato)[0]
    read = history.read_session(session_path)
    assert read.entries == ()
    assert read.error is not None
    assert "bad.session" in read.error
    assert "malformed field" in read.error
    db = _db(tmp_path, {})
    conn = state_db.open_rw(db)
    try:
        report = library_import.import_serato_history(conn, serato, dry_run=False)
    finally:
        conn.close()
    assert report.errors == [read.error]
    argv = ["serato-history", "--source", str(serato), "--db", str(db), "--write"]
    assert library_import.main(argv) == 1


def test_capture_manifest_rejects_malformed_oracle_row() -> None:
    """[if] a manifest oracle row has wrong types [then] validation fails, [else stop]."""
    with pytest.raises(ValueError, match="sha256"):
        _validate_capture_row(
            {
                "file": "x.session",
                "sha256": "not-hex",
                "entries": [{"index": 0, "path": "p", "start_s": 1, "played": True}],
            }
        )
    with pytest.raises(TypeError, match="played must be bool"):
        _validate_capture_row(
            {
                "file": "y.session",
                "sha256": "a" * 64,
                "entries": [{"index": 0, "path": "p", "start_s": 1, "played": "yes"}],
            }
        )


def test_import_plays_refuses_opendj_source(tmp_path: Path) -> None:
    conn = state_db.open_rw(_db(tmp_path, {}))
    try:
        with pytest.raises(ValueError):
            play_log.import_plays(conn, [], source=play_log.OPENDJ_SOURCE, actor="t")
    finally:
        conn.close()


def test_capture_manifest_pins_every_committed_session() -> None:
    """[if] manifest lists every session file on disk [then] orphans fail, [else stop]."""
    manifests = _manifest()
    on_disk = {p.name for p in CAPTURES.glob("*.session")}
    assert {str(c["file"]) for c in manifests} == on_disk
    for cap in manifests:
        assert set(cap) >= _REQUIRED_CAPTURE_KEYS
        _validate_capture_row(cap)
        entries = cap["entries"]
        assert isinstance(entries, list) and entries
        for i, row in enumerate(entries):
            assert isinstance(row, dict)
            assert row["index"] == i
            path = str(row["path"])
            assert not any(marker in path for marker in _FORBIDDEN_PATH_MARKERS)
        session_path = CAPTURES / str(cap["file"])
        assert session_path.is_file()
        digest = hashlib.sha256(session_path.read_bytes()).hexdigest()
        assert digest == cap["sha256"]


def test_committed_captures_parse_to_their_oracle(tmp_path: Path) -> None:
    """[if] a committed capture exists [then] parser and import match the oracle, [else stop]."""
    captures = _manifest()
    if not captures:
        pytest.skip(
            "UNAVAILABLE: no captured Serato .session is committed under "
            "tests/fixtures/serato_history (see its README); Serato's real format is "
            "unmeasured here. This is a capability report, not a pass."
        )
    for cap_i, cap in enumerate(captures):
        session_path = CAPTURES / str(cap["file"])
        read = history.read_session(session_path)
        assert read.error is None
        oracle_entries = cap["entries"]
        assert isinstance(oracle_entries, list)
        parsed = [(e.index, e.path, e.start_s, e.played) for e in read.entries]
        expected = [
            (x["index"], x["path"], x["start_s"], x["played"]) for x in oracle_entries
        ]
        assert parsed == expected

        played_paths = {
            str(x["path"])
            for x in oracle_entries
            if x["played"] is True
        }
        cap_root = tmp_path / f"cap{cap_i}"
        serato_root = cap_root / "_Serato_"
        sessions = serato_root / "History" / "Sessions"
        sessions.mkdir(parents=True)
        (sessions / str(cap["file"])).write_bytes(session_path.read_bytes())
        resolve = library_import.serato_path_resolver(serato_root)
        db_paths = {
            f"{j:040x}": unicodedata.normalize("NFC", resolve(p))
            for j, p in enumerate(sorted(played_paths))
        }
        db = _db(cap_root, db_paths)
        conn = state_db.open_rw(db)
        try:
            report = library_import.import_serato_history(conn, serato_root, dry_run=False)
        finally:
            conn.close()
        played_count = sum(1 for x in oracle_entries if x["played"] is True)
        unplayed_count = sum(1 for x in oracle_entries if x["played"] is False)
        assert report.plays_written == played_count
        assert report.skipped.get("not played", 0) == unplayed_count
