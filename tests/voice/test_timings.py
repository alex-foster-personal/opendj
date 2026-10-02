"""Unit tests for apps.voice.timings (VOICE-01)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.voice import timings

pytestmark = pytest.mark.requirement("VOICE-01")


class TestTimings:
    def test_measure_captures_stage(self):
        t = timings.Timings()
        with t.measure("wake"):
            pass
        assert "wake" in t.stages
        assert t.stages["wake"] >= 0

    def test_record_accumulates(self):
        t = timings.Timings()
        t.record("stt", 100.0)
        t.record("stt", 50.0)
        assert t.stages["stt"] == pytest.approx(150.0)

    def test_total_and_dict(self):
        t = timings.Timings()
        t.record("wake", 10.0)
        t.record("stt", 90.0)
        out = t.to_dict()
        assert out["wake"] == 10.0
        assert out["total_ms"] == 100.0


class TestAppendJsonl:
    def test_appends_line(self, tmp_path: Path):
        p = tmp_path / "sub" / "timings.jsonl"
        timings.append_jsonl(p, {"stage": "wake", "ms": 10})
        timings.append_jsonl(p, {"stage": "stt", "ms": 900})
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["stage"] == "wake"


class TestBudgetWarning:
    def test_under_budget_no_warn(self):
        assert timings.budget_warning(500.0) is None

    def test_over_budget_returns_message(self):
        msg = timings.budget_warning(2_500.0)
        assert msg is not None
        assert "exceeded" in msg


class TestDefaultLogPath:
    def test_env_override(self):
        p = timings.default_log_path(env={"VOICE_TIMINGS_LOG": "/tmp/custom.log"})
        # Path() normalises separators per-OS; compare Paths, not strings.
        assert p == Path("/tmp/custom.log")

    def test_default_points_at_data_voice(self):
        # INSTALL-30: the data dir (``<repo>/data`` in a checkout), never the
        # source tree, which is the signed payload in the installed app.
        from apps.shared import platform_paths

        p = timings.default_log_path(env={})
        assert p == Path(platform_paths.DATA_DIR) / "voice" / "timings.jsonl"
