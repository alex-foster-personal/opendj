"""Unit tests for apps.voice.actions (VOICE-01)."""
from __future__ import annotations

import time

import pytest

from apps.voice import actions, grammar


pytestmark = pytest.mark.requirement("VOICE-01")


def _intent(kind: str, slots: dict | None = None, raw: str = "") -> grammar.Intent:
    return grammar.Intent(kind=kind, slots=slots or {}, raw_transcript=raw)


def _published(bus, kind: str):
    return [e for e in bus.events if e.get("kind") == kind]


# ----- Read-only handlers --------------------------------------------------


class TestHandleSearch:
    def test_publishes_search_event_and_speaks(self, voice_context):
        intent = _intent("SEARCH", {"query": "daft punk"}, raw="find daft punk")
        resp = actions.handle_search(intent, voice_context)
        assert resp.published is True
        evts = _published(voice_context.event_bus, "SEARCH")
        assert len(evts) == 1
        assert evts[0]["slots"]["query"] == "daft punk"
        assert "searching for daft punk" in voice_context.tts_engine.spoken[0]

    def test_empty_query(self, voice_context):
        resp = actions.handle_search(_intent("SEARCH", {"query": "   "}), voice_context)
        assert resp.published is False
        assert "didn't catch" in voice_context.tts_engine.spoken[0]


class TestHandleReadBpm:
    def test_no_deck_state(self, voice_context):
        resp = actions.handle_read_bpm(_intent("READ_BPM"), voice_context)
        assert resp.published is False
        assert "no deck playing" in voice_context.tts_engine.spoken[0]

    def test_speaks_rounded_bpm(self, voice_context):
        voice_context.event_bus.publish({"kind": "deck_state", "bpm": 128.4})
        voice_context.tts_engine.spoken.clear()
        resp = actions.handle_read_bpm(_intent("READ_BPM"), voice_context)
        assert resp.published is True
        assert voice_context.tts_engine.spoken[-1] == "128 BPM"

    def test_missing_bpm_field(self, voice_context):
        voice_context.event_bus.publish({"kind": "deck_state"})
        voice_context.tts_engine.spoken.clear()
        resp = actions.handle_read_bpm(_intent("READ_BPM"), voice_context)
        assert resp.published is False
        assert "bpm unknown" in voice_context.tts_engine.spoken[0]


class TestHandleReadKey:
    def test_no_deck_state(self, voice_context):
        resp = actions.handle_read_key(_intent("READ_KEY"), voice_context)
        assert resp.published is False

    def test_reads_camelot(self, voice_context):
        voice_context.event_bus.publish({"kind": "deck_state", "camelot": "8A"})
        voice_context.tts_engine.spoken.clear()
        resp = actions.handle_read_key(_intent("READ_KEY"), voice_context)
        assert resp.published is True
        assert "8A" in voice_context.tts_engine.spoken[-1]


class TestHandleAdvanceQueue:
    def test_publishes_and_speaks(self, voice_context):
        resp = actions.handle_advance_queue(_intent("ADVANCE_QUEUE"), voice_context)
        assert resp.published is True
        assert _published(voice_context.event_bus, "ADVANCE_QUEUE")
        assert voice_context.tts_engine.spoken[-1] == "advancing queue"


class TestHandleMuteUnmute:
    def test_mute_sets_mute_until(self, voice_context):
        actions.handle_mute_voice(_intent("MUTE_VOICE"), voice_context)
        assert voice_context.mute_until is not None
        assert voice_context.is_muted()

    def test_unmute_clears(self, voice_context):
        voice_context.mute_until = time.time() + 600
        actions.handle_unmute_voice(_intent("UNMUTE_VOICE"), voice_context)
        assert voice_context.mute_until is None


# ----- Destructive handlers (Plan 2 scope: always dry_run) ----------------


class TestHandleSaveCueDestructiveOff:
    def test_no_recent_transition(self, voice_context):
        resp = actions.handle_save_cue(_intent("SAVE_CUE"), voice_context)
        assert resp.published is False
        assert "no recent transition" in voice_context.tts_engine.spoken[0]

    def test_dry_run_when_destructive_off(self, voice_context):
        voice_context.event_bus.publish(
            {
                "kind": "transition",
                "slots": {"track_from": "abc", "track_to": "def"},
                "ts_epoch": time.time(),
            }
        )
        voice_context.tts_engine.spoken.clear()
        resp = actions.handle_save_cue(_intent("SAVE_CUE"), voice_context)
        assert resp.published is True
        assert resp.dry_run is True
        evts = _published(voice_context.event_bus, "SAVE_CUE")
        assert evts[-1]["dry_run"] is True
        assert "destructive mode off" in voice_context.tts_engine.spoken[-1]


