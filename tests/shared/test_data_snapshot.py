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


@pytest.mark.parametrize("member", ["state/state.db", "master.plain.db"])
def test_pack_rejects_symlinked_required_members_before_creating_tar(
    tmp_path: Path, member: str
) -> None:
    data_dir = _make_data_dir(tmp_path, with_vocal_cache=False)
    required_path = data_dir / member
    target = tmp_path / f"real-{required_path.name}"
    required_path.replace(target)
    try:
        required_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    out = tmp_path / "out.tar"

    with pytest.raises(ValueError, match="regular file"):
        data_snapshot.pack(data_dir, out)

    assert not out.exists()


def test_pack_checks_cache_size_before_reading_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = _make_data_dir(tmp_path, with_vocal_cache=False)
    cache_dir = data_dir / "state" / "vocal-cache"
    cache_dir.mkdir()
    oversized = cache_dir / "oversized.json"
    oversized.write_bytes(b'{"oversized": true}')
    monkeypatch.setattr(data_snapshot, "MAX_JSON_MEMBER_BYTES", 4)
    real_read_bytes = Path.read_bytes
    read_paths: list[Path] = []

    def spy_read_bytes(path: Path) -> bytes:
        read_paths.append(path)
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    out = tmp_path / "out.tar"

    with pytest.raises(ValueError, match="JSON member byte limit"):
        data_snapshot.pack(data_dir, out)

    assert read_paths == []
    assert not out.exists()


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


def test_pack_rejects_non_cache_payloads(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    (data_dir / "state" / "vocal-cache" / "secret.env").write_text("token=secret")
    with pytest.raises(ValueError, match="only regular .json"):
        data_snapshot.pack(data_dir, tmp_path / "out.tar")


def test_unpack_rejects_unexpected_and_traversal_members(tmp_path: Path) -> None:
    snapshot = tmp_path / "hostile.tar"
    with tarfile.open(snapshot, "w") as tar:
        for name in data_snapshot.REQUIRED_MEMBERS:
            info = tarfile.TarInfo(name=name)
            info.size = 2
            tar.addfile(info, io.BytesIO(b"db"))
        secret = tarfile.TarInfo(name="state/vocal-cache/secret.env")
        secret.size = 5
        tar.addfile(secret, io.BytesIO(b"token"))
        escape = tarfile.TarInfo(name="../../outside")
        escape.size = 4
        tar.addfile(escape, io.BytesIO(b"nope"))

    dest = tmp_path / "dest"
    with pytest.raises(ValueError, match="unsupported"):
        data_snapshot.unpack(snapshot, dest)
    assert not (tmp_path / "outside").exists()


def _write_required_snapshot(
    snapshot: Path,
    *,
    extra_members: list[tuple[str, bytes]] | None = None,
) -> None:
    with tarfile.open(snapshot, "w") as tar:
        for name in data_snapshot.REQUIRED_MEMBERS:
            payload = b"db"
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        for name, payload in extra_members or []:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize(
    "alias",
    [
        r"state\state.db",
        r"state\vocal-cache\entry.json",
        r"state/vocal-cache\entry.json",
    ],
)
def test_unpack_rejects_windows_backslash_member_aliases(
    tmp_path: Path, alias: str
) -> None:
    snapshot = tmp_path / "backslash-alias.tar"
    _write_required_snapshot(snapshot, extra_members=[(alias, b"{}")])
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="backslash"):
        data_snapshot.unpack(snapshot, dest)

    assert not dest.exists()


def test_unpack_rejects_archive_over_byte_cap_before_opening_tar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "oversized.tar"
    snapshot.write_bytes(b"x" * 17)
    monkeypatch.setattr(data_snapshot, "MAX_ARCHIVE_BYTES", 16)

    with pytest.raises(ValueError, match="archive byte limit"):
        data_snapshot.unpack(snapshot, tmp_path / "dest")


def test_unpack_rejects_member_count_cap_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "too-many.tar"
    _write_required_snapshot(snapshot)
    monkeypatch.setattr(data_snapshot, "MAX_MEMBER_COUNT", 1)
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="member count limit"):
        data_snapshot.unpack(snapshot, dest)

    assert not dest.exists()


def test_unpack_rejects_member_byte_cap_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "member-too-large.tar"
    _write_required_snapshot(snapshot)
    monkeypatch.setattr(data_snapshot, "MAX_MEMBER_BYTES", 1)
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="member byte limit"):
        data_snapshot.unpack(snapshot, dest)

    assert not dest.exists()


def test_unpack_rejects_total_member_byte_cap_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "total-too-large.tar"
    _write_required_snapshot(snapshot)
    monkeypatch.setattr(data_snapshot, "MAX_TOTAL_MEMBER_BYTES", 3)
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="total member byte limit"):
        data_snapshot.unpack(snapshot, dest)

    assert not dest.exists()


def test_unpack_rejects_malformed_vocal_cache_json_before_writing(
    tmp_path: Path
) -> None:
    snapshot = tmp_path / "bad-cache-json.tar"
    _write_required_snapshot(
        snapshot,
        extra_members=[("state/vocal-cache/bad.json", b"not-json")],
    )
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="malformed JSON"):
        data_snapshot.unpack(snapshot, dest)

    assert not dest.exists()


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
