"""Extra tests for :mod:`apps.sync.usb.pioneer.agent_actuator` dispatch.

Complements the main ``test_pioneer_agent.py`` suite, focusing on pure
helpers and the ``Actuator.execute_action`` simulated dispatch paths
that don't require a live macOS GUI. The goal is to cover the action
router + simulated-skip branches for every supported action type.

Requirement: CAT-06.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.requirement("CAT-06")]

# The agent_actuator module transitively imports Quartz / PIL. On non-
# Darwin hosts those imports succeed thanks to the module's own guards
# (Quartz is optional) — but we still guard at collection time so the
# whole module is skipped cleanly on Linux CI (matches the sibling
# test_pioneer_agent.py behaviour).
_IS_DARWIN = sys.platform == "darwin"

if not _IS_DARWIN:  # pragma: no cover
    pytest.skip(
        "Pioneer agent is macOS-only (Quartz + cliclick).",
        allow_module_level=True,
    )


from apps.sync.usb.pioneer.agent_actuator import (  # noqa: E402
    Actuator,
    DisplayGeometry,
    Trace,
    _build_scroll_applescript,
    _display_fallback_metadata,
    _point_arg,
    _translate_key_combo,
    capture_window,
    scaled_to_points,
    window_scaled_to_global_points,
)

# --------------------------------------------------------------------------- #
# Pure helpers — _point_arg, _build_scroll_applescript
# --------------------------------------------------------------------------- #


def test_point_arg_format() -> None:
    assert _point_arg(10, 20) == "10,20"


def test_build_scroll_applescript_clamps_amount() -> None:
    """``amount`` is clamped to 1..10 and a known key code is emitted."""
    script = _build_scroll_applescript("down", 100)
    # 100 clamps to 10.
    assert "repeat 10 times" in script
    # down → key code 125.
    assert "key code 125" in script


def test_build_scroll_applescript_defaults_to_down_for_unknown() -> None:
    script = _build_scroll_applescript("weird-direction", 2)
    assert "key code 125" in script  # down fallback
    assert "repeat 2 times" in script


def test_build_scroll_applescript_supports_all_directions() -> None:
    # up=126, down=125, left=123, right=124
    assert "key code 126" in _build_scroll_applescript("up", 1)
    assert "key code 123" in _build_scroll_applescript("left", 1)
    assert "key code 124" in _build_scroll_applescript("right", 1)


def test_build_scroll_applescript_clamps_zero_and_negative() -> None:
    """Amount <= 0 clamps up to 1 (never zero-loop)."""
    script = _build_scroll_applescript("down", 0)
    assert "repeat 1 times" in script
    script = _build_scroll_applescript("down", -5)
    assert "repeat 1 times" in script


# --------------------------------------------------------------------------- #
# _translate_key_combo
# --------------------------------------------------------------------------- #


def test_translate_key_combo_single_named_key() -> None:
    assert _translate_key_combo("return") == ["kp:return"]
    assert _translate_key_combo("enter") == ["kp:return"]  # alias
    assert _translate_key_combo("esc") == ["kp:esc"]


def test_translate_key_combo_modifier_chain() -> None:
    assert _translate_key_combo("cmd+c") == ["kd:cmd", "kp:c", "ku:cmd"]


def test_translate_key_combo_multi_modifier() -> None:
    out = _translate_key_combo("cmd+shift+s")
    # Modifiers are joined with commas in the order they appear.
    assert out[0].startswith("kd:")
    assert out[1] == "kp:s"
    assert out[2].startswith("ku:")
    # Both mods present in the down+up entries.
    assert "cmd" in out[0] and "shift" in out[0]


def test_translate_key_combo_pure_modifier_falls_back() -> None:
    assert _translate_key_combo("cmd") == ["kp:cmd"]


def test_translate_key_combo_empty_string_emits_waitzero() -> None:
    """An empty text produces a harmless ``w:0`` no-op."""
    assert _translate_key_combo("") == ["w:0"]


# --------------------------------------------------------------------------- #
# scaled_to_points
# --------------------------------------------------------------------------- #


def test_scaled_to_points_rejects_nonpositive_dimensions() -> None:
    geom = DisplayGeometry(
        pixel_width=1920, pixel_height=1080,
        point_width=1920, point_height=1080,
    )
    with pytest.raises(ValueError):
        scaled_to_points(10, 10, scaled_width=0, scaled_height=10, geometry=geom)
    with pytest.raises(ValueError):
        scaled_to_points(10, 10, scaled_width=10, scaled_height=-1, geometry=geom)


def test_scaled_to_points_retina_divides_by_backing_scale() -> None:
    # Retina: physical is 2× point. A downsampled 1920×1080 frame maps
    # back to 2× points via (pixel/scaled) × (1/backing_scale) = 1.
    geom = DisplayGeometry(
        pixel_width=3840, pixel_height=2160,
        point_width=1920, point_height=1080,
    )
    x, y = scaled_to_points(
        960, 540, scaled_width=1920, scaled_height=1080, geometry=geom,
    )
    assert (x, y) == (960, 540)


# --------------------------------------------------------------------------- #
# capture_window — display fallback when no window found
# --------------------------------------------------------------------------- #


def _tiny_png() -> bytes:
    """A real 2×2 PNG, used to exercise the downsample path."""
    import io

    from PIL import Image

    img = Image.new("RGB", (8, 4), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_capture_window_falls_back_to_display_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``find_app_window`` returns None, capture_window uses display mode."""
    from apps.sync.usb.pioneer import agent_actuator as aa

    monkeypatch.setattr(aa, "find_app_window", lambda _: None)

    # Stub display geometry + feed a raw PNG so the Quartz path never runs.
    def _fake_geom() -> DisplayGeometry:
        return DisplayGeometry(
            pixel_width=8, pixel_height=4,
            point_width=8, point_height=4,
        )
    monkeypatch.setattr(aa, "get_display_geometry", _fake_geom)

    _png, meta = aa.capture_window(
        "rekordbox", downsample_width=100, _raw_bytes=_tiny_png()
    )
    assert meta["capture_mode"] == "display"
    assert meta["window_id"] is None
    assert meta["owner_name"] == ""
    assert meta["backing_scale"] == 1.0


