"""Unit tests for the rekordbox-ingestion helper. Real files in tmp_path, no mocks.

Each test name reads as "if <behavior> breaks, this fails".

[if] a rekordbox-ingestion helper behavior regresses [then] fail, [else stop].
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.rekordbox_ingestion import cli, playlists, stage, status

pytestmark = pytest.mark.requirement("INBOX-7771")
from apps.rekordbox_ingestion.desktop import TouchTracker

# ----- completion signal ---------------------------------------------------------


def _anlz(share: Path, rel: str, suffixes: tuple[str, ...], size: int = 10) -> str:
    dat = share / rel.lstrip("/")
    dat.parent.mkdir(parents=True, exist_ok=True)
    for suffix in suffixes:
        dat.with_suffix(suffix).write_bytes(b"x" * size)
    return rel


def test_classify_reports_missing_when_no_row(tmp_path: Path) -> None:
    assert status.classify(None, tmp_path) == ("MISSING", [])


def test_classify_reports_imported_for_an_unanalyzed_row(tmp_path: Path) -> None:
    row = status.TrackRow("1", "/a.mp3", analysed=0, analysis_data_path="")
    assert status.classify(row, tmp_path) == ("IMPORTED", [".DAT", ".EXT", ".2EX"])


def test_classify_reports_analyzed_only_with_flag_path_and_all_anlz_files(tmp_path: Path) -> None:
    rel = _anlz(tmp_path, "/PIONEER/USBANLZ/abc/uuid/ANLZ0000.DAT", (".DAT", ".EXT", ".2EX"))
    row = status.TrackRow("1", "/a.mp3", analysed=105, analysis_data_path=rel)
    assert status.classify(row, tmp_path) == ("ANALYZED", [])


def test_classify_keeps_imported_while_an_anlz_file_is_missing_or_empty(tmp_path: Path) -> None:
    rel = _anlz(tmp_path, "/PIONEER/USBANLZ/abc/uuid/ANLZ0000.DAT", (".DAT", ".EXT"))
    (tmp_path / rel.lstrip("/")).with_suffix(".2EX").write_bytes(b"")
    row = status.TrackRow("1", "/a.mp3", analysed=105, analysis_data_path=rel)
    assert status.classify(row, tmp_path) == ("IMPORTED", [".2EX"])


def test_classify_keeps_imported_when_files_exist_but_flag_is_zero(tmp_path: Path) -> None:
    rel = _anlz(tmp_path, "/PIONEER/USBANLZ/abc/uuid/ANLZ0000.DAT", (".DAT", ".EXT", ".2EX"))
    row = status.TrackRow("1", "/a.mp3", analysed=0, analysis_data_path=rel)
    assert status.classify(row, tmp_path)[0] == "IMPORTED"


def test_in_rekordbox_count_sums_imported_and_analyzed() -> None:
    st = status.BatchStatus(
        batch_dir="/b",
        denominator=3,
        counts={"MISSING": 1, "IMPORTED": 1, "ANALYZED": 1},
        in_playlist_count=0,
        playlists={},
        files=[],
    )
    assert st.in_rekordbox_count == 2


def test_batch_denominator_counts_only_visible_audio(tmp_path: Path) -> None:
    for name in ("a.mp3", "b.WAV", ".hidden.mp3", "notes.txt", ".batch.json"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in status.list_batch_audio(tmp_path)] == ["a.mp3", "b.WAV"]


def test_missing_batch_dir_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        status.list_batch_audio(tmp_path / "nope")


# ----- quit decision -------------------------------------------------------------


def test_fresh_tracker_never_authorizes_a_quit() -> None:
    tracker = TouchTracker(idle_minutes=5, started_at=1000.0)
    ok, _ = tracker.may_quit(1000.0 + 60, "Finder", hid_idle=600)
    assert not ok


def test_no_quit_while_rekordbox_is_frontmost_and_in_use() -> None:
    tracker = TouchTracker(idle_minutes=5, started_at=0.0)
    ok, reason = tracker.may_quit(1000.0, "rekordbox", hid_idle=10)
    assert not ok and "frontmost" in reason


def test_no_quit_within_the_window_after_a_touch() -> None:
    tracker = TouchTracker(idle_minutes=5, started_at=0.0)
    tracker.observe(1000.0, "rekordbox", hid_idle=5, poll_seconds=60)
    ok, _ = tracker.may_quit(1000.0 + 120, "Finder", hid_idle=1)
    assert not ok


def test_quit_allowed_after_the_window_with_maintainer_elsewhere() -> None:
    tracker = TouchTracker(idle_minutes=5, started_at=0.0)
    tracker.observe(1000.0, "rekordbox", hid_idle=5, poll_seconds=60)
    ok, _ = tracker.may_quit(1000.0 + 400, "Finder", hid_idle=1)
    assert ok


def test_rekordbox_frontmost_but_idle_does_not_count_as_a_touch() -> None:
    tracker = TouchTracker(idle_minutes=5, started_at=0.0)
    tracker.observe(1000.0, "rekordbox", hid_idle=900, poll_seconds=60)
    assert tracker.last_touch_at is None


# ----- staging -------------------------------------------------------------------


def test_stage_refuses_an_empty_source_before_copying_anything(tmp_path: Path) -> None:
    good, empty = tmp_path / "good.mp3", tmp_path / "empty.mp3"
    good.write_bytes(b"x" * 2048)
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        stage.plan_stage([good, empty], tmp_path / "batch")
    assert not (tmp_path / "batch").exists()


def test_stage_refuses_a_same_size_file_with_different_bytes(tmp_path: Path) -> None:
    source = tmp_path / "in" / "a.mp3"
    source.parent.mkdir()
    source.write_bytes(b"AAAA")
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "a.mp3").write_bytes(b"BBBB")
    with pytest.raises(ValueError, match="different file already at"):
        stage.plan_stage([source], batch)


def test_stage_refuses_two_sources_whose_names_differ_only_in_case(tmp_path: Path) -> None:
    first, second = tmp_path / "disc1" / "Track.mp3", tmp_path / "disc2" / "track.mp3"
    for source in (first, second):
        source.parent.mkdir()
        source.write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="two sources share the name"):
        stage.plan_stage([first, second], tmp_path / "batch")


def test_stage_dry_run_copies_nothing(tmp_path: Path, capsys) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.mp3").write_bytes(b"x" * 2048)
    batch = tmp_path / "batch"
    assert cli.main(["stage", "--dir", str(batch), "--from-dir", str(src), "--dry-run"]) == 0
    assert not batch.exists()
    assert "copy 1" in capsys.readouterr().out


def test_stage_live_copies_and_leaves_the_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.mp3").write_bytes(b"y" * 4096)
    batch = tmp_path / "batch"
    assert cli.main(["stage", "--dir", str(batch), "--from-dir", str(src), "--live"]) == 0
    assert (batch / "a.mp3").read_bytes() == b"y" * 4096
    assert (src / "a.mp3").exists()


def test_mutating_command_without_a_mode_refuses(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["stage", "--dir", str(tmp_path / "b"), "--from-dir", str(tmp_path)])


# ----- masterPlaylists6.xml append -------------------------------------------------

_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n\r\n<MASTER_PLAYLIST Version="3.0.0" AutomaticSync="0">\r\n'
    '  <PRODUCT Name="rekordbox" Version="6.8.2" Company="Pioneer DJ"/>\r\n  <PLAYLISTS>\r\n'
    '    <NODE Id="A" ParentId="0" Attribute="0" Timestamp="1" Lib_Type="0" CheckType="0"/>\r\n'
    "  </PLAYLISTS>\r\n</MASTER_PLAYLIST>\r\n"
)


def test_xml_append_keeps_crlf_and_every_existing_node(tmp_path: Path) -> None:
    xml = tmp_path / "masterPlaylists6.xml"
    xml.write_bytes(_XML.encode())
    before = playlists._xml_nodes(xml)
    node = {"Id": "B", "ParentId": "0", "Attribute": "1", "Timestamp": "2", "Lib_Type": "0", "CheckType": "0"}
    playlists._append_xml_nodes(xml, [node])
    raw = xml.read_bytes().decode()
    assert raw.count("\r\n") == _XML.count("\r\n") + 1 and "\n" not in raw.replace("\r\n", "")
    playlists._assert_xml_only_grew(before, playlists._xml_nodes(xml), [node])


def test_xml_guard_catches_a_changed_existing_node(tmp_path: Path) -> None:
    before = {"A": {"Id": "A", "Timestamp": "1"}}
    after = {"A": {"Id": "A", "Timestamp": "999"}}
    with pytest.raises(playlists.VerificationFailed):
        playlists._assert_xml_only_grew(before, after, [])


def test_holder_probe_refuses_a_missing_db_instead_of_listing_every_open_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        playlists.db_set_holders(tmp_path / "nope.db")


def test_windows_holder_probe_sees_this_process_through_psutil(tmp_path: Path) -> None:
    db = tmp_path / "master.db"
    db.write_bytes(b"x")
    with db.open("rb"):
        holders = playlists._db_set_holders_via_psutil([str(db)])
    assert any(h.endswith(f"({os.getpid()})") for h in holders)
    assert playlists._db_set_holders_via_psutil([str(db)]) == []


def test_holder_probe_sees_a_process_holding_the_file(tmp_path: Path) -> None:
    db = tmp_path / "master.db"
    db.write_bytes(b"x")
    with db.open("rb"):
        # Match this process by pid: its command name is "python" under
        # `python -m pytest` but "pytest" under the CI shards' entrypoint.
        assert any(h.endswith(f"({os.getpid()})") for h in playlists.db_set_holders(db))
    assert playlists.db_set_holders(db) == []


def test_ingestion_behavior_is_registered_in_reqs_json() -> None:
    """[if] INBOX-7771 is missing from reqs.json [then] the CLI ships outside the requirements contract."""
    payload = json.loads((Path(__file__).resolve().parents[2] / "reqs.json").read_text())
    ids = {
        req["id"]
        for bucket in payload.values()
        if isinstance(bucket, dict)
        for category in bucket.values()
        if isinstance(category, dict)
        for req in category.get("requirements", [])
    }
    assert "INBOX-7771" in ids
