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
import sys
from pathlib import Path

import pytest

from apps.shared import platform_paths as pp


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
                    {"from": "/Users/dev", "to": "D:/wrong"},
                    {"from": "/Users/dev/Music", "to": "D:/lib"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))

    result = pp.load_path_map()

    assert result.entries[0] == ("/Users/dev/Music", "D:/lib")
    assert result.entries[1] == ("/Users/dev", "D:/wrong")


def test_load_path_map_falls_back_to_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/dev", "to": "D:/lib"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(pp, "DATA_DIR", data_dir)

    result = pp.load_path_map()

    assert result.entries == (("/Users/dev", "D:/lib"),)


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
        json.dumps({"entries": [{"from": "/Users/dev", "to": ""}]}), encoding="utf-8"
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    with pytest.raises(ValueError, match="empty"):
        pp.load_path_map()


def test_load_path_map_preserves_windows_drive_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    map_file = tmp_path / "drive-root.json"
    map_file.write_text(
        json.dumps({"entries": [{"from": "/Users/dev", "to": "D:/"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))
    assert pp.load_path_map().entries == (("/Users/dev", "D:/"),)


def test_load_path_map_uses_mdt_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "snapshot-data"
    data_dir.mkdir()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/dev", "to": "D:/music"}]}), encoding="utf-8"
    )
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    monkeypatch.setattr(pp, "DATA_DIR", data_dir)
    assert pp.load_path_map().entries == (("/Users/dev", "D:/music"),)


def test_load_path_map_uses_explicit_data_dir_over_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_data = tmp_path / "default-data"
    explicit_data = tmp_path / "snapshot-data"
    default_data.mkdir()
    explicit_data.mkdir()
    (default_data / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/dev", "to": "D:/wrong"}]}), encoding="utf-8"
    )
    (explicit_data / "path-map.json").write_text(
        json.dumps({"entries": [{"from": "/Users/dev", "to": "D:/snapshot"}]}), encoding="utf-8"
    )
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    monkeypatch.setattr(pp, "DATA_DIR", default_data)
    assert pp.load_path_map(explicit_data).entries == (("/Users/dev", "D:/snapshot"),)


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
        native = "/Users/dev/Music/track.wav"
    result = pp.resolve_library_path(native, path_map=pp.PathMap(entries=()))
    assert result.mapped is True
    assert result.reason == "native"
    assert result.resolved == Path(native)


def test_resolve_library_path_unmapped_on_simulated_win32_with_empty_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    result = pp.resolve_library_path("/Users/dev/Music/a.wav", path_map=pp.PathMap(entries=()))
    assert result.resolved is None
    assert result.mapped is False
    assert result.reason.startswith("unmapped")


def test_resolve_library_path_path_map_hit_on_simulated_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    path_map = pp.PathMap(entries=(("/Users/dev/Music", "D:/lib"),))
    result = pp.resolve_library_path("/Users/dev/Music/a.wav", path_map=path_map)
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
            ("/Users/dev/Music", "D:/lib"),
            ("/Users/dev", "D:/wrong"),
        )
    )
    result = pp.resolve_library_path("/Users/dev/Music/a.wav", path_map=path_map)
    assert result.resolved == Path("D:/lib/a.wav")
    assert result.reason == "path-map"


def test_resolve_library_path_does_not_match_a_non_boundary_prefix_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "IS_DARWIN", False)
    monkeypatch.setattr(pp, "IS_WINDOWS", True)
    result = pp.resolve_library_path(
        "/Users/dev/Music-backup/a.wav",
        path_map=pp.PathMap(entries=(("/Users/dev/Music", "D:/library"),)),
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
        json.dumps({"entries": [{"from": "/Users/dev/Music", "to": "D:/lib"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("MDT_PATH_MAP", str(map_file))

    result = pp.resolve_library_path("/Users/dev/Music/a.wav")

    assert result.resolved == Path("D:/lib/a.wav")
    assert result.reason == "path-map"
