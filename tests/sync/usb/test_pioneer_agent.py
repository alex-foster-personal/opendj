"""Tests for Prototype C -- Claude computer-use Rekordbox export agent.

These tests stay strictly offline by default. The live-run smoke is gated
behind the ``RB_AGENT_LIVE=1`` env var so CI can't accidentally trigger a
real USB write or burn Anthropic credits.

Requirement: CAT-06 -- Rekordbox 7 USB export automation prototype.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
    DisplayGeometry,
    Trace,
    _translate_key_combo,
    scaled_to_points,
    take_screenshot,
)

# --------------------------------------------------------------------------- #
# Coordinate scaling -- pure math, no GUI needed.
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
        in macOS points -- the bottom-right corner."""
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
# Screenshot smoke -- real Quartz capture.
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
# Agent loop -- mocked API client.
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
            # This test is offline by contract ("no network or GUI
            # needed"). Leaving frontmost_app at its "rekordbox" default
            # ran the real preflight activate and COLD-LAUNCHED Rekordbox
            # on every suite run - the bug fixed alongside this test.
            frontmost_app=None,
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

    def test_offline_flow_never_activates_an_app(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression (Sun 31 Aug 2026): this offline flow used to run the
        real frontmost preflight, and AppleScript ``activate`` LAUNCHES a
        stopped app - so `pytest tests/` cold-started Rekordbox (MyTag +
        library scan) on every full-suite run, on every machine, with no
        hardware involved. The suite must drive zero GUI activations.

        ``frontmost_app=None`` is the real guard here (``run_export_agent``
        only reaches ``ensure_frontmost`` when it is set) - not a
        monkeypatched substitute for one. Earlier this test also replaced
        ``ensure_frontmost`` wholesale, which proved only that the FAKE was
        never called and hid the real running-app/activation path from
        coverage entirely. This version leaves ``ensure_frontmost`` real
        and asserts at the actual OS boundary it would use to activate
        (``subprocess.run``, the ``osascript`` dispatcher), so a regression
        that calls activation unconditionally shows up as a real
        invocation attempt rather than vanishing into a mock.
        """
        from apps.sync.usb.pioneer import agent as agent_mod
        from apps.sync.usb.pioneer import agent_actuator as actuator_mod
        from apps.sync.usb.pioneer.agent import AgentConfig, run_export_agent

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
        activations: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **kw: Any) -> SimpleNamespace:
            activations.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(actuator_mod.subprocess, "run", _fake_subprocess_run)

        mock_client = MagicMock()
        resp = SimpleNamespace(
            id="msg_1",
            stop_reason="end_turn",
            content=[
                SimpleNamespace(type="text", text="END_TURN: DRY-RUN COMPLETE")
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        mock_client.beta.messages.create.side_effect = [resp, resp]

        cfg = AgentConfig(
            playlist="UL Percussion",
            usb_path="/Volumes/MAINTAINER",
            dry_run=True,
            max_steps=1,
            trace_dir=tmp_path,
            api_key="sk-test-xxx",
            frontmost_app=None,
        )
        run_export_agent(cfg, client=mock_client)

        assert activations == [], (
            f"offline test invoked a real activation subprocess {activations} "
            "- it will open Rekordbox"
        )

    def test_preflight_reaches_real_activation_when_frontmost_app_set(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Companion to the regression above: proves ``frontmost_app=None``
        is doing real work rather than a guard that happens to never fire.
        With ``frontmost_app`` set, ``run_export_agent``'s real preflight
        must reach ``ensure_frontmost``'s real activation branch - stubbed
        only at the OS boundary (``subprocess.run``, ``_frontmost_app_name``),
        never at ``ensure_frontmost`` itself.
        """
        from apps.sync.usb.pioneer import agent as agent_mod
        from apps.sync.usb.pioneer import agent_actuator as actuator_mod
        from apps.sync.usb.pioneer.agent import AgentConfig, run_export_agent

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
        activations: list[list[str]] = []

        def _fake_subprocess_run(args: list[str], **kw: Any) -> SimpleNamespace:
            activations.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(actuator_mod.subprocess, "run", _fake_subprocess_run)
        # ensure_frontmost checks _frontmost_app_name() before each
        # activate attempt; force "never frontmost" so the real loop
        # actually reaches activate rather than short-circuiting.
        monkeypatch.setattr(actuator_mod, "_frontmost_app_name", lambda: None)

        mock_client = MagicMock()
        resp = SimpleNamespace(
            id="msg_1",
            stop_reason="end_turn",
            content=[
                SimpleNamespace(type="text", text="END_TURN: DRY-RUN COMPLETE")
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        mock_client.beta.messages.create.side_effect = [resp, resp]

        cfg = AgentConfig(
            playlist="UL Percussion",
            usb_path="/Volumes/MAINTAINER",
            dry_run=True,
            max_steps=1,
            trace_dir=tmp_path,
            api_key="sk-test-xxx",
            frontmost_app="rekordbox",
        )
        run_export_agent(cfg, client=mock_client)

        assert activations, (
            "preflight with frontmost_app set never reached real "
            "activation - the offline regression's guard may be a no-op"
        )
        assert all(args[:2] == ["osascript", "-e"] for args in activations)