def test_capture_window_uses_injected_window_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``_injected_window``, we skip Quartz calls entirely."""
    window = {
        "window_id": 42,
        "owner_name": "rekordbox",
        "title": "MyDeck",
        "bounds": {"x": 100.0, "y": 50.0, "w": 8.0, "h": 4.0},
        "layer": 0,
    }
    _png, meta = capture_window(
        "rekordbox", downsample_width=100,
        _raw_bytes=_tiny_png(), _injected_window=window,
    )
    assert meta["capture_mode"] == "window"
    assert meta["window_id"] == 42
    assert meta["owner_name"] == "rekordbox"
    assert meta["title"] == "MyDeck"
    assert meta["window_bounds"] == {"x": 100.0, "y": 50.0, "w": 8.0, "h": 4.0}


# --------------------------------------------------------------------------- #
# _display_fallback_metadata
# --------------------------------------------------------------------------- #


def test_display_fallback_metadata_carries_window_hints() -> None:
    geom = DisplayGeometry(
        pixel_width=3840, pixel_height=2160,
        point_width=1920, point_height=1080,
    )
    meta = _display_fallback_metadata(
        b"", geom, (960, 540), prev_owner="rekordbox", prev_title="Deck",
    )
    assert meta["capture_mode"] == "display"
    assert meta["window_bounds"] == {"x": 0.0, "y": 0.0, "w": 1920.0, "h": 1080.0}
    assert meta["owner_name"] == "rekordbox"
    assert meta["title"] == "Deck"
    assert meta["physical_width"] == 3840
    assert meta["backing_scale"] == 2.0


# --------------------------------------------------------------------------- #
# Actuator.execute_action — simulated dispatch paths
# --------------------------------------------------------------------------- #


def _mk_actuator(tmp_path: Path, **kwargs) -> Actuator:
    """Build an Actuator wired for pure-logic tests (simulated=True)."""
    tr = Trace.new(tmp_path)
    meta = {
        "capture_mode": "window",
        "window_bounds": {"x": 0.0, "y": 0.0, "w": 400.0, "h": 300.0},
        "window_id": 7,
        "owner_name": "rekordbox",
        "title": "rekordbox",
        "downsample_width": 200,
        "downsample_height": 150,
        "physical_width": 400,
        "physical_height": 300,
        "backing_scale": 1.0,
    }
    act = Actuator(
        tr, downsample_width=200, simulated=True,
        frontmost_app=None, enforce_click_guard=False,
        **kwargs,
    )
    act._last_capture_meta = meta
    act._last_scaled = (200, 150)
    return act


def test_execute_action_right_click_simulated_skips(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    # With the _mk_actuator metadata (ds 200×150 → phys 400×300, bounds at
    # (0,0)), the click at (10,10) maps to point (20,20).
    result = act.execute_action(
        {"action": "right_click", "coordinate": [10, 10]}
    )
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["meta"]["point"] == [20, 20]


def test_execute_action_double_click_simulated_skips(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action(
        {"action": "double_click", "coordinate": [10, 10]}
    )
    assert result["ok"] is True and result["skipped"] is True


def test_execute_action_coordinate_required_for_click(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "left_click"})
    assert result["ok"] is False
    assert "requires coordinate" in result["error"]


def test_execute_action_cursor_position_returns_last(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    act._last_cursor = (42, 37)
    result = act.execute_action({"action": "cursor_position"})
    assert result == {"ok": True, "x": 42, "y": 37, "meta": result["meta"]}


def test_execute_action_mouse_move_simulated_updates_cursor(
    tmp_path: Path,
) -> None:
    """simulated=True + mouse_move: no cliclick, but cursor state advances."""
    act = _mk_actuator(tmp_path)
    result = act.execute_action(
        {"action": "mouse_move", "coordinate": [100, 75]}
    )
    assert result["ok"] is True
    # After mouse_move, _last_cursor should reflect the translated point
    # (100×(400/200), 75×(300/150)) = (200, 150).
    assert act._last_cursor == (200, 150)


def test_execute_action_type_simulated_skips(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "type", "text": "hello"})
    assert result["ok"] is True and result["skipped"] is True
    assert result["meta"]["text"] == "hello"


def test_execute_action_type_requires_text(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "type"})
    assert result["ok"] is False
    assert "requires text" in result["error"]


def test_execute_action_key_simulated_skips(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "key", "text": "cmd+c"})
    assert result["ok"] is True and result["skipped"] is True
    # The translated cliclick args are recorded in the trace payload.
    assert result["meta"]["cli"] == ["kd:cmd", "kp:c", "ku:cmd"]


def test_execute_action_key_requires_text(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "key"})
    assert result["ok"] is False
    assert "requires text" in result["error"]


def test_execute_action_left_click_drag_simulated_skips(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({
        "action": "left_click_drag",
        "start_coordinate": [10, 10],
        "coordinate": [50, 60],
    })
    assert result["ok"] is True and result["skipped"] is True
    # Coordinates are translated via window_scaled_to_global_points (×2/×2).
    assert result["meta"]["from"] == [20, 20]
    assert result["meta"]["to"] == [100, 120]


def test_execute_action_left_click_drag_requires_endpoints(
    tmp_path: Path,
) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "left_click_drag"})
    assert result["ok"] is False
    assert "left_click_drag requires" in result["error"]


def test_execute_action_scroll_simulated_skips(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({
        "action": "scroll",
        "coordinate": [10, 10],
        "scroll_direction": "down",
        "scroll_amount": 3,
    })
    assert result["ok"] is True and result["skipped"] is True
    assert result["meta"]["direction"] == "down"
    assert result["meta"]["amount"] == 3
    # Coordinate was translated + recorded.
    assert "point" in result["meta"]


def test_execute_action_scroll_without_coordinate(tmp_path: Path) -> None:
    """Scroll with no coordinate still works (simulated)."""
    act = _mk_actuator(tmp_path)
    result = act.execute_action({
        "action": "scroll",
        "scroll_direction": "up",
        "scroll_amount": 2,
    })
    assert result["ok"] is True and result["skipped"] is True
    assert result["meta"]["direction"] == "up"


def test_execute_action_wait_simulated_returns_ok(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "wait", "duration": 0.1})
    assert result["ok"] is True
    assert result["meta"]["seconds"] == 0.1
    assert result["meta"]["outcome"] == "waited"


def test_execute_action_screenshot_captures_via_simulated_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``screenshot`` action delegates to ``capture()``."""
    act = _mk_actuator(tmp_path)
    # Stub capture() so no Quartz path runs; just return a tiny base64 blob.
    def _fake_capture():
        meta = {
            "capture_mode": "window",
            "window_bounds": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "window_id": 0, "owner_name": "", "title": "",
            "downsample_width": 1, "downsample_height": 1,
            "physical_width": 1, "physical_height": 1,
            "backing_scale": 1.0,
        }
        act._last_capture_meta = meta
        act._last_scaled = (1, 1)
        return {
            "png_bytes": b"img",
            "capture_meta": meta,
            "scaled_size": (1, 1),
            "base64": "QUJD",
        }
    monkeypatch.setattr(act, "capture", _fake_capture)
    result = act.execute_action({"action": "screenshot"})
    assert result["ok"] is True
    assert result["screenshot"] == "QUJD"


