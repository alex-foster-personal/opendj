"""SETREC-03: control-event schema v2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from apps.sets import control_events as ce
from apps.sets import paths as sets_paths


def _load_schema() -> dict:
    path = Path(sets_paths.__file__).parent / "schemas" / "control_event.v2.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.requirement("SETREC-11805")
def test_control_event_v2_schema_is_valid_draft_2020_12():
    """[if] v2 schema loads [then] it is draft 2020-12, [else stop]."""
    Draft202012Validator.check_schema(_load_schema())


@pytest.mark.requirement("SETREC-11805")
def test_control_event_v2_rejects_missing_required_field():
    """[if] a line omits a required v2 field [then] validation fails, [else stop]."""
    sample = {
        "v": 2,
        "session_id": "2026-04-17T21-30-00",
        "seq": 0,
        "sample": 100,
        "sample_rate": 48000,
        "wall_clock": "2026-04-17T21:30:01.000+00:00",
        "control": "deck.1.play",
        "kind": "set",
        "source": "user",
    }
    errors = list(Draft202012Validator(_load_schema()).iter_errors(sample))
    assert errors, "missing value must fail schema"


@pytest.mark.requirement("SETREC-11805")
def test_control_event_v2_accepts_canonical_sample():
    """[if] row has every required v2 field [then] schema accepts it, [else stop]."""
    event = ce.ControlEventV2(
        session_id="2026-04-17T21-30-00",
        seq=0,
        sample=4800,
        sample_rate=48000,
        wall_clock="2026-04-17T21:30:00.100+00:00",
        control="deck.1.tempo",
        value=1.04,
        kind="set",
        source="controller",
        deck=1,
    )
    event.validate()
