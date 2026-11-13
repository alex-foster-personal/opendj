"""Regression tests for the underpowered (gated) path of scripts/bench/shootout_report.py.

The report is one entry script plus sibling ``shootout_report_*`` modules. ``build_report``
is reached through the entry script, as a caller would; the private helpers are read from
the sibling module that owns each one.

The tracked manifest has enough tracks to rank, so the committed MODEL-SHOOTOUT.md only
locks the full report. These tests pin the early return in ``build_report``: below the
ranking minimum the ranking-only sections are withheld while the report tail survives.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_BENCH = Path(__file__).resolve().parents[2] / "scripts" / "bench"
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import shootout_report  # noqa: E402
import shootout_report_build  # noqa: E402
import shootout_report_constants  # noqa: E402
import shootout_report_decision  # noqa: E402
import shootout_report_model  # noqa: E402

_MIN = shootout_report_constants._MIN_TRACKS_FOR_RANKING

# Sections that only make sense once there are enough tracks to rank across.
_RANKING_ONLY_HEADINGS: tuple[str, ...] = (
    "## Honesty check: is any SI-SDR win bought by under-separating?",
    "## Perceptual proxy: log-spectral distance (lower is better)",
    "## The production decision",
    "## Sizing the train-on-test leak, and the quantization cost",
)


def _manifest_with(track_count: int) -> dict[str, Any]:
    """Return the tracked shootout manifest truncated to ``track_count`` tracks."""
    manifest = json.loads((_BENCH / "model_shootout.json").read_text(encoding="utf-8"))
    assert len(manifest["tracks"]) >= track_count
    trimmed = dict(manifest)
    trimmed["tracks"] = list(manifest["tracks"])[:track_count]
    return trimmed


@pytest.mark.parametrize(
    ("track_count", "gated"),
    [(1, True), (_MIN - 1, True), (_MIN, False), (_MIN + 1, False)],
)
def test_underpowered_gate_boundary(track_count: int, gated: bool) -> None:
    """The gate fires strictly below the ranking minimum and nowhere else."""
    assert shootout_report_decision._underpowered(track_count) is gated
    assert (shootout_report_decision._underpowered_banner(track_count) != []) is gated


def test_underpowered_manifest_sets_gated_fact() -> None:
    """A manifest with fewer than the minimum tracks is collected as gated."""
    facts = shootout_report_build._collect_report_facts(_manifest_with(_MIN - 1))
    assert facts.gated is True
    assert len(facts.tracks) == _MIN - 1


def test_underpowered_report_withholds_ranking_only_sections() -> None:
    """The gated render withdraws ranking, headline and cost and skips every later ranking section."""
    count = _MIN - 1
    report = shootout_report.build_report(_manifest_with(count))

    assert "## INCOMPLETE RUN, NOT A RESULT" in report
    assert f"Withheld. {count} tracks are not enough to name a best model" in report
    assert f"Producing a ranking and a spread from {count} tracks" in report
    assert f"Producing a cost ranking from {count} tracks" in report
    assert "## Quality per unit compute" in report
    assert "median separate_s" not in report
    assert "Best median quality among test-set-clean models" not in report
    for heading in _RANKING_ONLY_HEADINGS:
        assert heading not in report, heading


def test_underpowered_report_preserves_tail() -> None:
    """The gated render still ends with the full sample-size-independent tail."""
    manifest = _manifest_with(_MIN - 1)
    report = shootout_report.build_report(manifest)
    stats, tracks = shootout_report_model._collect(manifest)
    failed = [s for s in stats if not s.ran]
    tail = shootout_report_build._tail_sections(manifest, tracks, manifest["config"], failed)

    assert report.endswith("\n".join(tail))
    assert "## What this run did not measure" in report
    assert "## Per-track windows" in report
    assert report.endswith("-Claude\n")
    withdrawn_at = report.index("Producing a cost ranking from")
    assert withdrawn_at < report.index("## What this run did not measure")


def test_sufficient_manifest_renders_ranking_sections() -> None:
    """At the ranking minimum the full report renders with no banner and no withdrawals."""
    report = shootout_report.build_report(_manifest_with(_MIN))

    assert "## INCOMPLETE RUN, NOT A RESULT" not in report
    assert "**WITHDRAWN.**" not in report
    assert "Best median quality among test-set-clean models" in report
    assert "median separate_s" in report
    for heading in _RANKING_ONLY_HEADINGS:
        assert heading in report, heading
    assert "## What this run did not measure" in report
    assert report.endswith("-Claude\n")
