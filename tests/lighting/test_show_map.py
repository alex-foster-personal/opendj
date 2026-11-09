"""LIGHT-10561b: section-to-look show map and 8-bar autoloop fallback (issue #9900).

[if] the map is loaded [then] every (section, mood) pair has at least two looks that exist in the look file, [else stop].
[if] a track has only an unknown span [then] the autoloop list is used and the look changes on every 8th downbeat, [else stop].
[if] a look id in the map does not exist [then] loading fails naming it, [else stop].
"""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import pytest

from apps.lighting.sections import LightSection, TrackMood, resolve_sections
from apps.lighting.show_map import (
    DEFAULT_MAP_PATH,
    MIN_LOOKS,
    ShowMapError,
    load_look_ids,
    load_show_map,
)

pytestmark = [pytest.mark.requirement("LIGHT-10561b")]

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_map(tmp_path: Path, mutate) -> tuple[Path, Path]:
    data = copy.deepcopy(_load_json(DEFAULT_MAP_PATH))
    looks_name = data["looks_file"]
    looks_src = DEFAULT_MAP_PATH.parent / looks_name
    looks_dst = tmp_path / looks_name
    looks_dst.write_text(looks_src.read_text(encoding="utf-8"), encoding="utf-8")
    mutate(data)
    map_path = tmp_path / "show_map.json"
    map_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return map_path, looks_dst


def test_shipped_map_covers_every_section_mood_with_two_looks() -> None:
    """[if] the map is loaded [then] every (section, mood) has >=2 looks from the look file."""
    show_map = load_show_map()
    known = load_look_ids()
    assert len(show_map.entries) == len(LightSection) * len(TrackMood)
    for section in LightSection:
        for mood in TrackMood:
            entry = show_map.entry(section, mood)
            assert len(entry.looks) >= MIN_LOOKS
            assert all(look_id in known for look_id in entry.looks)


def test_unknown_span_uses_autoloop_every_eighth_downbeat() -> None:
    """[if] a track has only an unknown span [then] autoloop changes look every 8th downbeat."""
    show_map = load_show_map()
    spans = resolve_sections({"phrases": []}, duration_s=300)
    assert show_map.uses_autoloop(spans)
    assert show_map.autoloop_look(7) == show_map.autoloop_look(0)
    assert show_map.autoloop_look(8) != show_map.autoloop_look(0)

    downbeats_ms = [float(i * 500) for i in range(33)]
    changes = show_map.autoloop_changes(downbeats_ms)
    assert [i for i in range(0, 33, show_map.autoloop_bars)] == [0, 8, 16, 24, 32]
    assert len(changes) == 5
    assert changes[0][0] == downbeats_ms[0]
    looks = [look for _, look in changes]
    for prev, cur in itertools.pairwise(looks):
        assert prev != cur
    cycle = list(show_map.autoloop)
    for index, (_, look) in enumerate(changes):
        assert look == cycle[index % len(cycle)]


def test_uses_autoloop_false_when_phrases_resolve() -> None:
    """[if] resolved phrases exist [then] uses_autoloop is false."""
    payload = json.loads((FIXTURES / "anlz_pssi_sample.json").read_text(encoding="utf-8"))
    spans = resolve_sections(payload, duration_s=90)
    show_map = load_show_map()
    assert not show_map.uses_autoloop(spans)
    assert any(span.section is not LightSection.UNKNOWN for span in spans)


def test_unknown_look_in_cell_fails_with_path(tmp_path: Path) -> None:
    """[if] a cell references a missing look [then] ShowMapError names the id and JSON path."""
    def mutate(data: dict) -> None:
        data["sections"]["drop"]["high"]["looks"][0] = "no_such_look"

    map_path, looks_path = _write_map(tmp_path, mutate)
    with pytest.raises(ShowMapError, match="no_such_look") as exc:
        load_show_map(map_path, looks_path=looks_path)
    assert any("sections.drop.high.looks[0]" in line for line in exc.value.errors)


def test_unknown_look_in_autoloop_fails(tmp_path: Path) -> None:
    """[if] autoloop references a missing look [then] ShowMapError names the id."""
    def mutate(data: dict) -> None:
        data["autoloop"][0] = "ghost_look"

    map_path, looks_path = _write_map(tmp_path, mutate)
    with pytest.raises(ShowMapError, match="ghost_look") as exc:
        load_show_map(map_path, looks_path=looks_path)
    assert any("autoloop[0]" in line for line in exc.value.errors)


def test_cell_with_one_look_fails(tmp_path: Path) -> None:
    """[if] a cell has only one look [then] loading fails requiring at least two."""
    def mutate(data: dict) -> None:
        data["sections"]["verse"]["mid"]["looks"] = ["wash_warm"]

    map_path, looks_path = _write_map(tmp_path, mutate)
    with pytest.raises(ShowMapError, match="at least 2"):
        load_show_map(map_path, looks_path=looks_path)


def test_missing_mood_and_section_reported(tmp_path: Path) -> None:
    """[if] a mood or section cell is missing [then] loading fails naming the path."""
    def mutate(data: dict) -> None:
        del data["sections"]["verse"]["mid"]
        del data["sections"]["bridge"]

    map_path, looks_path = _write_map(tmp_path, mutate)
    with pytest.raises(ShowMapError) as exc:
        load_show_map(map_path, looks_path=looks_path)
    joined = "\n".join(exc.value.errors)
    assert "sections.verse.mid: missing" in joined
    assert "sections.bridge: missing" in joined


def test_extra_section_key_fails(tmp_path: Path) -> None:
    """[if] an unknown section key is present [then] loading fails naming it."""
    def mutate(data: dict) -> None:
        data["sections"]["solo"] = data["sections"]["verse"]

    map_path, looks_path = _write_map(tmp_path, mutate)
    with pytest.raises(ShowMapError, match="unknown section"):
        load_show_map(map_path, looks_path=looks_path)


def test_bad_effect_level_and_duplicate_look_id(tmp_path: Path) -> None:
    """[if] effect_level is invalid or a look id is duplicated [then] loading fails naming the problem."""
    def mutate_map(data: dict) -> None:
        data["sections"]["build"]["high"]["effect_level"] = "nuclear"

    map_path, looks_path = _write_map(tmp_path, mutate_map)
    with pytest.raises(ShowMapError, match="effect_level"):
        load_show_map(map_path, looks_path=looks_path)

    looks = _load_json(looks_path)
    looks["looks"].append({"id": "wash_warm", "name": "Dup warm"})
    looks_path.write_text(json.dumps(looks, indent=2), encoding="utf-8")
    with pytest.raises(ShowMapError, match="duplicate look id 'wash_warm'"):
        load_look_ids(looks_path)


def test_multiple_problems_collected(tmp_path: Path) -> None:
    """[if] several validation problems exist [then] one ShowMapError lists them all."""
    def mutate(data: dict) -> None:
        data["sections"]["drop"]["high"]["looks"][0] = "missing_a"
        data["autoloop"][1] = "missing_b"
        data["extra_key"] = True

    map_path, looks_path = _write_map(tmp_path, mutate)
    with pytest.raises(ShowMapError) as exc:
        load_show_map(map_path, looks_path=looks_path)
    assert len(exc.value.errors) >= 2


def test_autoloop_look_negative_raises() -> None:
    """[if] autoloop_look gets a negative bar index [then] ValueError is raised."""
    show_map = load_show_map()
    with pytest.raises(ValueError, match="non-negative"):
        show_map.autoloop_look(-1)
