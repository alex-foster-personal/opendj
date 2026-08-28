"""Tests for the :mod:`apps.open_dj.schema` typed dataclass layer.

Covers:

* Dataclass construction with defaults + field validation (frozen).
* :class:`Adapter` Protocol runtime-check (structural typing).
* :meth:`AdapterReport.bump` / :meth:`AdapterReport.warn` mutation.
* :meth:`OpenDjLibrary.to_dict` round-trip.
* :meth:`Capabilities.to_dict` shape.
* :func:`serialize_jcs` stability + RFC 8785 equivalence.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from apps.open_dj import (
    Adapter,
    AdapterReport,
    BeatGridPoint,
    Capabilities,
    CapabilityField,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
    Warning,
    serialize_jcs,
)
from apps.open_dj.canon import to_canonical_bytes

# ------------------------------------------------------------- builders


def _sample_track() -> Track:
    return Track(
        track_id="a" * 40,
        file_path="/Music/demo.mp3",
        title="Demo",
        artists=("Alice", "Bob"),
        album="Vol 1",
        bpm=128.0,
        key_camelot="8A",
        rating=4,
        duration_ms=240_000,
        play_count=3,
        color_rgb=0xFF00FF,
        cues=(
            CuePoint(index=0, position_ms=0, type="hot", name="Intro"),
            CuePoint(
                index=1,
                position_ms=32_000,
                type="loop",
                name="Loop A",
                length_ms=4_000,
            ),
        ),
        beats=(
            BeatGridPoint(position_ms=0, bpm=128.0),
            BeatGridPoint(position_ms=240_000, bpm=128.0, terminal=True),
        ),
        isrc="USABC1234567",
        extensions={"x_demo": 1},
    )


def _sample_library() -> OpenDjLibrary:
    t = _sample_track()
    return OpenDjLibrary(
        version="0.2",
        tracks=(t,),
        playlists=(
            Playlist(name="Warmup", track_ids=(t.track_id,)),
        ),
    )


# ------------------------------------------------------------ dataclass


@pytest.mark.requirement("OPEN-01")
class TestDataclasses:
    def test_track_defaults(self) -> None:
        t = Track(track_id="x", file_path="/x.mp3")
        assert t.title == ""
        assert t.artists == ()
        assert t.bpm is None
        assert t.rating is None
        assert t.play_count == 0
        assert t.cues == ()
        assert t.extensions == {}

    def test_track_frozen(self) -> None:
        t = Track(track_id="x", file_path="/x.mp3")
        with pytest.raises(FrozenInstanceError):
            t.title = "mutated"  # type: ignore[misc]

    def test_cue_point_frozen(self) -> None:
        c = CuePoint(index=0, position_ms=0, type="hot")
        with pytest.raises(FrozenInstanceError):
            c.name = "mutated"  # type: ignore[misc]

    def test_beatgrid_point_terminal_defaults_false(self) -> None:
        b = BeatGridPoint(position_ms=0, bpm=128.0)
        assert b.terminal is False

    def test_playlist_children_nest(self) -> None:
        leaf = Playlist(name="Leaf", track_ids=("a" * 40,))
        folder = Playlist(name="Folder", track_ids=(), children=(leaf,))
        assert folder.children == (leaf,)

    def test_library_to_dict(self) -> None:
        lib = _sample_library()
        d = lib.to_dict()
        assert d["version"] == "0.2"
        assert d["tracks"][0]["title"] == "Demo"
        assert d["playlists"][0]["name"] == "Warmup"

    def test_capabilities_to_dict_shape(self) -> None:
        caps = Capabilities(
            adapter="serato",
            version="0.1.0",
            fields=(
                CapabilityField(field="bpm", supports="lossless"),
                CapabilityField(
                    field="rating", supports="lossy", rationale="no native"
                ),
            ),
        )
        d = caps.to_dict()
        assert d["adapter"] == "serato"
        assert d["fields"][1]["rationale"] == "no native"


# ---------------------------------------------------------- adapter API


class _GoodAdapter:
    name: str = "good"

    def read(self, source: Path) -> tuple[OpenDjLibrary, AdapterReport]:
        return _sample_library(), AdapterReport()

    def write(self, library: OpenDjLibrary, target: Path) -> AdapterReport:
        return AdapterReport()

    def capabilities(self) -> Capabilities:
        return Capabilities(adapter="good", version="1.0", fields=())


class _MissingName:
    def read(self, source: Path) -> tuple[OpenDjLibrary, AdapterReport]:
        return _sample_library(), AdapterReport()

    def write(self, library: OpenDjLibrary, target: Path) -> AdapterReport:
        return AdapterReport()

    def capabilities(self) -> Capabilities:
        return Capabilities(adapter="x", version="1.0", fields=())


@pytest.mark.requirement("OPEN-01")
class TestAdapterProtocol:
    def test_structural_match(self) -> None:
        assert isinstance(_GoodAdapter(), Adapter)


@pytest.mark.requirement("OPEN-01")
class TestAdapterReport:
    def test_bump_increments(self) -> None:
        r = AdapterReport()
        r.bump("tracks", 2)
        r.bump("tracks")
        assert r.counts == {"tracks": 3}

    def test_warn_appends_structured_warning(self) -> None:
        r = AdapterReport()
        r.warn(
            field="rating",
            track_id="a" * 40,
            action="dropped",
            reason="not supported",
        )
        assert len(r.warnings) == 1
        w = r.warnings[0]
        assert isinstance(w, Warning)
        assert w.field == "rating"
        assert w.action == "dropped"


# ----------------------------------------------------------- serialize


@pytest.mark.requirement("OPEN-01")
class TestSerializeJcs:
    def test_dataclass_to_canonical_bytes(self) -> None:
        lib = _sample_library()
        b = serialize_jcs(lib)
        assert isinstance(b, bytes)
        assert b.startswith(b"{")

    def test_equivalent_to_canon_via_dict(self) -> None:
        lib = _sample_library()
        via_dataclass = serialize_jcs(lib)
        via_dict = to_canonical_bytes(
            # to_dict uses asdict which recursively turns nested dataclasses
            # into dicts, but tuples come through as lists, matching
            # serialize_jcs's _normalise path.
            lib.to_dict()
        )
        assert via_dataclass == via_dict

    def test_stable_across_calls(self) -> None:
        lib = _sample_library()
        assert serialize_jcs(lib) == serialize_jcs(lib)
