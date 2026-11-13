"""Tests for the look / show map / ambient cross-reference linter (issue #14516).

[if] the lighting JSON files are linted [then] cross-reference errors are reported with their path, [else stop].
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from apps.lighting import look_lint
from apps.lighting.ambient import DEFAULT_AMBIENT_PATH
from apps.lighting.show_map import DEFAULT_LOOKS_PATH, DEFAULT_MAP_PATH

pytestmark = [pytest.mark.requirement("LIGHT-14570")]


def _copy_defaults(tmp_path: Path) -> tuple[Path, Path, Path]:
    paths = []
    for src in (DEFAULT_LOOKS_PATH, DEFAULT_MAP_PATH, DEFAULT_AMBIENT_PATH):
        dst = tmp_path / src.name
        shutil.copy(src, dst)
        paths.append(dst)
    return paths[0], paths[1], paths[2]


def test_main_json_files_lint_clean() -> None:
    """[if] the looks, show map and ambient JSON on main are linted [then] zero issues come back, [else stop]."""
    assert look_lint.lint_files() == []


def test_missing_look_id_in_show_map_reports_path(tmp_path: Path) -> None:
    """[if] a show map cell names a look id absent from the catalog [then] the error carries that cell path, [else stop]."""
    looks, show_map, ambient = _copy_defaults(tmp_path)
    data = json.loads(show_map.read_text())
    data["sections"]["intro"]["high"]["looks"][0] = "no_such_look"
    show_map.write_text(json.dumps(data))
    issues = look_lint.lint_files(looks, show_map, ambient)
    found = [i for i in issues if i.path == "sections.intro.high.looks[0]"]
    assert [i.message for i in found] == ["unknown look id 'no_such_look'"]
    assert found[0].source == str(show_map)


def test_unknown_effect_and_unpatched_group_reported() -> None:
    """[if] a look names an unknown effect and groups not in the patch [then] each is reported by path, [else stop]."""
    data = {
        "looks": [
            {
                "id": "a",
                "groups": {"front": {}, "ghost": {}},
                "effects": [{"effect_id": "wobble", "groups": ["front", "phantom"]}],
            }
        ]
    }
    issues = look_lint.lint_looks(data, "x.json", frozenset({"front"}))
    assert sorted((i.path, i.message) for i in issues) == [
        (
            "$.looks[0].effects[0].effect_id",
            "unknown effect 'wobble'; known: ['chase', 'colour_step', 'pulse', 'strobe', 'sweep']",
        ),
        ("$.looks[0].effects[0].groups", "group 'phantom' is not in the patch"),
        ("$.looks[0].groups", "group 'ghost' is not in the patch"),
    ]


def test_fade_beats_must_be_positive_whole_beats() -> None:
    """[if] fade_beats is zero, negative, fractional or not a number [then] it is rejected, [else stop]."""
    bad = [0, -4, 1.5, "8", True]
    data = {"looks": [{"id": f"l{i}", "fade_beats": v} for i, v in enumerate(bad)] + [{"id": "ok", "fade_beats": 4}]}
    issues = look_lint.lint_looks(data, "x.json")
    assert [i.path for i in issues] == [f"$.looks[{i}].fade_beats" for i in range(len(bad))]
