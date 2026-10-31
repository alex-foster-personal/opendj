"""The grid adjust / grid shift math (GRIDEDIT-6235b), pure.

[if] a grid edit is applied [then] beats move by the offset at the same tempo and a shift only relabels bars, [else stop].

Regression lines:
- if a positive offset moves beats earlier then broken (later is positive)
- if a grid adjust changes any beat's bpm then broken (no tempo change)
- if a whole-beat shift moves a beat time then broken (only `n` moves)
- if shifting bar 1 later by one beat does not make the old beat 2 the new 1 then broken
- if four beats of shift is not the identity then broken
- if an offset past the limit is stored then broken (grid shift is the tool past a beat)
- if a reset (identity) changes the payload grid then broken
- if an unedited track's payload gains any key then every existing `/anlz`
  consumer and ETag changes for nothing: broken (the identity is not stamped)
- overshoot control: if a real edit on an EMPTY grid is not stamped then the
  stored edit is invisible to the deck that would show it: broken

-Claude
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import grid_edit
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("GRIDEDIT-6235b")


def _grid(times: list[float], bpm: float = 120.0) -> list[dict[str, Any]]:
    return [{"n": (i % 4) + 1, "bpm": bpm, "t": t} for i, t in enumerate(times)]


STEADY = _grid([0.5 + 0.5 * i for i in range(8)])


def test_positive_offset_moves_every_beat_later_and_keeps_tempo() -> None:
    moved = grid_edit.apply_to_beats(STEADY, grid_edit.make_edit(12.0, 0))
    assert [b["t"] for b in moved] == pytest.approx([b["t"] + 0.012 for b in STEADY])
    assert [b["bpm"] for b in moved] == [b["bpm"] for b in STEADY]
    assert [b["n"] for b in moved] == [b["n"] for b in STEADY]


def test_negative_offset_moves_earlier_and_drops_beats_before_zero() -> None:
    grid = _grid([0.004, 0.504, 1.004])
    moved = grid_edit.apply_to_beats(grid, grid_edit.make_edit(-10.0, 0))
    assert [b["t"] for b in moved] == pytest.approx([0.494, 0.994])
    # The dropped beat takes its own label with it; the rest keep theirs.
    assert [b["n"] for b in moved] == [2, 3]


def test_input_beats_are_not_mutated() -> None:
    before = json.dumps(STEADY)
    grid_edit.apply_to_beats(STEADY, grid_edit.make_edit(5.0, 1))
    assert json.dumps(STEADY) == before


def test_shift_one_beat_later_makes_old_beat_two_the_new_downbeat() -> None:
    moved = grid_edit.apply_to_beats(STEADY, grid_edit.make_edit(0.0, 1))
    assert [b["t"] for b in moved] == [b["t"] for b in STEADY], "a whole-beat shift moves no time"
    assert [b["n"] for b in moved] == [4, 1, 2, 3, 4, 1, 2, 3]
    downbeat_before = next(b["t"] for b in STEADY if b["n"] == 1)
    downbeat_after = next(b["t"] for b in moved if b["n"] == 1)
    assert downbeat_after == pytest.approx(downbeat_before + 0.5), "bar 1 lands one beat later"


def test_shift_one_beat_earlier_is_the_opposite_direction() -> None:
    moved = grid_edit.apply_to_beats(STEADY, grid_edit.make_edit(0.0, -1))
    assert [b["n"] for b in moved] == [2, 3, 4, 1, 2, 3, 4, 1]


@pytest.mark.parametrize(
    ("shift", "normalized"), [(0, 0), (1, 1), (2, 2), (3, -1), (4, 0), (-1, -1), (-2, 2), (-5, -1), (9, 1)]
)
def test_beat_shift_is_stored_modulo_the_bar(shift: int, normalized: int) -> None:
    assert grid_edit.normalize_beat_shift(shift) == normalized
    assert grid_edit.make_edit(0.0, shift).beat_shift == normalized


def test_four_beats_of_shift_is_the_identity() -> None:
    edit = grid_edit.make_edit(0.0, 0)
    for _ in range(4):
        edit = grid_edit.nudged(edit, beats=1)
    assert edit.is_identity()


def test_repeated_float_steps_do_not_drift() -> None:
    edit = grid_edit.IDENTITY
    for _ in range(10):
        edit = grid_edit.nudged(edit, offset_ms=0.1)
    assert edit.offset_ms == 1.0
    for _ in range(10):
        edit = grid_edit.nudged(edit, offset_ms=-0.1)
    assert edit.offset_ms == 0.0
    assert math.copysign(1.0, edit.offset_ms) == 1.0, "no -0.0 in a stored identity"


def test_offset_past_the_limit_is_refused() -> None:
    at_limit = grid_edit.make_edit(grid_edit.MAX_OFFSET_MS, 0)
    assert at_limit.offset_ms == grid_edit.MAX_OFFSET_MS
    with pytest.raises(grid_edit.GridEditError) as excinfo:
        grid_edit.nudged(at_limit, offset_ms=1.0)
    assert excinfo.value.code == "GRID_EDIT_OUT_OF_RANGE"
    with pytest.raises(grid_edit.GridEditError):
        grid_edit.make_edit(float("nan"), 0)


# Codex P2 on PR #6235: held presses merged into one request from +400 ms
# must land the ones that fit (to +500), not fail whole at the limit.
def test_a_step_past_the_limit_lands_at_the_limit() -> None:
    near = grid_edit.make_edit(400.0, 1)
    assert grid_edit.nudged(near, offset_ms=120.0) == grid_edit.make_edit(grid_edit.MAX_OFFSET_MS, 1)
    assert grid_edit.nudged(grid_edit.make_edit(-400.0, 0), offset_ms=-120.0).offset_ms == -grid_edit.MAX_OFFSET_MS
    # Control: a step inside the limit is exact, not clamped.
    assert grid_edit.nudged(near, offset_ms=60.0).offset_ms == 460.0
    # Control: back inward from the limit still works.
    at_limit = grid_edit.make_edit(grid_edit.MAX_OFFSET_MS, 0)
    assert grid_edit.nudged(at_limit, offset_ms=-10.0).offset_ms == grid_edit.MAX_OFFSET_MS - 10.0


def test_relabel_refuses_a_beat_number_outside_the_bar() -> None:
    with pytest.raises(ValueError, match=r"outside 1\.\.4"):
        grid_edit.relabel_beat_number(0, 1)


def test_payload_identity_is_untouched_and_unstamped() -> None:
    payload = {"beatgrid": {"source": "rekordbox", "beat_count": 8, "beats": STEADY}}
    before = json.dumps(payload, sort_keys=True)
    out = grid_edit.apply_to_payload(payload, grid_edit.IDENTITY, "rekordbox")
    assert json.dumps(out, sort_keys=True) == before


def test_a_real_edit_on_an_empty_grid_is_still_stamped() -> None:
    payload = {"beatgrid": {"source": "own", "status": "missing", "beat_count": 0, "beats": []}}
    out = grid_edit.apply_to_payload(payload, grid_edit.make_edit(4.0, 0), "own")
    assert out["beatgrid"]["beats"] == []
    assert out["beatgrid"]["edit"] == {"source": "own", "offset_ms": 4.0, "beat_shift": 0}


# Sol P1 on PR #6235: a payload with tempo markers but no beats is still
# stamped, so its markers must move with the offset too.
def test_an_offset_moves_tempo_markers_even_with_no_beats() -> None:
    payload = {
        "beatgrid": {"source": "own", "status": "missing", "beat_count": 0, "beats": []},
        "tempo_changes": [{"at_s": 0.75, "bpm_before": 120.0, "bpm_after": 124.0, "confidence": 0.9}],
    }
    out = grid_edit.apply_to_payload(payload, grid_edit.make_edit(4.0, 0), "own")
    assert out["tempo_changes"][0]["at_s"] == pytest.approx(0.754)
    assert out["beatgrid"]["edit"]["offset_ms"] == 4.0


def test_control_a_shift_only_edit_with_no_beats_leaves_markers_alone() -> None:
    marker = {"at_s": 0.75, "bpm_before": 120.0, "bpm_after": 124.0, "confidence": 0.9}
    payload = {"beatgrid": {"source": "own", "beats": []}, "tempo_changes": [dict(marker)]}
    out = grid_edit.apply_to_payload(payload, grid_edit.make_edit(0.0, 1), "own")
    assert out["tempo_changes"] == [marker]


def test_payload_edit_moves_beats_and_tempo_markers_and_recounts() -> None:
    payload = {
        "beatgrid": {"source": "own", "status": "ok", "beat_count": 3, "beats": _grid([0.002, 0.5, 1.0])},
        "tempo_changes": [{"at_s": 0.75, "bpm_before": 120.0, "bpm_after": 124.0, "confidence": 0.9}],
    }
    out = grid_edit.apply_to_payload(payload, grid_edit.make_edit(-5.0, 1), "own")
    assert out["beatgrid"]["status"] == "ok", "other grid fields are kept"
    assert out["beatgrid"]["beat_count"] == len(out["beatgrid"]["beats"]) == 2
    assert out["tempo_changes"][0]["at_s"] == pytest.approx(0.745)
    assert out["beatgrid"]["edit"] == {"source": "own", "offset_ms": -5.0, "beat_shift": 1}


# Sol P1 on PR #6235: grid shift relabels beat numbers and moves no time, so
# a shift-only edit must hand the stored times back exactly, not re-rounded.
def test_shift_only_edit_keeps_every_time_byte_identical() -> None:
    fine = 0.123456789
    payload = {
        "beatgrid": {"source": "own", "status": "ok", "beat_count": 2, "beats": _grid([fine, fine + 0.5])},
        "tempo_changes": [{"at_s": 0.987654321, "bpm_before": 120.0, "bpm_after": 124.0, "confidence": 0.9}],
    }
    out = grid_edit.apply_to_payload(payload, grid_edit.make_edit(0.0, 1), "own")
    assert [b["t"] for b in out["beatgrid"]["beats"]] == [fine, fine + 0.5]
    assert out["tempo_changes"][0]["at_s"] == 0.987654321
    assert [b["n"] for b in out["beatgrid"]["beats"]] != [b["n"] for b in _grid([fine, fine + 0.5])]


def test_control_an_offset_edit_still_moves_and_rounds_times() -> None:
    fine = 0.123456789
    moved = grid_edit.apply_to_beats(_grid([fine]), grid_edit.make_edit(1.0, 0))
    assert moved[0]["t"] == round(fine + 0.001, 6)


def test_store_round_trip_keeps_one_edit_per_source_and_history(tmp_path: Any) -> None:
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, created_at, updated_at) "
            "VALUES (?, 'inferred', 't', '[]', '2026-01-01', '2026-01-01')",
            ("a" * 40,),
        )
        conn.commit()
        assert grid_edit.read(conn, "a" * 40) == grid_edit.NO_EDITS
        writes: list[tuple[str, dict[grid_edit.GridSource, grid_edit.GridEdit]]] = [
            ("2026-10-07T10:00:00Z", {"rekordbox": grid_edit.make_edit(6.0, 1)}),
            ("2026-10-07T10:00:01Z", {"rekordbox": grid_edit.IDENTITY, "own": grid_edit.make_edit(-2.0, 0)}),
        ]
        for stamp, edits in writes:
            with StateWriter(conn, actor="webui") as writer:
                writer.set_field(
                    "a" * 40,
                    grid_edit.FIELD,
                    grid_edit.blob(edits),
                    source=grid_edit.OWN_SOURCE,
                    modified_at=stamp,
                )
            conn.commit()
        stored = grid_edit.read(conn, "a" * 40)
        assert stored.for_source("rekordbox") == grid_edit.IDENTITY
        assert stored.for_source("own") == grid_edit.make_edit(-2.0, 0)
        assert stored.source == "webui"
        history = conn.execute(
            "SELECT value_json FROM track_field_history WHERE stable_id = ? AND field_name = ?",
            ("a" * 40, grid_edit.FIELD),
        ).fetchall()
        assert [json.loads(row[0])["edits"]["rekordbox"]["offset_ms"] for row in history] == [6.0]
    finally:
        conn.close()


# Sol P1 on PR #6235: a malformed stored blob is corruption and must raise,
# never read as "no edit" or as a coerced (truncated) value.
@pytest.mark.parametrize(
    "blob",
    [
        {"v": 1},
        {"v": 1.0, "edits": {}},
        {"v": True, "edits": {}},
        {"v": "1", "edits": {}},
        {"v": 1, "edits": None},
        {"v": 1, "edits": []},
        {"v": 1, "edits": {"own": {"offset_ms": "6", "beat_shift": 1}}},
        {"v": 1, "edits": {"own": {"offset_ms": 6.0, "beat_shift": 1.5}}},
        {"v": 1, "edits": {"own": {"offset_ms": 6.0, "beat_shift": "1"}}},
        {"v": 1, "edits": {"own": {"offset_ms": True, "beat_shift": 1}}},
        {"v": 1, "edits": {"own": {"offset_ms": 6.0}}},
        {"v": 1, "edits": {"own": None}},
        {"v": 1, "edits": {"own": {"offset_ms": 6.0, "beat_shift": 1, "extra": 0}}},
    ],
)
def test_a_malformed_stored_blob_raises(blob: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=grid_edit.FIELD):
        grid_edit._parse_blob("a" * 40, json.dumps(blob))


def test_a_non_finite_stored_offset_raises() -> None:
    with pytest.raises(ValueError, match="offset_ms"):
        grid_edit._parse_blob("a" * 40, '{"v": 1, "edits": {"own": {"offset_ms": NaN, "beat_shift": 0}}}')


def test_control_a_well_formed_blob_still_parses_including_an_integer_offset() -> None:
    parsed = grid_edit._parse_blob("a" * 40, json.dumps({"v": 1, "edits": {"own": {"offset_ms": 6, "beat_shift": 1}}}))
    assert parsed == {"own": grid_edit.make_edit(6.0, 1)}
    assert grid_edit._parse_blob("a" * 40, json.dumps(grid_edit.blob({}))) == {}
