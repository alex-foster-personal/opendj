"""Tests for Prototype C — Claude computer-use Rekordbox export agent.

These tests stay strictly offline by default. The live-run smoke is gated
behind the ``RB_AGENT_LIVE=1`` env var so CI can't accidentally trigger a
real USB write or burn Anthropic credits.

Requirement: CAT-06 — Rekordbox 7 USB export automation prototype.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.requirement("CAT-06")]


# --------------------------------------------------------------------------- #
# Skip the whole module on non-Darwin: Quartz import would fail. The agent
# is a macOS-only tool; there's nothing to validate on Linux CI.
#
# Note: the Darwin skip must happen BEFORE importing PIL / Pillow or the
# apps.sync.usb.pioneer.agent_actuator module, because those imports are not
# guaranteed to be installed on the Ubuntu CI runner (the agent is a macOS
# prototype and its deps are not in the base requirements). Keeping platform
# checks at the very top of the module lets pytest collection succeed on
# Linux without pulling in macOS-only dependencies.
# --------------------------------------------------------------------------- #

_IS_DARWIN = sys.platform == "darwin"

if not _IS_DARWIN:  # pragma: no cover
    pytest.skip(
        "Prototype C agent is macOS-only (Quartz + cliclick).",
        allow_module_level=True,
    )


from PIL import Image  # noqa: E402

from apps.sync.usb.pioneer.agent_actuator import (  # noqa: E402
    Actuator,
    DisplayGeometry,
    Trace,
    _translate_key_combo,
    capture_window,
    ensure_frontmost,
    is_point_inside_window,
    scaled_to_points,
    take_screenshot,
    window_scaled_to_global_points,
)


# --------------------------------------------------------------------------- #
# Coordinate scaling — pure math, no GUI needed.
# --------------------------------------------------------------------------- #


class TestCoordScaling:
    """The actuator must map Claude's downsampled-frame coords back to
    real screen points, accounting for both downsampling *and* Retina
    backing scale."""

    def test_identity_no_downsample(self) -> None:
        geom = DisplayGeometry(1280, 720, 1280, 720)
        x, y = scaled_to_points(
            640, 360, scaled_width=1280, scaled_height=720, geometry=geom
        )
        assert (x, y) == (640, 360)

    def test_non_retina_downsample(self) -> None:
        """5120×2160 non-Retina → 1280×540 Claude frame."""
        geom = DisplayGeometry(5120, 2160, 5120, 2160)
        x, y = scaled_to_points(
            640, 270, scaled_width=1280, scaled_height=540, geometry=geom
        )
        # 640 * 5120/1280 = 2560; 270 * 2160/540 = 1080
        assert (x, y) == (2560, 1080)

    def test_retina_downsample(self) -> None:
        """Retina 2880×1800 pixels / 1440×900 points. Claude sees 1280×800.
        Clicking (1280, 800) in Claude frame should land at (1440, 900)
        in macOS points — the bottom-right corner."""
        geom = DisplayGeometry(2880, 1800, 1440, 900)
        x, y = scaled_to_points(
            1280, 800, scaled_width=1280, scaled_height=800, geometry=geom
        )
        assert (x, y) == (1440, 900)

    def test_origin_maps_to_origin(self) -> None:
        geom = DisplayGeometry(2880, 1800, 1440, 900)
        assert scaled_to_points(
            0, 0, scaled_width=1280, scaled_height=800, geometry=geom
        ) == (0, 0)

    def test_rejects_zero_dims(self) -> None:
        geom = DisplayGeometry(1000, 1000, 1000, 1000)
        with pytest.raises(ValueError):
            scaled_to_points(
                1, 1, scaled_width=0, scaled_height=10, geometry=geom
            )


# --------------------------------------------------------------------------- #
# Key combo translation
# --------------------------------------------------------------------------- #


class TestKeyTranslate:
    def test_named_key(self) -> None:
        assert _translate_key_combo("return") == ["kp:return"]

    def test_enter_alias(self) -> None:
        assert _translate_key_combo("enter") == ["kp:return"]

    def test_single_modifier_combo(self) -> None:
        assert _translate_key_combo("cmd+c") == [
            "kd:cmd",
            "kp:c",
            "ku:cmd",
        ]

    def test_multi_modifier_combo(self) -> None:
        assert _translate_key_combo("cmd+shift+s") == [
            "kd:cmd,shift",
            "kp:s",
            "ku:cmd,shift",
        ]


# --------------------------------------------------------------------------- #
# Screenshot smoke — real Quartz capture.
# Uses an injected PNG so we don't actually need Screen Recording perms
# in the test sandbox; that's covered by the env-gated live test below.
# --------------------------------------------------------------------------- #


def _synthetic_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestScreenshot:
    def test_synthetic_downsample(self) -> None:
        raw = _synthetic_png(5120, 2160)
        png, geom, scaled = take_screenshot(
            downsample_width=1280, _raw_bytes=raw
        )
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "must return a PNG"
        assert len(png) > 0
        assert scaled == (1280, 540)
        # geom is queried live from Quartz; can't assert exact values.
        assert geom.pixel_width > 0 and geom.point_width > 0

    def test_no_downsample_below_threshold(self) -> None:
        raw = _synthetic_png(800, 600)
        png, _, scaled = take_screenshot(
            downsample_width=1280, _raw_bytes=raw
        )
        assert scaled == (800, 600)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# Trace recorder
# --------------------------------------------------------------------------- #


class TestTrace:
    def test_records_action_without_png(self, tmp_path: Path) -> None:
        tr = Trace.new(tmp_path)
        tr.record("action", {"foo": "bar"})
        files = sorted(tr.root.glob("step_*"))
        assert len(files) == 1
        assert files[0].suffix == ".json"

    def test_records_screenshot_with_png(self, tmp_path: Path) -> None:
        tr = Trace.new(tmp_path)
        tr.record("screenshot", {"k": 1}, png_bytes=b"fake-png")
        json_files = sorted(tr.root.glob("*.json"))
        png_files = sorted(tr.root.glob("*.png"))
        assert len(json_files) == 1 and len(png_files) == 1
        assert png_files[0].read_bytes() == b"fake-png"


# --------------------------------------------------------------------------- #
# Agent loop — mocked API client.
# --------------------------------------------------------------------------- #


class TestAgentDryRunInit:
    """Validate the agent calls screenshot first and wires the tool schema
    correctly, using a mock Anthropic client so no network or GUI needed.
    """

    def test_mock_client_flow(self, tmp_path: Path, monkeypatch) -> None:
        from apps.sync.usb.pioneer import agent as agent_mod
        from apps.sync.usb.pioneer.agent import AgentConfig, run_export_agent

        # Stub out the live screenshot path so tests don't need Screen
        # Recording perms in CI / sandbox.
        monkeypatch.setattr(
            agent_mod.Actuator,
            "capture",
            lambda self: {
                "png_bytes": _synthetic_png(1280, 540),
                "geometry": DisplayGeometry(5120, 2160, 5120, 2160),
                "scaled_size": (1280, 540),
                "base64": "",
            },
        )

        # Mock client that returns (1) a probe success, (2) a response
        # with no tool_use so the loop terminates immediately.
        mock_client = MagicMock()
        probe_resp = SimpleNamespace(
            id="msg_probe",
            stop_reason="end_turn",
            content=[],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        main_resp = SimpleNamespace(
            id="msg_1",
            stop_reason="end_turn",
            content=[
                SimpleNamespace(type="text", text="END_TURN: DRY-RUN COMPLETE")
            ],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )
        mock_client.beta.messages.create.side_effect = [probe_resp, main_resp]

        cfg = AgentConfig(
            playlist="UL Percussion",
            usb_path="/Volumes/MAINTAINER",
            dry_run=True,
            max_steps=5,
            trace_dir=tmp_path,
            api_key="sk-test-xxx",
        )
        result = run_export_agent(cfg, client=mock_client)

        # The agent must have called the API at least twice (probe + loop).
        assert mock_client.beta.messages.create.call_count == 2

        # First API call = probe: inspect its tool schema.
        first_call = mock_client.beta.messages.create.call_args_list[0]
        tools = first_call.kwargs["tools"]
        assert tools[0]["type"] == "computer_20250124"
        assert tools[0]["name"] == "computer"
        assert tools[0]["display_width_px"] == 1280

        # First run-loop call must include the initial screenshot in the
        # user message (validates "take a screenshot first" discipline).
        second_call = mock_client.beta.messages.create.call_args_list[1]
        msgs = second_call.kwargs["messages"]
        first_user_content = msgs[0]["content"]
        assert any(
            isinstance(b, dict) and b.get("type") == "image"
            for b in first_user_content
        ), "initial user turn must embed a screenshot"

        # Loop terminated cleanly (1 real API call + probe).
        assert result.steps == 1
        assert result.stop_reason == "end_turn"
        assert result.success is True
        assert result.usage_in_tokens == 100
        assert result.usage_out_tokens == 20
        # Trace directory was created.
        assert result.trace_root.exists()
        # Cost math
        assert result.estimated_cost_usd == pytest.approx(
            100 / 1_000_000 * 3.0 + 20 / 1_000_000 * 15.0
        )


# --------------------------------------------------------------------------- #
# Window-isolated capture + click-guard + frontmost enforcement (Fixes 1/3/6)
# --------------------------------------------------------------------------- #


class TestWindowIsolatedCapture:
    """``capture_window`` must emit the documented metadata shape in
    both window and display-fallback modes, and (critically) must
    translate clicks back to GLOBAL screen points using the window's
    origin \u2014 not just the display frame.
    """

    def test_window_mode_metadata_shape(self) -> None:
        raw = _synthetic_png(1600, 1000)
        injected = {
            "window_id": 42,
            "owner_name": "rekordbox",
            "title": "rekordbox 7.2.14",
            "bounds": {"x": 100.0, "y": 200.0, "w": 1600.0, "h": 1000.0},
            "layer": 0,
        }
        png, meta = capture_window(
            app_name_substring="rekordbox",
            downsample_width=800,
            _raw_bytes=raw,
            _injected_window=injected,
        )
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert meta["capture_mode"] == "window"
        assert meta["window_id"] == 42
        assert meta["owner_name"] == "rekordbox"
        assert meta["window_bounds"] == {
            "x": 100.0, "y": 200.0, "w": 1600.0, "h": 1000.0,
        }
        assert meta["downsample_width"] == 800
        assert meta["downsample_height"] == 500
        assert meta["physical_width"] == 1600
        assert meta["physical_height"] == 1000
        assert meta["backing_scale"] == pytest.approx(1.0)

    def test_window_coord_translation_adds_origin(self) -> None:
        """A click at the CENTRE of the downsampled frame should land
        at the centre of the window in GLOBAL screen coords \u2014 i.e.
        the window origin must be added after coordinate scaling.
        """
        meta = {
            "capture_mode": "window",
            "window_bounds": {"x": 500.0, "y": 300.0, "w": 1600.0, "h": 1000.0},
            "window_id": 1,
            "owner_name": "rekordbox",
            "title": "",
            "downsample_width": 800,
            "downsample_height": 500,
            "physical_width": 1600,
            "physical_height": 1000,
            "backing_scale": 1.0,
        }
        gx, gy = window_scaled_to_global_points(400, 250, meta)
        assert gx == 500 + 800
        assert gy == 300 + 500

    def test_display_fallback_when_no_window_injected(self) -> None:
        """When no window is injected and no live app matches, we fall
        back to display-mode capture with capture_mode="display".
        """
        raw = _synthetic_png(2560, 1440)
        png, meta = capture_window(
            app_name_substring="__definitely_not_a_real_app__",
            downsample_width=1920,
            _raw_bytes=raw,
        )
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert meta["capture_mode"] == "display"
        assert meta["window_bounds"]["x"] == 0.0
        assert meta["window_bounds"]["y"] == 0.0
        assert meta["downsample_width"] == 1920


class TestClickGuard:
    """``is_point_inside_window`` + Actuator integration enforce Fix 6
    \u2014 clicks outside the Rekordbox window are rejected.
    """

    def test_point_inside_passes(self) -> None:
        meta = {
            "capture_mode": "window",
            "window_bounds": {"x": 100.0, "y": 100.0, "w": 1000.0, "h": 800.0},
        }
        assert is_point_inside_window(500, 500, meta) is True
        assert is_point_inside_window(100, 100, meta) is True
        assert is_point_inside_window(1099, 899, meta) is True

    def test_point_outside_fails(self) -> None:
        meta = {
            "capture_mode": "window",
            "window_bounds": {"x": 100.0, "y": 100.0, "w": 1000.0, "h": 800.0},
        }
        assert is_point_inside_window(50, 50, meta) is False
        assert is_point_inside_window(2000, 500, meta) is False
        assert is_point_inside_window(500, 2000, meta) is False

    def test_display_mode_always_passes(self) -> None:
        """Display-mode captures have no window bounds to enforce;
        the guard is a no-op."""
        meta = {
            "capture_mode": "display",
            "window_bounds": {"x": 0.0, "y": 0.0, "w": 5120.0, "h": 2160.0},
        }
        assert is_point_inside_window(999999, 999999, meta) is True

    def test_actuator_rejects_off_window_click(self, tmp_path: Path) -> None:
        """End-to-end: Actuator.execute_action must reject a left_click
        whose translated global point falls outside the active window,
        return ok=False, and surface a helpful error to Claude.
        """
        tr = Trace.new(tmp_path)
        act = Actuator(
            tr,
            downsample_width=200,
            simulated=True,
            frontmost_app=None,
        )
        act._last_capture_meta = {
            "capture_mode": "window",
            "window_bounds": {"x": 1000.0, "y": 500.0, "w": 400.0, "h": 300.0},
            "window_id": 7,
            "owner_name": "rekordbox",
            "title": "rekordbox",
            "downsample_width": 200,
            "downsample_height": 150,
            "physical_width": 400,
            "physical_height": 300,
            "backing_scale": 1.0,
        }
        act._last_scaled = (200, 150)
        result = act.execute_action(
            {"action": "left_click", "coordinate": [500, 500]}
        )
        assert result["ok"] is False
        assert "outside" in result["error"].lower()
        assert "rekordbox" in result["error"].lower()
        events = sorted(tr.root.glob("step_*_action.json"))
        assert len(events) == 1

    def test_actuator_allows_on_window_click(self, tmp_path: Path) -> None:
        """Click within bounds is accepted (simulated=True so no mouse)."""
        tr = Trace.new(tmp_path)
        act = Actuator(
            tr,
            downsample_width=200,
            simulated=True,
            frontmost_app=None,
        )
        act._last_capture_meta = {
            "capture_mode": "window",
            "window_bounds": {"x": 1000.0, "y": 500.0, "w": 400.0, "h": 300.0},
            "window_id": 7,
            "owner_name": "rekordbox",
            "title": "rekordbox",
            "downsample_width": 200,
            "downsample_height": 150,
            "physical_width": 400,
            "physical_height": 300,
            "backing_scale": 1.0,
        }
        act._last_scaled = (200, 150)
        result = act.execute_action(
            {"action": "left_click", "coordinate": [100, 75]}
        )
        assert result["ok"] is True
        assert result.get("skipped") is True


class TestEnsureFrontmost:
    """Fix 3: re-activate Rekordbox before each click if focus drifted.
    Offline \u2014 we inject the probe + activate callables.
    """

    def test_already_frontmost_short_circuits(self) -> None:
        calls = {"activate": 0, "probe": 0}

        def probe() -> str | None:
            calls["probe"] += 1
            return "rekordbox"

        def activate() -> None:
            calls["activate"] += 1

        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=3,
            wait_ms=0,
            _frontmost_probe=probe,
            _activate=activate,
        )
        assert ok is True
        assert calls["activate"] == 0
        assert calls["probe"] == 1

    def test_activates_when_another_app_focused(self) -> None:
        """First probe shows Safari; activate runs; second probe shows
        rekordbox. Must return True with exactly one activate call."""
        states = iter(["Safari", "rekordbox", "rekordbox"])
        calls = {"activate": 0}

        def probe() -> str | None:
            return next(states)

        def activate() -> None:
            calls["activate"] += 1

        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=3,
            wait_ms=0,
            _frontmost_probe=probe,
            _activate=activate,
        )
        assert ok is True
        assert calls["activate"] == 1

    def test_returns_false_after_max_attempts(self) -> None:
        calls = {"activate": 0}

        def probe() -> str | None:
            return "Safari"

        def activate() -> None:
            calls["activate"] += 1

        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=3,
            wait_ms=0,
            _frontmost_probe=probe,
            _activate=activate,
        )
        assert ok is False
        assert calls["activate"] == 3

    def test_case_insensitive_match(self) -> None:
        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=1,
            wait_ms=0,
            _frontmost_probe=lambda: "Rekordbox",
            _activate=lambda: None,
        )
        assert ok is True


# --------------------------------------------------------------------------- #
# Live run — only when RB_AGENT_LIVE=1 (and Rekordbox is open).
# This is the "it actually talks to Anthropic and actually clicks" gate.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("RB_AGENT_LIVE") != "1",
    reason="Set RB_AGENT_LIVE=1 to run the live agent smoke against Rekordbox",
)
def test_agent_live_smoke(tmp_path: Path) -> None:  # pragma: no cover
    """Last-ditch check: run a live dry-run agent against Rekordbox.

    This is gated because it costs money (≤ $0.50/run) and requires the
    user to have Rekordbox 7 running. CI never sets RB_AGENT_LIVE.
    """
    from apps.sync.usb.pioneer.agent import AgentConfig, run_export_agent

    cfg = AgentConfig(
        playlist=os.environ.get("RB_AGENT_PLAYLIST", "UL Percussion"),
        usb_path=os.environ.get("RB_AGENT_USB", "/Volumes/MAINTAINER"),
        dry_run=True,
        max_steps=8,
        trace_dir=tmp_path,
    )
    result = run_export_agent(cfg)
    assert result.model.startswith("claude-")
    assert result.steps >= 1
