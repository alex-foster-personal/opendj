"""Tests for Prototype C window capture, click-guard, and frontmost
enforcement (Fixes 1/3/6) -- split out of ``test_pioneer_agent.py`` to keep
both files under the repo's 600-line file-size ratchet (Thu 3 Sep 2026).

These tests stay strictly offline. See ``test_pioneer_agent.py`` for the
coordinate-math, screenshot, and dry-run-init coverage, and its module
docstring for why the Darwin skip must precede the PIL / agent_actuator
imports.

Requirement: CAT-06 -- Rekordbox 7 USB export automation prototype.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = [pytest.mark.requirement("CAT-06")]

_IS_DARWIN = sys.platform == "darwin"

if not _IS_DARWIN:  # pragma: no cover
    pytest.skip(
        "Prototype C agent is macOS-only (Quartz + cliclick).",
        allow_module_level=True,
    )


from PIL import Image  # noqa: E402

from apps.sync.usb.pioneer.agent_actuator import (  # noqa: E402
    Actuator,
    Trace,
    capture_window,
    ensure_frontmost,
    is_point_inside_window,
    quit_app,
    window_scaled_to_global_points,
)


def _synthetic_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Window-isolated capture + click-guard + frontmost enforcement (Fixes 1/3/6)
# --------------------------------------------------------------------------- #


class TestWindowIsolatedCapture:
    """``capture_window`` must emit the documented metadata shape in
    both window and display-fallback modes, and (critically) must
    translate clicks back to GLOBAL screen points using the window's
    origin -- not just the display frame.
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
        at the centre of the window in GLOBAL screen coords -- i.e.
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
    -- clicks outside the Rekordbox window are rejected.
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

    def test_actuator_rejects_click_when_focus_enforcement_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """P1 BLOCKING (PR #549 review): ``execute_action`` used to discard
        ``ensure_frontmost``'s return value and dispatch ``cliclick``
        regardless - if Rekordbox quit or crashed between the screenshot
        and the click, the click would land in whatever app now occupies
        that screen position. Only the true OS boundary
        (``_find_running_app``, the NSWorkspace query) is stubbed to
        report "not running"; ``ensure_frontmost`` and ``execute_action``
        run for real, ``simulated=False`` so the enforcement branch is not
        skipped.
        """
        from apps.sync.usb.pioneer import agent_actuator as actuator_mod

        monkeypatch.setattr(actuator_mod, "_find_running_app", lambda app: None)
        cliclick_calls: list[list[str]] = []

        def _fake_run_cliclick(args: list[str]) -> SimpleNamespace:
            cliclick_calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(actuator_mod, "_run_cliclick", _fake_run_cliclick)

        tr = Trace.new(tmp_path)
        act = Actuator(
            tr,
            downsample_width=200,
            simulated=False,
            frontmost_app="rekordbox",
        )
        act._last_capture_meta = {
            "capture_mode": "display",
            "window_bounds": {"x": 0.0, "y": 0.0, "w": 5120.0, "h": 2160.0},
            "downsample_width": 200,
            "downsample_height": 150,
            "physical_width": 5120,
            "physical_height": 2160,
            "backing_scale": 1.0,
        }
        act._last_scaled = (200, 150)
        result = act.execute_action(
            {"action": "left_click", "coordinate": [100, 75]}
        )
        assert result["ok"] is False
        assert "frontmost" in result["error"].lower()
        assert cliclick_calls == [], (
            f"cliclick dispatched {cliclick_calls} despite failed focus check"
        )

    @pytest.mark.parametrize(
        "action_payload",
        [
            pytest.param(
                {
                    "action": "left_click_drag",
                    "start_coordinate": [10, 10],
                    "coordinate": [100, 75],
                },
                id="left_click_drag",
            ),
            pytest.param({"action": "type", "text": "hello"}, id="type"),
            pytest.param({"action": "key", "text": "cmd+c"}, id="key"),
            pytest.param(
                {
                    "action": "scroll",
                    "coordinate": [100, 75],
                    "scroll_direction": "down",
                    "scroll_amount": 3,
                },
                id="scroll",
            ),
        ],
    )
    def test_actuator_rejects_non_click_input_when_focus_enforcement_fails(
        self, tmp_path: Path, monkeypatch, action_payload: dict
    ) -> None:
        """P1 BLOCKING (PR #549 review): the frontmost guard was invoked
        only from the click branch of ``execute_action``, so
        ``left_click_drag``, ``type``, ``key``, and ``scroll`` all
        dispatched real input via cliclick/System Events even when
        Rekordbox had quit or lost focus after the screenshot. Same real
        ``ensure_frontmost`` path as the click regression test above --
        only the OS boundary (``_find_running_app``) is stubbed.
        """
        from apps.sync.usb.pioneer import agent_actuator as actuator_mod

        monkeypatch.setattr(actuator_mod, "_find_running_app", lambda app: None)
        cliclick_calls: list[list[str]] = []

        def _fake_run_cliclick(args: list[str]) -> SimpleNamespace:
            cliclick_calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(actuator_mod, "_run_cliclick", _fake_run_cliclick)
        osascript_calls: list[list[str]] = []

        def _fake_subprocess_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
            osascript_calls.append(list(args[0]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(actuator_mod.subprocess, "run", _fake_subprocess_run)

        tr = Trace.new(tmp_path)
        act = Actuator(
            tr,
            downsample_width=200,
            simulated=False,
            frontmost_app="rekordbox",
        )
        act._last_capture_meta = {
            "capture_mode": "display",
            "window_bounds": {"x": 0.0, "y": 0.0, "w": 5120.0, "h": 2160.0},
            "downsample_width": 200,
            "downsample_height": 150,
            "physical_width": 5120,
            "physical_height": 2160,
            "backing_scale": 1.0,
        }
        act._last_scaled = (200, 150)
        result = act.execute_action(action_payload)
        assert result["ok"] is False
        assert "frontmost" in result["error"].lower()
        assert cliclick_calls == [], (
            f"cliclick dispatched {cliclick_calls} despite failed focus check"
        )
        assert osascript_calls == [], (
            f"osascript dispatched {osascript_calls} despite failed focus check"
        )


class TestEnsureFrontmost:
    """Fix 3: re-activate Rekordbox before each click if focus drifted.
    Offline -- we inject the probe + activate callables.
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
            _running_probe=lambda: True,
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
            _running_probe=lambda: True,
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
            _running_probe=lambda: True,
        )
        assert ok is False
        assert calls["activate"] == 3

    def test_does_not_launch_a_stopped_app(self) -> None:
        """THE regression: AppleScript ``activate`` launches a stopped app,
        so a focus check on a closed Rekordbox must do NOTHING. If this
        fails, running the test suite cold-starts Rekordbox again."""
        calls = {"activate": 0}

        def activate() -> None:
            calls["activate"] += 1

        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=3,
            wait_ms=0,
            _frontmost_probe=lambda: "Terminal",
            _activate=activate,
            _running_probe=lambda: False,
        )
        assert ok is False
        assert calls["activate"] == 0

    def test_allow_launch_opts_in_to_starting_the_app(self) -> None:
        """The export preflight genuinely wants the app up - and only it
        passes allow_launch=True."""
        states = iter(["Terminal", "rekordbox"])
        calls = {"activate": 0}

        def activate() -> None:
            calls["activate"] += 1

        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=3,
            wait_ms=0,
            allow_launch=True,
            _frontmost_probe=lambda: next(states),
            _activate=activate,
            _running_probe=lambda: False,
        )
        assert ok is True
        assert calls["activate"] == 1

    def test_case_insensitive_match(self) -> None:
        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=1,
            wait_ms=0,
            _frontmost_probe=lambda: "Rekordbox",
            _activate=lambda: None,
            _running_probe=lambda: True,
        )
        assert ok is True

    def test_rejects_substring_match(self) -> None:
        """P2 BLOCKING (PR #549 review): the frontmost confirmation
        predicate used to accept any frontmost name CONTAINING the needle
        (``needle in current.lower()``), so a background helper process
        named e.g. "rekordboxAgent" would satisfy a check meant to confirm
        the real app is frontmost, and the next click would land on
        whatever ELSE actually has focus. Must require exact (case
        insensitive) equality, matching ``_find_running_app``.
        """
        ok = ensure_frontmost(
            "rekordbox",
            max_attempts=1,
            wait_ms=0,
            _frontmost_probe=lambda: "rekordboxAgent",
            _activate=lambda: None,
            _running_probe=lambda: True,
        )
        assert ok is False


class TestQuitApp:
    """Quit-after (MDT_RB_QUIT_AFTER=1). Offline; probes injected.
    Regression: quits a not-running app = broken; returns True while the
    process survives = broken; never sends the AppleScript quit = broken.
    """

    @staticmethod
    def _run(
        states: list[bool], *, max_wait_s: float, poll_s: float
    ) -> tuple[bool, int]:
        seq = iter(states)
        calls = {"quit": 0}

        def do_quit() -> None:
            calls["quit"] += 1

        ok = quit_app(
            "rekordbox",
            max_wait_s=max_wait_s,
            poll_s=poll_s,
            _quit=do_quit,
            _running_probe=lambda: next(seq),
        )
        return ok, calls["quit"]

    def test_noop_when_not_running(self) -> None:
        ok, quits = self._run([False], max_wait_s=0.0, poll_s=0.0)
        assert (ok, quits) == (True, 0)

    def test_quits_and_waits_for_exit(self) -> None:
        ok, quits = self._run([True, False], max_wait_s=5.0, poll_s=0.0)
        assert (ok, quits) == (True, 1)

    def test_returns_false_when_process_survives(self) -> None:
        ok, quits = self._run([True] * 60, max_wait_s=0.05, poll_s=0.01)
        assert (ok, quits) == (False, 1)


# --------------------------------------------------------------------------- #
# Live run -- only when RB_AGENT_LIVE=1 (and Rekordbox is open).
# This is the "it actually talks to Anthropic and actually clicks" gate.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("RB_AGENT_LIVE") != "1",
    reason="Set RB_AGENT_LIVE=1 to run the live agent smoke against Rekordbox",
)
def test_agent_live_smoke(tmp_path: Path) -> None:  # pragma: no cover
    """Last-ditch check: run a live dry-run agent against Rekordbox.

    This is gated because it costs money (<= $0.50/run) and requires the
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
