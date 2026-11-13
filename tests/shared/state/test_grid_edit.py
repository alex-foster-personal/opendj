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
- if an anchor does not change the beat spacing from the snapped line on, keep
  `n` continuous and rewrite `tempo_changes` to one marker per anchor then
  broken (GRIDEDIT-10220)
- if an edit without anchors gains an `anchors` key then broken

-Claude
"""

from __future__ import annotations

import itertools
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


@pytest.mark.requirement("BEATGRID-12010")
def test_bpm_override_rebuilds_from_anchor_on_apply() -> None:
    """[if] edit holds bpm and anchor_t [then] apply uses rebuild_beats_at_bpm, [else stop]."""
    anchor_t = 2.0
    edit = grid_edit.make_edit(0.0, 0, bpm=128.0, anchor_t=anchor_t)
    expected = grid_edit.rebuild_beats_at_bpm(STEADY, anchor_t, 128.0)
    moved = grid_edit.apply_to_beats(STEADY, edit)
    assert moved == expected
    assert {b["bpm"] for b in moved} == {128.0}


@pytest.mark.requirement("BEATGRID-12010")
def test_bpm_and_anchor_t_are_paired_in_storage() -> None:
    """[if] only one of bpm or anchor_t is set [then] make_edit refuses, [else stop]."""
    with pytest.raises(grid_edit.GridEditError) as lone_bpm:
        grid_edit.make_edit(0.0, 0, bpm=128.0, anchor_t=None)
    assert lone_bpm.value.code == "GRID_EDIT_INVALID"
    raw = '{"v": 1, "edits": {"own": {"offset_ms": 0.0, "beat_shift": 0, "bpm": 128.0}}}'
    with pytest.raises(ValueError, match=grid_edit.FIELD):
        grid_edit._parse_blob("a" * 40, raw)


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


# ---------------------------------------------------------------- GRIDEDIT-10220: tempo anchors

Anchor = grid_edit.Anchor
LONG = _grid([0.5 * i for i in range(241)])  # 120 BPM, 0..120 s


def _anchored(
    anchors: tuple[grid_edit.Anchor, ...], offset_ms: float = 0.0, shift: int = 0, markers: list | None = None
) -> dict[str, Any]:
    payload = {"beatgrid": {"source": "rekordbox", "beats": LONG}, "tempo_changes": markers or []}
    return grid_edit.apply_to_payload(payload, grid_edit.make_edit(offset_ms, shift, anchors=anchors), "rekordbox")


def _continuous(beats: list[dict[str, Any]]) -> bool:
    return all(b["n"] == a["n"] % 4 + 1 for a, b in itertools.pairwise(beats))


@pytest.mark.requirement("GRIDEDIT-10220")
def test_acceptance_one_anchor_at_60s_changes_tempo_to_126() -> None:
    payload = _anchored((Anchor(60.0, 126.0),))
    beats = payload["beatgrid"]["beats"]
    i = next(k for k, b in enumerate(beats) if b["t"] == 60.0)
    assert all(beats[k + 1]["t"] - beats[k]["t"] == pytest.approx(0.5) for k in range(i))
    assert all(beats[k + 1]["t"] - beats[k]["t"] == pytest.approx(60 / 126, abs=1e-5) for k in range(i, len(beats) - 1))
    assert (beats[i]["bpm"], beats[i - 1]["bpm"]) == (126.0, 120.0)
    assert _continuous(beats)
    assert beats[-1]["t"] <= 120.0
    assert payload["tempo_changes"] == [{"at_s": 60.0, "bpm_before": 120.0, "bpm_after": 126.0, "confidence": 1.0}]
    assert payload["beatgrid"]["static_grid_untrusted"] is True
    assert payload["beatgrid"]["edit"]["anchors"] == [{"at_s": 60.0, "bpm": 126.0}]
    assert payload["beatgrid"]["beat_count"] == len(beats)
    assert payload["performance_hints"] == {"dynamic_tempo": True}
    assert LONG[121]["bpm"] == 120.0, "input not mutated"


@pytest.mark.requirement("GRIDEDIT-10220")
def test_an_anchor_snaps_to_the_nearest_grid_line() -> None:
    payload = _anchored((Anchor(60.2, 126.0),))
    assert payload["tempo_changes"][0]["at_s"] == 60.0


@pytest.mark.requirement("GRIDEDIT-10220")
def test_two_anchors_chain_and_measured_markers_after_the_first_are_dropped() -> None:
    measured = [
        {"at_s": 10.0, "bpm_before": 120.0, "bpm_after": 120.0, "confidence": 0.5},
        {"at_s": 80.0, "bpm_before": 120.0, "bpm_after": 121.0, "confidence": 0.5},
    ]
    payload = _anchored((Anchor(40.0, 125.0), Anchor(70.0, 130.0)), markers=measured)
    markers = payload["tempo_changes"]
    assert [m["at_s"] for m in markers[:2]] == [10.0, 40.0]
    assert len(markers) == 3 and markers[2]["bpm_before"] == 125.0 and markers[2]["bpm_after"] == 130.0
    on_125 = [40.0 + k * 60 / 125 for k in range(80)]
    assert min(abs(markers[2]["at_s"] - t) for t in on_125) < 1e-5, "the second snaps onto the regenerated grid"
    assert _continuous(payload["beatgrid"]["beats"])


@pytest.mark.requirement("GRIDEDIT-10220")
def test_offset_applies_before_anchors_and_shift_keeps_bars_continuous() -> None:
    payload = _anchored((Anchor(60.0, 126.0),), offset_ms=10.0, shift=1)
    assert payload["tempo_changes"][0]["at_s"] == pytest.approx(60.01)
    assert _continuous(payload["beatgrid"]["beats"])


@pytest.mark.requirement("GRIDEDIT-10220")
def test_an_anchor_on_the_first_beat_retempos_everything_without_a_marker() -> None:
    payload = _anchored((Anchor(0.0, 100.0),))
    assert {b["bpm"] for b in payload["beatgrid"]["beats"]} == {100.0}
    assert payload["tempo_changes"] == []
    assert "performance_hints" not in payload


@pytest.mark.requirement("GRIDEDIT-10220")
def test_with_anchor_replaces_near_adds_far_and_refuses_bad_input() -> None:
    edit = grid_edit.with_anchor(grid_edit.IDENTITY, 60.0, 126.0)
    assert grid_edit.with_anchor(edit, 60.1, 128.0).anchors == (Anchor(60.1, 128.0),)
    assert grid_edit.with_anchor(edit, 59.0, 128.0).anchors == (Anchor(59.0, 128.0), Anchor(60.0, 126.0))
    full = grid_edit.make_edit(0, 0, anchors=[Anchor(float(k), 120.0) for k in range(64)])
    for at_s, bpm, status, code in [
        (100.0, 120.0, 422, "GRID_ANCHOR_LIMIT"),
        (1.0, 39.99, 422, "GRID_EDIT_OUT_OF_RANGE"),
        (1.0, 300.01, 422, "GRID_EDIT_OUT_OF_RANGE"),
        (math.nan, 120.0, 422, "GRID_EDIT_INVALID"),
        (-1.0, 120.0, 422, "GRID_EDIT_INVALID"),
    ]:
        with pytest.raises(grid_edit.GridEditError) as err:
            grid_edit.with_anchor(full if code == "GRID_ANCHOR_LIMIT" else edit, at_s, bpm)
        assert (err.value.status, err.value.code) == (status, code)
    with pytest.raises(grid_edit.GridEditError) as missing:
        grid_edit.without_anchor(edit, 90.0)
    assert (missing.value.status, missing.value.code) == (404, "GRID_ANCHOR_NOT_FOUND")
    assert grid_edit.without_anchor(edit, 60.2).anchors == ()


@pytest.mark.requirement("GRIDEDIT-10220")
def test_an_edit_without_anchors_serializes_as_before_and_nudges_keep_anchors() -> None:
    assert json.dumps(grid_edit.blob({"own": grid_edit.make_edit(6.0, 1)})) == (
        '{"v": 1, "edits": {"own": {"offset_ms": 6.0, "beat_shift": 1}}}'
    )
    anchored = grid_edit.make_edit(0, 0, anchors=(Anchor(60.0, 126.0),))
    assert not anchored.is_identity()
    assert grid_edit.nudged(anchored, offset_ms=2).anchors == anchored.anchors
    raw = json.dumps(grid_edit.blob({"own": anchored}))
    assert grid_edit._parse_blob("a" * 40, raw) == {"own": anchored}, "round trip"


@pytest.mark.requirement("GRIDEDIT-10220")
@pytest.mark.parametrize(
    "anchors",
    [
        {"at_s": 1, "bpm": 120},
        [],
        [{"at_s": float(k), "bpm": 120} for k in range(65)],
        [{"at_s": 1}],
        [{"at_s": 1, "bpm": 120, "x": 0}],
        [{"at_s": True, "bpm": 120}],
        [{"at_s": 1, "bpm": 301}],
        [{"at_s": 2, "bpm": 120}, {"at_s": 1, "bpm": 120}],
    ],
)
def test_a_malformed_stored_anchor_raises(anchors: object) -> None:
    raw = json.dumps({"v": 1, "edits": {"own": {"offset_ms": 0.0, "beat_shift": 0, "anchors": anchors}}})
    with pytest.raises(ValueError, match=grid_edit.FIELD):
        grid_edit._parse_blob("a" * 40, raw)


# ---------- undo from history (GRIDEDIT-10215) --------------------------------

_ID = "b" * 40


def _undo_db(tmp_path: Any) -> Any:
    conn = state_db.open_rw(tmp_path / "state.db")
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, created_at, updated_at) "
        "VALUES (?, 'inferred', 't', '[]', '2026-01-01', '2026-01-01')",
        (_ID,),
    )
    conn.commit()
    return conn


def _put(conn: Any, edits: dict[grid_edit.GridSource, grid_edit.GridEdit], stamp: str) -> None:
    with StateWriter(conn, actor="webui") as writer:
        writer.set_field(_ID, grid_edit.FIELD, grid_edit.blob(edits), source=grid_edit.OWN_SOURCE, modified_at=stamp)
    conn.commit()


def _undo(conn: Any) -> bool:
    step = grid_edit.previous(conn, _ID)
    if step is None:
        return False
    _put(conn, step.edits, step.modified_at)
    return True


def _rb(conn: Any) -> grid_edit.GridEdit:
    return grid_edit.read(conn, _ID).for_source("rekordbox")


@pytest.mark.requirement("GRIDEDIT-10215")
def test_undo_steps_back_through_history_to_the_measured_grid(tmp_path: Any) -> None:
    conn = _undo_db(tmp_path)
    try:
        _put(conn, {"rekordbox": grid_edit.make_edit(10.0, 0)}, "2026-11-09T00:00:01.000001Z")
        _put(conn, {"rekordbox": grid_edit.make_edit(10.0, 1)}, "2026-11-09T00:00:02.000001Z")
        assert _undo(conn) and _rb(conn) == grid_edit.make_edit(10.0, 0)
        assert grid_edit.read(conn, _ID).modified_at == "2026-11-09T00:00:01.000001Z"
        assert _undo(conn) and _rb(conn) == grid_edit.IDENTITY
        assert grid_edit.previous(conn, _ID) is None
    finally:
        conn.close()


@pytest.mark.requirement("GRIDEDIT-10215")
def test_undo_with_no_row_or_after_resetting_an_unedited_track_is_none(tmp_path: Any) -> None:
    conn = _undo_db(tmp_path)
    try:
        assert grid_edit.previous(conn, _ID) is None
        _put(conn, {"rekordbox": grid_edit.IDENTITY}, "2026-11-09T00:00:01.000001Z")
        assert grid_edit.previous(conn, _ID) is None
    finally:
        conn.close()


@pytest.mark.requirement("GRIDEDIT-10215")
def test_a_new_edit_after_an_undo_drops_the_undone_edit(tmp_path: Any) -> None:
    conn = _undo_db(tmp_path)
    try:
        a, b, c = grid_edit.make_edit(1.0, 0), grid_edit.make_edit(2.0, 0), grid_edit.make_edit(3.0, 0)
        _put(conn, {"rekordbox": a}, "2026-11-09T00:00:01.000001Z")
        _put(conn, {"rekordbox": b}, "2026-11-09T00:00:02.000001Z")
        assert _undo(conn) and _rb(conn) == a
        _put(conn, {"rekordbox": c}, "2026-11-09T00:00:04.000001Z")
        assert _undo(conn) and _rb(conn) == a
        assert _undo(conn) and _rb(conn) == grid_edit.IDENTITY
        assert not _undo(conn)
    finally:
        conn.close()


@pytest.mark.requirement("GRIDEDIT-10215")
def test_undo_restores_both_sources(tmp_path: Any) -> None:
    conn = _undo_db(tmp_path)
    try:
        _put(conn, {"rekordbox": grid_edit.make_edit(5.0, 0)}, "2026-11-09T00:00:01.000001Z")
        _put(
            conn,
            {"rekordbox": grid_edit.make_edit(5.0, 0), "own": grid_edit.make_edit(-2.0, 1)},
            "2026-11-09T00:00:02.000001Z",
        )
        assert _undo(conn)
        stored = grid_edit.read(conn, _ID)
        assert stored.for_source("rekordbox") == grid_edit.make_edit(5.0, 0)
        assert stored.for_source("own") == grid_edit.IDENTITY
    finally:
        conn.close()


# ---------- a beat or bar 1 at the playhead (GRIDEDIT-10115) -----------------


def _grid_120_beat3_at_10s() -> list[dict[str, Any]]:
    """A 120 BPM grid (one line every 0.5 s) whose beat at 10.000 s is bar position 3."""
    return [{"t": k * 0.5, "n": ((k - 18) % 4) + 1, "bpm": 120.0} for k in range(64)]


def _line_at(beats: list[dict[str, Any]], t_s: float) -> dict[str, Any]:
    """The served line nearest ``t_s``."""
    return min(beats, key=lambda beat: abs(float(beat["t"]) - t_s))


@pytest.mark.requirement("GRIDEDIT-10115")
def test_downbeat_at_moves_the_nearest_line_and_makes_it_bar_1() -> None:
    """[if] downbeat at 10.120 s on a 120 BPM grid with beat 3 at 10 s [then] +120 ms and n=1 there, [else stop]."""
    beats = _grid_120_beat3_at_10s()
    assert _line_at(beats, 10.0)["n"] == 3, "positive control: the fixture's beat at 10 s is beat 3"
    edit = grid_edit.edit_for_downbeat_at(beats, grid_edit.IDENTITY, 10.120)
    assert edit.offset_ms == pytest.approx(120.0)
    served = grid_edit.apply_to_beats(beats, edit)
    line = _line_at(served, 10.120)
    assert (line["t"], line["n"]) == (pytest.approx(10.120), 1)
    assert grid_edit.edit_for_downbeat_at(beats, edit, 10.120) == edit, "a second call on bar 1 is a no-op"


@pytest.mark.requirement("GRIDEDIT-10115")
def test_beat_at_moves_the_offset_only_and_composes_with_the_stored_edit() -> None:
    """[if] beat at 10.120 s [then] only the offset moves, from the stored edit's grid, [else stop]."""
    beats = _grid_120_beat3_at_10s()
    stored = grid_edit.make_edit(20.0, 1)
    edit = grid_edit.edit_for_beat_at(beats, stored, 10.120)
    assert (edit.offset_ms, edit.beat_shift) == (pytest.approx(120.0), 1)
    assert _line_at(grid_edit.apply_to_beats(beats, edit), 10.120)["t"] == pytest.approx(10.120)


@pytest.mark.requirement("GRIDEDIT-10115")
def test_at_with_no_line_in_reach_is_refused_409() -> None:
    """[if] the grid is empty or the move passes the offset limit [then] 409 GRID_EDIT_NO_BEAT_NEAR, [else stop]."""
    with pytest.raises(grid_edit.GridEditError) as empty:
        grid_edit.edit_for_beat_at([], grid_edit.IDENTITY, 1.0)
    assert (empty.value.status, empty.value.code) == (409, "GRID_EDIT_NO_BEAT_NEAR")
    near_limit = grid_edit.make_edit(450.0, 0)
    with pytest.raises(grid_edit.GridEditError) as past:
        grid_edit.edit_for_beat_at(_grid_120_beat3_at_10s(), near_limit, 10.45 + 0.2)
    assert (past.value.status, past.value.code) == (409, "GRID_EDIT_NO_BEAT_NEAR")
