"""NATIVE-24 (#5578): two rows for one file are analysed once by the ahead-of-time drain.

Regression lines:
  - if a row whose same-file sibling has the lane's result is enqueued then broken
  - if a row with a unique path is skipped without its own result then broken
  - if a covered sibling still counts as missing in coverage then broken

[if] the drain decodes one file twice for one lane [then] fail, [else stop].
"""
from __future__ import annotations

import pytest

from apps.webui.server import ahead_analysis as aa
from apps.webui.server.ahead_analysis_siblings import with_path_siblings

pytestmark = pytest.mark.requirement("NATIVE-24")

#: a and b are two rows for one file (b's path is the NFD spelling); c is its own file.
PATHS = {"a": "/m/Café.mp3", "b": "/m/Café.mp3", "c": "/m/other.mp3"}


def _drain(runs: list[tuple[str, list[str]]], *, with_paths: bool) -> aa.AheadDrain:
    done = {lane: {"a"} for lane, _b in aa.LANE_ORDER}

    def run_lane(lane: str, _backend: str, ids: list[str]) -> dict[str, str]:
        runs.append((lane, list(ids)))
        done[lane].update(ids)
        return {}

    return aa.AheadDrain(
        aa.AheadSources(
            present_fn=lambda: list(PATHS),
            mapped_fn=set,
            has_strip_fn=lambda _sid: True,
            write_strip_fn=lambda _sid: None,
            done_fn=lambda lane, _backend: set(done[lane]),
            run_lane_fn=run_lane,
            playing_fn=lambda: False,
            blank_tags_fn=set,
            refresh_tags_fn=lambda _sid: True,
            declined_fn=lambda _lane, _backend: {},
            paths_fn=(lambda: dict(PATHS)) if with_paths else None,
        )
    )


def _run_to_green(drain: aa.AheadDrain) -> None:
    for _ in range(50):
        if drain.tick() == "green":
            return
    raise AssertionError("drain never went green")


def test_a_covered_sibling_is_never_enqueued() -> None:
    """[if] a row's same-file sibling has the lane's result [then] it is not enqueued, [else stop]."""
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=True))
    assert runs and all(ids == ["c"] for _lane, ids in runs), runs


def test_mutation_control_without_paths_the_sibling_is_decoded_again() -> None:
    """[if] the drain has no path map [then] the sibling is enqueued, proving the guard bites, [else stop]."""
    runs: list[tuple[str, list[str]]] = []
    _run_to_green(_drain(runs, with_paths=False))
    assert any("b" in ids for _lane, ids in runs), "if the sibling is skipped with no path map then the test is blind"


def test_coverage_counts_a_covered_sibling_done() -> None:
    """[if] a sibling is covered [then] coverage counts it done, not missing, [else stop]."""
    cov = _drain([], with_paths=True).refresh_coverage()
    assert (cov["lanes"]["loudness"]["done"], cov["lanes"]["loudness"]["missing"]) == (2, 1)


def test_a_unique_path_needs_its_own_result() -> None:
    """[if] a done row shares no path [then] only it is done, [else stop]."""
    assert with_path_siblings({"c"}, PATHS) == {"c"}
    assert with_path_siblings({"a"}, PATHS) == {"a", "b"}
    assert with_path_siblings(set(), PATHS) == set()
