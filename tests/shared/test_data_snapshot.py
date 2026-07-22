"""Tests for :mod:`scripts.data_snapshot` (pack / unpack round-trip).

Does not use the ``requires_darwin`` / ``requires_audio_stack`` custom
markers (W4 owns registering those); this module runs on every platform.
"""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from scripts import data_snapshot


def _make_data_dir(tmp_path: Path, *, with_vocal_cache: bool = True) -> Path:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)

    (state_dir / "state.db").write_bytes(b"STATE-DB-PAYLOAD")

    master = data_dir / "master.plain.db"
    conn = sqlite3.connect(master)
    conn.execute("CREATE TABLE djmdContent (ID TEXT, FolderPath TEXT)")
    conn.executemany(
        "INSERT INTO djmdContent (ID, FolderPath) VALUES (?, ?)",
        [
            ("1", "/Users/dev/Music/track1.wav"),
            ("2", "/Users/dev/Music/subdir/track2.wav"),
            ("3", "/Volumes/External/DJ/track3.wav"),
            ("4", None),
            ("5", "tidal:12345"),
        ],
    )
    conn.commit()
    conn.close()

    if with_vocal_cache:
        cache_dir = state_dir / "vocal-cache"
        cache_dir.mkdir()
        (cache_dir / "abc123.json").write_text('{"regions": []}', encoding="utf-8")

    return data_dir


def test_pack_raises_when_state_db_missing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir).mkdir()
    (data_dir / "master.plain.db").touch()
    with pytest.raises(FileNotFoundError, match="state.db"):
        data_snapshot.pack(data_dir, tmp_path / "out.tar")


def test_pack_raises_when_master_plain_db_missing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    with pytest.raises(FileNotFoundError, match="master.plain.db"):
        data_snapshot.pack(data_dir, tmp_path / "out.tar")


def test_pack_discovers_distinct_folder_path_roots(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    out = tmp_path / "snapshot.tar"

    result = data_snapshot.pack(data_dir, out)

    assert result["path_map_roots"] == ["/Users/dev", "/Volumes/External"]


def test_pack_then_unpack_round_trips_members(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    out = tmp_path / "snapshot.tar"
    data_snapshot.pack(data_dir, out)

    dest = tmp_path / "unpacked"
    result = data_snapshot.unpack(out, dest)

    assert (dest / "state" / "state.db").read_bytes() == b"STATE-DB-PAYLOAD"
    assert (dest / "master.plain.db").is_file()
    assert (dest / "state" / "vocal-cache" / "abc123.json").is_file()
    assert (dest / "path-map.json").is_file()
    assert "state/state.db" in result["members"]
    assert "master.plain.db" in result["members"]


def test_pack_without_vocal_cache_still_succeeds(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path, with_vocal_cache=False)
    out = tmp_path / "snapshot.tar"

    result = data_snapshot.pack(data_dir, out)

    member_names = {m["member"] for m in result["members"]}
    assert "state/vocal-cache" not in member_names
    assert "state/state.db" in member_names
    assert "master.plain.db" in member_names


def test_pack_never_includes_audio_bytes(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    (data_dir / "audio-should-not-be-packed.wav").write_bytes(b"RIFF....fake wav")
    out = tmp_path / "snapshot.tar"

    data_snapshot.pack(data_dir, out)

    with tarfile.open(out, "r") as tar:
        names = tar.getnames()
    assert not any(name.endswith(".wav") for name in names)


def test_pack_writes_generated_path_map_with_empty_to(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    out = tmp_path / "snapshot.tar"
    data_snapshot.pack(data_dir, out)

    with tarfile.open(out, "r") as tar:
        member = tar.extractfile("path-map.json")
        assert member is not None
        payload = json.loads(member.read())

    assert "entries" in payload
    froms = {entry["from"] for entry in payload["entries"]}
    assert froms == {"/Users/dev", "/Volumes/External"}
    for entry in payload["entries"]:
        assert entry["to"] == ""


def test_unpack_raises_when_tar_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        data_snapshot.unpack(tmp_path / "does-not-exist.tar", tmp_path / "dest")


def test_unpack_raises_when_required_member_missing(tmp_path: Path) -> None:
    incomplete_tar = tmp_path / "incomplete.tar"
    with tarfile.open(incomplete_tar, "w") as tar:
        info = tarfile.TarInfo(name="master.plain.db")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"data"))
        # state/state.db deliberately omitted.

    with pytest.raises(ValueError, match="state.db"):
        data_snapshot.unpack(incomplete_tar, tmp_path / "dest")


def test_cli_pack_and_unpack_json_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = _make_data_dir(tmp_path)
    out = tmp_path / "snapshot.tar"

    exit_code = data_snapshot.main(
        ["pack", "--data-dir", str(data_dir), "--out", str(out), "--json"]
    )
    assert exit_code == 0
    pack_output = json.loads(capsys.readouterr().out)
    assert pack_output["out"] == str(out)
    assert pack_output["path_map_roots"]

    dest = tmp_path / "unpacked-cli"
    exit_code = data_snapshot.main(
        ["unpack", "--snapshot", str(out), "--dest", str(dest), "--json"]
    )
    assert exit_code == 0
    unpack_output = json.loads(capsys.readouterr().out)
    assert unpack_output["dest"] == str(dest)
    assert (dest / "state" / "state.db").is_file()
