"""Destructive-path integration tests for SAVE_CUE + RATE_TRACK (VOICE-01)."""
from __future__ import annotations

import time

import pytest

from apps.voice import actions, confirm, grammar
from apps.voice import tts as tts_mod

pytestmark = pytest.mark.requirement("VOICE-01")


def _intent(kind: str, slots: dict | None = None, raw: str = "") -> grammar.Intent:
    return grammar.Intent(kind=kind, slots=slots or {}, raw_transcript=raw)


def _make_confirm_fn(answers: list[str], tmp_path):
    """Build a confirm_fn that pulls answers from a queue + logs."""
    remaining = list(answers)

    def fake_listen(timeout_s: float) -> str:
        return remaining.pop(0) if remaining else ""

    engine = confirm.ConfirmEngine(
        tts_engine=tts_mod.RecordingTts(),
        listen_fn=fake_listen,
        log_path=tmp_path / "confirmations.jsonl",
        timeout_s=0.1,
    )
    return confirm.bind_confirm(engine)


# ----- SAVE_CUE ------------------------------------------------------------


class TestSaveCueDestructiveOn:
    def test_confirm_yes_publishes_live_event(self, voice_context, tmp_path):
        voice_context.destructive = True
        voice_context.event_bus.publish(
            {
                "kind": "transition",
                "slots": {"track_from": "A", "track_to": "B"},
                "ts_epoch": time.time(),
            }
        )
        voice_context.tts_engine.spoken.clear()
        confirm_fn = _make_confirm_fn(["yes"], tmp_path)
        resp = actions.handle_save_cue(
            _intent("SAVE_CUE"), voice_context, confirm_fn=confirm_fn
        )
        assert resp.published is True
        assert resp.dry_run is False
        events = [e for e in voice_context.event_bus.events if e["kind"] == "SAVE_CUE"]
        assert events
        assert events[-1]["dry_run"] is False
        assert "saved cue points" in voice_context.tts_engine.spoken[-1]

    def test_confirm_no_publishes_cancelled_event(self, voice_context, tmp_path):
        voice_context.destructive = True
        voice_context.event_bus.publish(
            {
                "kind": "transition",
                "slots": {},
                "ts_epoch": time.time(),
            }
        )
        voice_context.tts_engine.spoken.clear()
        confirm_fn = _make_confirm_fn(["no"], tmp_path)
        resp = actions.handle_save_cue(
            _intent("SAVE_CUE"), voice_context, confirm_fn=confirm_fn
        )
        assert resp.reply == "save_cue_cancelled"
        cancelled = [
            e for e in voice_context.event_bus.events if e["kind"] == "SAVE_CUE_CANCELLED"
        ]
        assert cancelled
        live = [
            e
            for e in voice_context.event_bus.events
            if e["kind"] == "SAVE_CUE" and not e["dry_run"]
        ]
        assert not live
        assert "cancelled" in voice_context.tts_engine.spoken[-1]

    def test_stale_transition_does_not_publish(self, voice_context, tmp_path):
        voice_context.destructive = True
        voice_context.event_bus.publish(
            {
                "kind": "transition",
                "slots": {},
                "ts_epoch": time.time() - 11 * 60,  # 11 minutes ago
            }
        )
        voice_context.tts_engine.spoken.clear()
        confirm_fn = _make_confirm_fn(["yes"], tmp_path)
        resp = actions.handle_save_cue(
            _intent("SAVE_CUE"), voice_context, confirm_fn=confirm_fn
        )
        assert resp.published is False
        assert "no recent transition" in voice_context.tts_engine.spoken[-1]


# ----- RATE_TRACK ----------------------------------------------------------


class TestRateTrackDestructiveOn:
    def test_confirm_yes_publishes_live(self, voice_context, tmp_path):
        voice_context.destructive = True
        voice_context.event_bus.publish(
            {"kind": "deck_state", "stable_id": "xyz", "bpm": 128}
        )
        voice_context.tts_engine.spoken.clear()
        confirm_fn = _make_confirm_fn(["yes"], tmp_path)
        resp = actions.handle_rate_track(
            _intent("RATE_TRACK", {"stars": 5}),
            voice_context,
            confirm_fn=confirm_fn,
        )
        assert resp.published is True
        assert resp.dry_run is False
        events = [e for e in voice_context.event_bus.events if e["kind"] == "RATE_TRACK"]
        assert events
        assert events[-1]["slots"]["stars"] == 5
        assert events[-1]["slots"]["stable_id"] == "xyz"
        assert events[-1]["dry_run"] is False

    def test_confirm_no_publishes_cancelled(self, voice_context, tmp_path):
        voice_context.destructive = True
        voice_context.event_bus.publish(
            {"kind": "deck_state", "stable_id": "xyz"}
        )
        voice_context.tts_engine.spoken.clear()
        confirm_fn = _make_confirm_fn(["nope"], tmp_path)
        resp = actions.handle_rate_track(
            _intent("RATE_TRACK", {"stars": 4}),
            voice_context,
            confirm_fn=confirm_fn,
        )
        assert resp.reply == "rate_track_cancelled"
        cancelled = [
            e for e in voice_context.event_bus.events if e["kind"] == "RATE_TRACK_CANCELLED"
        ]
        assert cancelled


class TestDestructiveDefaultOff:
    def test_cli_flag_must_be_explicit(self):
        """Guard against regressions: --enable-destructive default must be off."""
        from apps.voice import __main__ as cli

        parser = cli.build_parser()
        ns = parser.parse_args(["run"])
        assert ns.enable_destructive is False