class TestHandleRateTrackDestructiveOff:
    def test_out_of_range(self, voice_context):
        resp = actions.handle_rate_track(
            _intent("RATE_TRACK", {"stars": 7}), voice_context
        )
        assert resp.published is False
        assert "1 to 5" in voice_context.tts_engine.spoken[-1]

    def test_invalid_stars(self, voice_context):
        resp = actions.handle_rate_track(
            _intent("RATE_TRACK", {"stars": "many"}), voice_context
        )
        assert resp.published is False
        assert "didn't catch" in voice_context.tts_engine.spoken[-1]

    def test_no_deck_state(self, voice_context):
        resp = actions.handle_rate_track(
            _intent("RATE_TRACK", {"stars": 5}), voice_context
        )
        assert resp.published is False

    def test_dry_run_when_destructive_off(self, voice_context):
        voice_context.event_bus.publish(
            {"kind": "deck_state", "stable_id": "stable_xyz", "bpm": 128}
        )
        voice_context.tts_engine.spoken.clear()
        resp = actions.handle_rate_track(
            _intent("RATE_TRACK", {"stars": 5}), voice_context
        )
        assert resp.published is True
        assert resp.dry_run is True
        evts = _published(voice_context.event_bus, "RATE_TRACK")
        assert evts[-1]["dry_run"] is True
        assert evts[-1]["slots"]["stable_id"] == "stable_xyz"
        assert evts[-1]["slots"]["stars"] == 5


# ----- Registry dispatch ---------------------------------------------------


class TestRegistry:
    def test_dispatch_routes_by_kind(self, voice_context):
        registry = actions.default_registry()
        voice_context.event_bus.publish({"kind": "deck_state", "bpm": 128})
        voice_context.tts_engine.spoken.clear()
        resp = registry.dispatch(_intent("READ_BPM"), voice_context)
        assert resp.published is True

    def test_muted_blocks_non_unmute(self, voice_context):
        voice_context.mute_until = time.time() + 600
        registry = actions.default_registry()
        resp = registry.dispatch(_intent("SEARCH", {"query": "x"}), voice_context)
        assert resp.published is False
        assert resp.meta and resp.meta.get("muted") is True

    def test_muted_allows_unmute(self, voice_context):
        voice_context.mute_until = time.time() + 600
        registry = actions.default_registry()
        resp = registry.dispatch(_intent("UNMUTE_VOICE"), voice_context)
        assert resp.published is True

    def test_debounce_blocks_second_call(self, voice_context):
        voice_context.debounce_s = 5.0
        registry = actions.default_registry()
        r1 = registry.dispatch(_intent("SEARCH", {"query": "a"}), voice_context)
        assert r1.published is True
        r2 = registry.dispatch(_intent("SEARCH", {"query": "b"}), voice_context)
        assert r2.published is False
        assert r2.meta and r2.meta.get("debounced") is True

    def test_unknown_intent_returns_no_handler(self, voice_context):
        registry = actions.Registry(handlers={})
        resp = registry.dispatch(_intent("MYSTERY"), voice_context)
        assert resp.published is False
        assert "no_handler" in resp.reply


# ----- Parse -> dispatch end-to-end ----------------------------------------


class TestEndToEnd:
    @pytest.mark.parametrize(
        "transcript,expected_kind",
        [
            ("find techno", "SEARCH"),
            ("mute voice", "MUTE_VOICE"),
            ("unmute voice", "UNMUTE_VOICE"),
            ("next track", "ADVANCE_QUEUE"),
        ],
    )
    def test_parse_to_dispatch(self, voice_context, transcript, expected_kind):
        registry = actions.default_registry()
        intent = grammar.parse(transcript)
        assert intent is not None
        resp = registry.dispatch(intent, voice_context)
        assert resp.published is True
        assert any(
            e.get("kind") == expected_kind for e in voice_context.event_bus.events
        )
        # Reset mute if we just muted, so the next iteration is not blocked.
        voice_context.unmute()
        voice_context.last_dispatch_at = None
