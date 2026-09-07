"""Tests for :mod:`apps.shared.platform_paths`.

Simulates win32 by monkeypatching the module's own ``IS_DARWIN`` /
``IS_WINDOWS`` flags (the branch points every function in this module
actually reads), plus ``os.environ["APPDATA"]`` where relevant. This module
must NOT use the ``requires_darwin`` / ``requires_audio_stack`` custom
markers (W4 registers those; using them here before registration would
fail ``--strict-markers``) -- use ``pytest.mark.skipif`` instead where a
real-platform-only check is needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared import platform_paths as pp


def _run_path_probe(code: str, *, env: dict[str, str]) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=pp.PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def test_platform_flags_match_this_interpreter() -> None:
    """PLATFORM / IS_DARWIN / IS_WINDOWS reflect the real sys.platform at import."""
    assert pp.PLATFORM == sys.platform
    assert pp.IS_DARWIN == (sys.platform == "darwin")
    assert pp.IS_WINDOWS == (sys.platform == "win32")


def test_rekordbox_app_dir_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", True)
    monkeypatch.setattr(pp, "IS_WINDOWS", False)
    assert pp.rekordbox_app_dir() == pp.HOME / "Library" / "Pioneer" / "rekordbox"


def test_rekordbox_app_dir_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", r"C:\Users\dj\AppData\Roaming")
    assert pp.rekordbox_app_dir() == Path(r"C:\Users\dj\AppData\Roaming") / "Pioneer" / "rekordbox"


def test_rekordbox_app_dir_win32_raises_without_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(RuntimeError, match="APPDATA"):
        pp.rekordbox_app_dir()


def test_rekordbox_app_dir_other_platform_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", False)
    assert pp.rekordbox_app_dir() == pp.HOME / ".Pioneer" / "rekordbox"


def test_import_succeeds_on_this_platform() -> None:
    """Sanity: importing the module at all (already done above) worked."""
    assert isinstance(pp.REKORDBOX_APP_DIR, Path)
    assert pp.REKORDBOX_LIVE_DB == pp.REKORDBOX_APP_DIR / "master.db"
    assert pp.SHARE_ROOT == pp.REKORDBOX_APP_DIR / "share"


@pytest.mark.parametrize(
    "path", [r"C:relative\song.mp3", "D:track.flac", "1:/music", "?:/music"]
)
def test_is_any_absolute_rejects_invalid_windows_drive_paths(path: str) -> None:
    assert pp.is_any_absolute(path) is False


@pytest.mark.parametrize(
    "path", [r"C:\Music\song.mp3", "D:/Music/track.flac", r"\\server\share\song.mp3"]
)
def test_is_any_absolute_accepts_windows_absolute_paths(path: str) -> None:
    assert pp.is_any_absolute(path) is True


# ----- load_path_map ------------------------------------------------------


def test_load_path_map_empty_when_no_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    monkeypatch.setattr(pp, "DATA_DIR", tmp_path / "data-does-not-exist")
    result = pp.load_path_map()
    assert result == pp.PathMap(entries=())


def test_load_path_map_reads_env_json_and_sorts_longest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_file = tmp_path / "custom-map.json"
    map_file.write_text(
        json.dumps(
            {
                "entries": [
                    {"from": "/Users/user", "to": "D:/wrong"},
                    {"from": "/Users/user/Music", "to": "D:/lib"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))

    result = pp.load_path_map()

    assert result.entries[0] == ("/Users/user/Music", "D:/lib")
    assert result.entries[1] == ("/Users/user", "D:/wrong")


def test_load_path_map_falls_back_to_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/user", "to": "D:/lib"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(pp, "DATA_DIR", data_dir)

    result = pp.load_path_map()

    assert result.entries == (("/Users/user", "D:/lib"),)


def test_load_path_map_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    map_file = tmp_path / "bad.json"
    map_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    with pytest.raises(ValueError):
        pp.load_path_map()


def test_load_path_map_raises_when_entries_not_a_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_file = tmp_path / "bad-entries.json"
    map_file.write_text(json.dumps({"entries": {"from": "x", "to": "y"}}), encoding="utf-8")
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    with pytest.raises(ValueError):
        pp.load_path_map()


def test_load_path_map_raises_on_entry_missing_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_file = tmp_path / "bad-entry.json"
    map_file.write_text(json.dumps({"entries": [{"from": "/x"}]}), encoding="utf-8")
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    with pytest.raises(ValueError):
        pp.load_path_map()


def test_load_path_map_rejects_empty_destination(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    map_file = tmp_path / "incomplete-map.json"
    map_file.write_text(
        json.dumps({"entries": [{"from": "/Users/user", "to": ""}]}), encoding="utf-8"
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    with pytest.raises(ValueError, match="empty"):
        pp.load_path_map()


def test_load_path_map_preserves_windows_drive_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    map_file = tmp_path / "drive-root.json"
    map_file.write_text(
        json.dumps({"entries": [{"from": "/Users/user", "to": "D:/"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    assert pp.load_path_map().entries == (("/Users/user", "D:/"),)


def test_load_path_map_uses_mdt_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "snapshot-data"
    data_dir.mkdir()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/user", "to": "D:/music"}]}), encoding="utf-8"
    )
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    monkeypatch.setattr(pp, "DATA_DIR", data_dir)
    assert pp.load_path_map().entries == (("/Users/user", "D:/music"),)


def test_load_path_map_uses_explicit_data_dir_over_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_data = tmp_path / "default-data"
    explicit_data = tmp_path / "snapshot-data"
    default_data.mkdir()
    explicit_data.mkdir()
    (default_data / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/user", "to": "D:/wrong"}]}), encoding="utf-8"
    )
    (explicit_data / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/user", "to": "D:/snapshot"}]}), encoding="utf-8"
    )
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    monkeypatch.setattr(pp, "DATA_DIR", default_data)
    assert pp.load_path_map(explicit_data).entries == (("/Users/user", "D:/snapshot"),)


def test_example_path_map_file_is_valid_and_loadable(monkeypatch: pytest.MonkeyPatch) -> None:
    example = pp.PROJECT_ROOT / "apps" / "shared" / "path_map.example.json"
    assert example.is_file()
    monkeypatch.setenv("MDT_PATH_MAP", str(example))
    result = pp.load_path_map()
    assert len(result.entries) >= 1
    for from_prefix, to_prefix in result.entries:
        assert isinstance(from_prefix, str) and from_prefix
        assert isinstance(to_prefix, str) and to_prefix


# ----- resolve_library_path ------------------------------------------------


def test_resolve_library_path_empty_is_streaming() -> None:
    result = pp.resolve_library_path("", path_map=pp.PathMap(entries=()))
    assert result.resolved is None
    assert result.mapped is False
    assert result.reason == "streaming"


@pytest.mark.parametrize("prefix", ["tidal:", "soundcloud:", "spotify:"])
def test_resolve_library_path_streaming_prefixes(prefix: str) -> None:
    result = pp.resolve_library_path(f"{prefix}abc123", path_map=pp.PathMap(entries=()))
    assert result.mapped is False
    assert result.reason == "streaming"


def test_resolve_library_path_share_relative() -> None:
    result = pp.resolve_library_path("/PIONEER/USB/x.mp3", path_map=pp.PathMap(entries=()))
    assert result.mapped is True
    assert result.reason == "share"
    assert result.resolved == pp.SHARE_ROOT / "PIONEER/USB/x.mp3"


def test_resolve_library_path_native_on_this_platform() -> None:
    if pp.IS_WINDOWS:
        native = r"D:\music\track.wav"
    else:
        native = "/Users/user/Music/track.wav"
    result = pp.resolve_library_path(native, path_map=pp.PathMap(entries=()))
    assert result.mapped is True
    assert result.reason == "native"
    assert result.resolved == Path(native)


def test_resolve_library_path_unmapped_on_simulated_win32_with_empty_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    result = pp.resolve_library_path("/Users/user/Music/a.wav", path_map=pp.PathMap(entries=()))
    assert result.resolved is None
    assert result.mapped is False
    assert result.reason.startswith("unmapped")


def test_resolve_library_path_path_map_hit_on_simulated_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    path_map = pp.PathMap(entries=(("/Users/user/Music", "D:/lib"),))
    result = pp.resolve_library_path("/Users/user/Music/a.wav", path_map=path_map)
    assert result.resolved == Path("D:/lib/a.wav")
    assert result.mapped is True
    assert result.reason == "path-map"


def test_resolve_library_path_longest_prefix_wins_on_simulated_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    # Deliberately unsorted / nested -- resolve_library_path trusts entry
    # order, so this proves load_path_map's sort is what makes the longest
    # prefix win end to end.
    path_map = pp.PathMap(
        entries=(
            ("/Users/user/Music", "D:/lib"),
            ("/Users/user", "D:/wrong"),
        )
    )
    result = pp.resolve_library_path("/Users/user/Music/a.wav", path_map=path_map)
    assert result.resolved == Path("D:/lib/a.wav")
    assert result.reason == "path-map"


def test_resolve_library_path_does_not_match_a_non_boundary_prefix_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    result = pp.resolve_library_path(
        "/Users/user/Music-backup/a.wav",
        path_map=pp.PathMap(entries=(("/Users/user/Music", "D:/library"),)),
    )
    assert result.resolved is None
    assert result.reason.startswith("unmapped")


def test_resolve_library_path_rejects_share_path_traversal() -> None:
    result = pp.resolve_library_path("/PIONEER/../../secret.wav", path_map=pp.PathMap(entries=()))
    assert result.resolved is None
    assert result.reason == "unsafe:share-path"


def test_resolve_library_path_foreign_absolute_on_simulated_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows drive-letter path read on simulated darwin is foreign."""
    monkeypatch.setattr(pp, "IS_DARWIN", True)
    monkeypatch.setattr(pp, "IS_WINDOWS", False)
    result = pp.resolve_library_path("D:/lib/a.wav", path_map=pp.PathMap(entries=()))
    assert result.resolved is None
    assert result.mapped is False
    assert result.reason.startswith("unmapped")