def test_execute_action_unsupported_returns_error(tmp_path: Path) -> None:
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"action": "teleport"})
    assert result["ok"] is False
    assert "unsupported action" in result["error"]
    assert "teleport" in result["error"]


def test_execute_action_accepts_type_key_instead_of_action(
    tmp_path: Path,
) -> None:
    """Actions can be dispatched with ``type`` field as fallback."""
    act = _mk_actuator(tmp_path)
    result = act.execute_action({"type": "wait", "duration": 0.01})
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# window_scaled_to_global_points — edge cases
# --------------------------------------------------------------------------- #


def test_window_scaled_to_global_points_with_retina_and_offset() -> None:
    """Window at (1000,500), physical 2× points, click maps to global pt."""
    meta = {
        "downsample_width": 200, "downsample_height": 150,
        "physical_width": 400, "physical_height": 300,
        "backing_scale": 2.0,
        "window_bounds": {"x": 1000.0, "y": 500.0, "w": 200.0, "h": 150.0},
    }
    # Click at center of downsampled frame (100, 75) →
    # px (200, 150) → pt (100, 75) → global (1100, 575)
    gx, gy = window_scaled_to_global_points(100, 75, meta)
    assert (gx, gy) == (1100, 575)


def test_window_scaled_to_global_points_treats_missing_values_as_identity() -> None:
    """Missing metadata keys default to safe identity mapping."""
    gx, gy = window_scaled_to_global_points(10, 20, {})
    assert (gx, gy) == (10, 20)
