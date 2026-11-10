"""JSON Schema validation for Phase 12 API responses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from apps.sets import paths as sets_paths

SCHEMA_DIR = Path(sets_paths.__file__).parent / "schemas"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.requirement("SET-03")
def test_timeline_event_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_load("timeline_event.schema.json"))


@pytest.mark.requirement("SET-03")
def test_transition_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_load("transition.schema.json"))


@pytest.mark.requirement("SET-03")
def test_session_summary_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_load("session_summary.schema.json"))


@pytest.mark.requirement("SET-03")
def test_session_manifest_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_load("session_manifest.schema.json"))


@pytest.mark.requirement("SET-03")
def test_timeline_event_schema_accepts_canonical_sample():
    schema = _load("timeline_event.schema.json")
    validator = Draft202012Validator(schema)
    sample = {
        "session_id": "2026-04-17T21-30-00",
        "timestamp_s": 12.5,
        "wall_clock": "2026-04-17T21:30:12.500+00:00",
        "deck": "A",
        "track_stable_id": "uuid-1",
        "action": "track_loaded",
        "source": "djay_monitor",
        "value": {"title": "Track 1"},
    }
    validator.validate(sample)


@pytest.mark.requirement("SET-03")
def test_transition_schema_accepts_canonical_sample():
    schema = _load("transition.schema.json")
    validator = Draft202012Validator(schema)
    sample = {
        "idx": 0,
        "t_start_s": 0.0,
        "t_change_s": 10.0,
        "t_end_s": 12.0,
        "from_deck": "A",
        "to_deck": "B",
        "from_track": "t1",
        "to_track": "t2",
        "predicted_class": "cut",
        "confidence": 0.9,
        "model_version": "rules-v0",
        "features": {
            "overlap_s": 0.2,
            "fade_s": 0.1,
            "incoming_preload_s": 1.0,
            "outgoing_trail_s": 0.1,
            "time_since_prev_transition_s": 0.0,
            "is_same_deck_reload": 0.0,
        },
    }
    validator.validate(sample)


@pytest.mark.requirement("SET-03")
def test_transition_schema_rejects_unknown_class():
    schema = _load("transition.schema.json")
    validator = Draft202012Validator(schema)
    sample = {
        "idx": 0,
        "t_start_s": 0.0,
        "t_change_s": 10.0,
        "t_end_s": 11.0,
        "predicted_class": "mystery",
        "confidence": 0.5,
        "model_version": "rules-v0",
        "features": {
            "overlap_s": 1.0,
            "fade_s": 1.0,
            "incoming_preload_s": 0.0,
            "outgoing_trail_s": 1.0,
            "time_since_prev_transition_s": 0.0,
            "is_same_deck_reload": 0.0,
        },
    }
    errors = list(validator.iter_errors(sample))
    assert any("predicted_class" in str(e.path) or "mystery" in str(e) for e in errors)


@pytest.mark.requirement("SET-03")
def test_session_summary_schema_accepts_canonical_sample():
    schema = _load("session_summary.schema.json")
    Draft202012Validator(schema).validate(
        {
            "session_id": "s1",
            "started_at": "2026-04-17T21:30:00+00:00",
            "ended_at": "2026-04-17T22:15:00+00:00",
            "duration_s": 2700.0,
            "event_count": 42,
            "transition_count": 3,
            "share_state": "private",
        }
    )


def _manifest_sample(**levels) -> dict:
    return {
        "session_id": "s",
        "started_at": "2026-11-10T00:00:00Z",
        "ended_at": None,
        "capture_device": "d",
        "share_state": "private",
        "event_count": 0,
        "deck_sources": [],
        "mp3_segments": [],
        "watermark": "w",
        "schema_version": 1,
        **levels,
    }


@pytest.mark.requirement("SET-11744")
@pytest.mark.parametrize(
    "levels",
    [
        {"audio_peak_dbfs": -6.02, "clipped_samples": 400, "clip_events": 1},
        {"audio_peak_dbfs": None, "clipped_samples": None, "clip_events": None},
        {},
    ],
)
def test_session_manifest_schema_accepts_level_fields(levels):
    """[if] numeric, null or absent (legacy) level fields fail the manifest schema [then] broken."""
    Draft202012Validator(_load("session_manifest.schema.json")).validate(_manifest_sample(**levels))


@pytest.mark.requirement("SET-11744")
@pytest.mark.parametrize("levels", [{"clipped_samples": -1}, {"audio_peak_dbfs": 1.5}, {"clip_events": 0.5}])
def test_session_manifest_schema_rejects_bad_level_fields(levels):
    """[if] a negative clip count or a peak above 0 dBFS passes the manifest schema [then] broken."""
    assert not Draft202012Validator(_load("session_manifest.schema.json")).is_valid(_manifest_sample(**levels))