def test_resolve_library_path_uses_load_path_map_when_none_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    map_file = tmp_path / "map.json"
    map_file.write_text(
        json.dumps({"entries": [{"from": "/Users/user/Music", "to": "D:/lib"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))

    result = pp.resolve_library_path("/Users/user/Music/a.wav")

    assert result.resolved == Path("D:/lib/a.wav")
    assert result.reason == "path-map"


# ----- resolve_asset_path --------------------------------------------------


def test_resolve_asset_path_rejects_share_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] a /PIONEER asset symlinks outside share [then ⛔️] it resolves."""
    share_root = tmp_path / "share"
    link = share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside.DAT"
    outside.write_bytes(b"dat")
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    monkeypatch.setattr(pp, "SHARE_ROOT", share_root)

    result = pp.resolve_asset_path(
        "/PIONEER/USB/ANLZ0000.DAT",
        path_map=pp.PathMap(entries=()),
    )

    assert result.resolved is None
    assert result.mapped is False
    assert result.reason == "unsafe:share-symlink"


def test_resolve_asset_path_preserves_explicit_path_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] an explicit data-dir map is supplied [then] asset resolution uses it."""
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    local_root = tmp_path / "mapped"
    local_root.mkdir()
    asset = local_root / "ANLZ0000.DAT"
    asset.write_bytes(b"dat")

    result = pp.resolve_asset_path(
        "/Users/user/Music/ANLZ0000.DAT",
        path_map=pp.PathMap(entries=(("/Users/user/Music", str(local_root)),)),
    )

    assert result.resolved == asset.resolve()
    assert result.reason == "path-map"


@pytest.mark.skipif(pp.IS_WINDOWS, reason="agentbox remap is a native POSIX case")
def test_native_missing_path_uses_path_map(tmp_path: Path) -> None:
    dest = tmp_path / "crate"
    dest.mkdir()
    audio = dest / "a.flac"
    audio.write_bytes(b"fLaC")
    result = pp.resolve_library_path(
        "/Users/user/Music/a.flac",
        path_map=pp.PathMap(entries=(("/Users/user/Music", str(dest)),)),
    )
    assert result.reason == "path-map"
    assert result.resolved == Path(str(dest) + "/a.flac")
    assert pp.fs_residency.is_materialised(result.resolved)


@pytest.mark.skipif(pp.IS_WINDOWS, reason="native-wins needs a real local file")
def test_native_materialised_path_wins_over_path_map(tmp_path: Path) -> None:
    native = tmp_path / "here.flac"
    native.write_bytes(b"fLaC")
    dest = tmp_path / "other"
    dest.mkdir()
    (dest / "here.flac").write_bytes(b"xxxx")
    result = pp.resolve_library_path(
        str(native),
        path_map=pp.PathMap(entries=((str(tmp_path), str(dest)),)),
    )
    assert result.reason == "native"
    assert result.resolved == native


@pytest.mark.skipif(pp.IS_WINDOWS, reason="agentbox is a POSIX host")
def test_remote_mode_never_uses_native_mac_users_tree(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    mapped_root = crate / "users" / "dev"
    mapped_file = mapped_root / "Music" / "a.flac"
    mapped_file.parent.mkdir(parents=True)
    mapped_file.write_bytes(b"fLaC")
    env = os.environ.copy()
    env.update({"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": str(crate)})
    code = (
        "import json; from apps.shared import platform_paths as p; "
        f"r=p.resolve_library_path('/Users/user/Music/a.flac', "
        f"path_map=p.PathMap(entries=(('/Users/user','{mapped_root}'),))); "
        "print(json.dumps({'resolved':str(r.resolved),'reason':r.reason}))"
    )
    result = _run_path_probe(code, env=env)
    assert result == {"resolved": str(mapped_file), "reason": "path-map"}


@pytest.mark.skipif(pp.IS_WINDOWS, reason="agentbox is a POSIX host")
def test_remote_mode_unmapped_mac_path_is_explicit(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    crate.mkdir()
    env = os.environ.copy()
    env.update({"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": str(crate)})
    code = (
        "import json; from apps.shared import platform_paths as p; "
        "r=p.resolve_library_path('/Users/user/Music/missing.flac', "
        "path_map=p.PathMap(entries=())); "
        "print(json.dumps({'resolved':r.resolved,'reason':r.reason}))"
    )
    result = _run_path_probe(code, env=env)
    assert result == {"resolved": None, "reason": "unmapped:remote"}


def test_remote_mode_pioneer_share_root_is_inside_crate(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    crate.mkdir()
    env = os.environ.copy()
    env.update({"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": str(crate)})
    code = (
        "import json; from apps.shared import platform_paths as p; "
        "print(json.dumps({'share_root':str(p.SHARE_ROOT)}))"
    )
    result = _run_path_probe(code, env=env)
    assert result == {"share_root": str(crate / "pioneer-share")}

