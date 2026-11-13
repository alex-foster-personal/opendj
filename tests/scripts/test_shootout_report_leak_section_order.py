"""Lock the leak-sizing section of the shootout report ((private path), Helper 9).

Loads scripts/bench/shootout_report.py via importlib (it is a uv-run script that
imports its sibling modules from scripts/bench, not a package) and renders from
the tracked ``scripts/bench/model_shootout.json`` input only. ``build_report`` is
reached through the entry script; the section helpers are read from
``shootout_report_build``, the sibling module that owns them.

  [if] all five documented comparison pairs are available, the table lists them in
       the documented order -> test_all_five_pairs_render_in_documented_order
  [if] one pair is unavailable, only that row is omitted and the rest keep their
       order -> test_missing_model_omits_only_its_pair,
       test_pair_without_shared_tracks_is_omitted
  [if] the report is rendered twice from identical input, the output is identical
       -> test_identical_input_renders_identical_report
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BENCH_DIR = REPO_ROOT / "scripts" / "bench"
_SCRIPT = _BENCH_DIR / "shootout_report.py"
_MANIFEST = _BENCH_DIR / "model_shootout.json"
_MODULE_NAME = "_shootout_report_leak_section_order"

_LEAK_HEADING = "## Sizing the train-on-test leak, and the quantization cost"

# Documented pair order from the spec: (left, right, what it isolates).
_DOCUMENTED_PAIRS: list[tuple[str, str, str]] = [
    ("mdx", "mdx_extra", "MusDB test set in the training data"),
    ("mdx_q", "mdx_extra_q", "MusDB test set in the training data (quantized)"),
    ("mdx", "mdx_q", "8-bit quantization"),
    ("mdx_extra", "mdx_extra_q", "8-bit quantization"),
    ("htdemucs", "htdemucs_ft", "per-source fine-tuning at roughly 4x compute"),
]


def _load_report_module() -> ModuleType:
    """Import shootout_report.py by path with scripts/bench importable for shootout_spec."""
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    if str(_BENCH_DIR) not in sys.path:
        sys.path.insert(0, str(_BENCH_DIR))
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


report = _load_report_module()

# Loading the entry script put scripts/bench on sys.path, so its sibling imports by name.
import shootout_report_build as report_build  # noqa: E402


def _manifest() -> dict[str, Any]:
    """Return a fresh copy of the tracked shootout manifest."""
    loaded: dict[str, Any] = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return loaded


def _pair_cells(lines: list[str]) -> list[tuple[str, str]]:
    """Return (pair, isolates) cells for each data row of the leak table, in order."""
    rows: list[tuple[str, str]] = []
    for line in lines:
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append((cells[0], cells[1]))
    return rows


def _expected(pairs: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Render the expected (pair, isolates) cells for the given documented pairs."""
    return [(f"`{left}` vs `{right}`", isolates) for left, right, isolates in pairs]


def _leak_block(rendered: str) -> list[str]:
    """Slice the leak-sizing section out of a full report, heading to next heading."""
    lines = rendered.split("\n")
    start = lines.index(_LEAK_HEADING)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return lines[start:end]


def test_all_five_pairs_render_in_documented_order() -> None:
    """With every model present, the leak table lists all five pairs in spec order."""
    facts = report_build._collect_report_facts(_manifest())
    section = report_build._leak_sizing_section(facts)

    assert section[0] == _LEAK_HEADING
    assert section[4] == (
        "| pair | what it isolates | median paired delta | range | tracks favouring the right-hand model |"
    )
    assert section[5] == "| --- | --- | ---: | ---: | ---: |"
    assert _pair_cells(section) == _expected(_DOCUMENTED_PAIRS)
    # The same order survives in the full rendered report, not only the helper.
    assert _pair_cells(_leak_block(report.build_report(_manifest()))) == _expected(_DOCUMENTED_PAIRS)


def test_missing_model_omits_only_its_pair() -> None:
    """A model with no scored tracks drops its one pair; the other four keep order."""
    manifest = _manifest()
    for track in manifest["tracks"]:
        track["results"].pop("htdemucs_ft", None)

    block = _leak_block(report.build_report(manifest))

    assert _pair_cells(block) == _expected(_DOCUMENTED_PAIRS[:4])
    assert not any("htdemucs_ft" in line for line in block)


def test_pair_without_shared_tracks_is_omitted() -> None:
    """Both models ran but share no scored track, so the empty-delta pair is skipped."""
    facts = report_build._collect_report_facts(_manifest())
    tracks = copy.deepcopy(facts.tracks)
    for track in tracks:
        track["results"]["htdemucs"] = {}
    # htdemucs still counts as ran (facts.ran unchanged), only its paired deltas vanish.
    section = report_build._leak_sizing_section(dataclasses.replace(facts, tracks=tracks))

    expected = [pair for pair in _DOCUMENTED_PAIRS if "htdemucs" not in pair[:2]]
    assert _pair_cells(section) == _expected(expected)


def test_identical_input_renders_identical_report() -> None:
    """Two renders from equal manifests are byte-identical, leak section included."""
    first = report.build_report(_manifest())
    second = report.build_report(_manifest())

    assert first == second
    assert _leak_block(first) == _leak_block(second)
    assert _pair_cells(_leak_block(first)) == _expected(_DOCUMENTED_PAIRS)
