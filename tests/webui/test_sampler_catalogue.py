"""Edge sampler slot catalogue: grid-snapped slots from own and rekordbox loops, skipped_no_grid.

[if] the catalogue builds slots wrongly [then] these tests fail, [else stop].

Regression one-liners:
  - if a loop cue's in_ms is not snapped to the nearest existing grid beat then broken
  - if a track with no grid yields a slot, or is not counted in skipped_no_grid, then broken
  - if slot_id is not "<stable_id>:<source>:<in_ms>-<out_ms>" then broken
  - if a track with an own cue row lists a rekordbox loop then broken (cue_store.load() rule)
  - if no rekordbox drops the own slots or hides the reason then broken
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.webui.server import sampler_catalogue
from apps.webui.server.sampler_catalogue import Catalogue, build_catalogue
from apps.webui.server.sampler_models import default_settings
from apps.webui.server.sampler_rekordbox import RbLoop
from tests.webui.sampler_support import GRID, make_env


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    yield from make_env(tmp_path, monkeypatch)


def _build(env: SimpleNamespace) -> Catalogue:
    return build_catalogue(env.request, env.app.state.backend)


def _regions(cat: Catalogue) -> list[tuple[str, str | None, int, int]]:
    return sorted((s["source"], s["cue_letter"], s["in_ms"], s["out_ms"]) for s in cat.slots)


def test_if_a_loop_cue_starts_30_ms_late_then_it_snaps_to_the_beat(env: SimpleNamespace) -> None:
    """[if] a loop cue starts 30 ms after a beat [then] in_ms is that beat, out 16 beats later, [else stop]."""
    slot = next(s for s in _build(env).slots if s["cue_letter"] == "A")
    assert (slot["in_ms"], slot["out_ms"], slot["beats"], slot["native_bpm"]) == (4000, 12000, 16, 120.0)
    assert slot["slot_id"] == f"{GRID}:loop_hot_cue:4000-12000"
    assert (slot["settings"], slot["name"], slot["vector"]) == (default_settings(is_loop=True), None, None)


def test_if_a_track_has_no_grid_then_it_has_no_slot_and_is_counted(env: SimpleNamespace) -> None:
    """[if] a track's grid has no beats [then] it yields no slot and skipped_no_grid counts it, [else stop]."""
    cat = _build(env)
    assert (cat.skipped_no_grid, {s["stable_id"] for s in cat.slots}) == (1, {GRID})


@pytest.mark.requirement("EDGESAMP-14822")
def test_if_a_track_has_an_own_cue_row_then_rekordbox_loops_do_not_join(env: SimpleNamespace) -> None:
    """[if] rekordbox holds hot loops A, C and a memory loop the own row lacks [then] only own A, B list, [else stop]."""
    cat = _build(env)
    assert (cat.rekordbox, _regions(cat)) == (
        "ok",
        [("loop_hot_cue", "A", 4000, 12000), ("loop_hot_cue", "B", 20000, 22000)],
    )


@pytest.mark.requirement("EDGESAMP-14822")
def test_if_a_loop_was_removed_in_open_dj_then_it_does_not_come_back_as_a_slot() -> None:
    """[if] the own row no longer holds rekordbox's loop C [then] C is no slot, as cue_store.load() hides it, [else stop]."""
    own = '{"cues": [{"kind": "hot_cue", "slot": "A", "in_ms": 4000, "out_ms": 12000}]}'
    rb_loops = [RbLoop("A", 4_000, 12_000), RbLoop("C", 40_000, 42_000), RbLoop(None, 60_000, 64_000)]
    assert [(r.cue_letter, r.in_ms) for r in sampler_catalogue.loop_regions(own, rb_loops)] == [("A", 4000)]
    # An own row with no cues left at all still wins: the DJ removed every loop.
    assert sampler_catalogue.loop_regions('{"cues": []}', rb_loops) == []


@pytest.mark.requirement("EDGESAMP-14822")
def test_if_a_track_has_no_own_cue_row_then_rekordbox_loops_list() -> None:
    """[if] a track has no own cue row [then] rekordbox's hot and memory loops list, the vendor fallback, [else stop]."""
    rb_loops = [RbLoop("C", 40_000, 42_000), RbLoop(None, 60_000, 64_000)]
    assert [(r.source, r.cue_letter, r.in_ms) for r in sampler_catalogue.loop_regions(None, rb_loops)] == [
        ("loop_hot_cue", "C", 40_000),
        ("memory_loop", None, 60_000),
    ]


def test_if_own_and_rekordbox_hold_one_memory_loop_then_it_is_listed_once() -> None:
    """[if] own store and rekordbox hold the same memory loop [then] loop_regions lists it once, [else stop]."""
    own = '{"cues": [{"kind": "loop", "in_ms": 60000, "out_ms": 64000}]}'
    rb_loops = [RbLoop(None, 60_000, 64_000)]
    assert [(r.source, r.in_ms, r.out_ms) for r in sampler_catalogue.loop_regions(own, rb_loops)] == [
        ("memory_loop", 60_000, 64_000)
    ]


def test_if_rekordbox_is_absent_then_own_slots_list_and_the_reason_is_reported(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the rekordbox DB is absent [then] own slots still list and rekordbox says why, [else stop]."""
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", env.tmp / "absent.db")
    cat = _build(env)
    assert cat.rekordbox == f"unavailable: rekordbox database not found at {env.tmp / 'absent.db'}"
    assert sorted(s["cue_letter"] for s in cat.slots) == ["A", "B"]
