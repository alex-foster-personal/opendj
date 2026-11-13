"""LIGHT-13985 (spec L73) offline render of a track's planned show to a frames JSON file (#9904).

[if] 30 s are rendered at 30 fps [then] the file holds 900 frames with increasing t_ns, [else stop].
[if] the same seed is used twice [then] the files are byte-identical, [else stop].
[if] the track has no analysis [then] the command fails naming the missing analysis, not an empty file, [else stop].
"""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path

import pytest

from apps.lighting.patch import Patch, PatchedFixture, StagePosition, VenueStore, store_to_json
from apps.opendj_cli import lighting_cli

FIXTURES = Path(__file__).parent / "fixtures"
TRACK = "sample-show-plan"


def _setup(tmp_path: Path) -> list[str]:
    analysis = tmp_path / "analysis"
    analysis.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / "track_profile_sample.json", analysis / f"{TRACK}.json")
    fixtures = tuple(
        PatchedFixture(f"par{i}", "generic_rgb", "3ch", 1, 1 + 3 * i, ("wash",), StagePosition(i / 4, 0.5))
        for i in range(4)
    )
    store = VenueStore("club", (Patch("club", fixtures),))
    (tmp_path / "patch.json").write_text(json.dumps(store_to_json(store)))
    return ["--patch", "club", "--patch-file", str(tmp_path / "patch.json"), "--analysis-dir", str(analysis)]


def _render(tmp_path: Path, out: str, *, track: str = TRACK, seed: int = 1) -> int:
    common = _setup(tmp_path)
    argv = ["render", track, *common, "--from-s", "60", "--to-s", "90", "--fps", "30", "--seed", str(seed)]
    return lighting_cli.run([*argv, "--out", str(tmp_path / out)])


@pytest.mark.requirement("LIGHT-13985")
def test_thirty_seconds_at_thirty_fps_is_900_increasing_frames(tmp_path: Path) -> None:
    """[if] 30 s are rendered at 30 fps [then] the file holds 900 frames with increasing t_ns, [else stop]."""
    assert _render(tmp_path, "frames.json") == 0
    frames = json.loads((tmp_path / "frames.json").read_text())["frames"]
    assert len(frames) == 900
    times = [f["t_ns"] for f in frames]
    assert all(b > a for a, b in itertools.pairwise(times))
    assert [fx["id"] for fx in frames[0]["fixtures"]] == ["par0", "par1", "par2", "par3"]


@pytest.mark.requirement("LIGHT-13985")
def test_same_seed_twice_is_byte_identical(tmp_path: Path) -> None:
    """[if] the same seed is used twice [then] the files are byte-identical, [else stop]."""
    assert _render(tmp_path, "a.json") == 0
    assert _render(tmp_path, "b.json") == 0
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


@pytest.mark.requirement("LIGHT-13985")
def test_missing_analysis_fails_naming_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the track has no analysis [then] the command fails naming the missing analysis, not an empty file, [else stop]."""
    assert _render(tmp_path, "none.json", track="no-such-track") != 0
    assert "no-such-track: missing analysis" in capsys.readouterr().err
    assert not (tmp_path / "none.json").exists()


@pytest.mark.requirement("LIGHT-13985")
@pytest.mark.requirement("LIGHT-14858")
def test_frames_come_from_the_live_show_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a show is rendered offline [then] each frame is one tick of the live show runtime, coloured by the look it shows, [else stop]."""
    from apps.lighting import runtime as live
    from apps.lighting.show_map import load_palette

    shown = []
    real_step = live.step

    def spy(state, decks, mode):
        state, frame = real_step(state, decks, mode)
        shown.append(frame)
        return state, frame

    monkeypatch.setattr(live, "step", spy)
    assert _render(tmp_path, "frames.json") == 0
    frames = json.loads((tmp_path / "frames.json").read_text())["frames"]
    assert len(shown) == len(frames) == 900
    palette = load_palette("cool")
    looks = set()
    for frame, show in zip(frames, shown, strict=True):
        (layer,) = [layer for layer in show.layers if layer.deck == show.colour_deck]
        looks.add(layer.look)
        colours = palette.colours[layer.look]
        assert [fx["rgb"] for fx in frame["fixtures"]] == [list(colours[n % len(colours)]) for n in range(4)]
    assert len(looks) > 1, "the 30 s window crosses at least one look change"
