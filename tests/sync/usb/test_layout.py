"""Tests for apps.sync.usb.layout."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.layout import dst_relpath, extension_for, sanitise_segment


@pytest.mark.requirement("CAT-02")
def test_sanitise_segment_replaces_forbidden() -> None:
    assert sanitise_segment("Peak/Time") == "Peak_Time"
    assert sanitise_segment('a:b*c?d"e<f>g|h\\i') == "a_b_c_d_e_f_g_h_i"


@pytest.mark.requirement("CAT-02")
def test_sanitise_segment_defaults_empty() -> None:
    assert sanitise_segment("") == "_"
    assert sanitise_segment("...") == "_"


@pytest.mark.requirement("CAT-02")
def test_extension_for_copy_preserves_case() -> None:
    assert extension_for("copy-as-is", Path("/x.WAV")) == ".wav"


@pytest.mark.requirement("CAT-02")
def test_extension_for_transcode_forces_mp3() -> None:
    assert extension_for("mp3@320", Path("/x.flac")) == ".mp3"


@pytest.mark.requirement("CAT-02")
def test_dst_relpath_artist_album() -> None:
    rel = dst_relpath(
        layout="Artist/Album/Track",
        format_="copy-as-is",
        playlist="Warmup",
        track_index=1,
        artist="Alice",
        album="AA",
        title="One",
        src=Path("/x.mp3"),
    )
    assert str(rel) == "Alice/AA/One.mp3"


@pytest.mark.requirement("CAT-02")
def test_dst_relpath_playlist_layout_prefixes_index() -> None:
    rel = dst_relpath(
        layout="Playlist/NN - Artist - Track",
        format_="copy-as-is",
        playlist="Warmup",
        track_index=3,
        artist="Alice",
        album="AA",
        title="One",
        src=Path("/x.mp3"),
    )
    assert str(rel) == "Warmup/03 - Alice - One.mp3"


@pytest.mark.requirement("CAT-02")
def test_dst_relpath_flat() -> None:
    rel = dst_relpath(
        layout="flat",
        format_="copy-as-is",
        playlist="Warmup",
        track_index=1,
        artist="Alice",
        album="AA",
        title="One",
        src=Path("/x.mp3"),
    )
    assert str(rel) == "Alice - One.mp3"


@pytest.mark.requirement("CAT-02")
def test_dst_relpath_unknown_layout_raises() -> None:
    with pytest.raises(ValueError):
        dst_relpath(
            layout="banana",
            format_="copy-as-is",
            playlist="P",
            track_index=1,
            artist="A",
            album="B",
            title="C",
            src=Path("/x.mp3"),
        )
