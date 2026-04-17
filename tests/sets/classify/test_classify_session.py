"""Tests for :func:`apps.sets.classify.classify_session`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sets.classify import classify_session, read_transitions
from apps.sets.transitions import Transition


def _mk_transition(idx: int, overlap_s: float, is_reload: float = 0.0) -> Transition:
    return Transition(
        idx=idx,
        t_start_s=0.0,
        t_change_s=10.0 * (idx + 1),
        t_end_s=10.0 * (idx + 1) + overlap_s,
        from_deck="A",
        to_deck="B" if is_reload == 0.0 else "A",
        from_track=f"t{idx}a",
        to_track=f"t{idx}b",
        features={
            "overlap_s": overlap_s,
            "fade_s": min(overlap_s, 1.0),
            "incoming_preload_s": 0.0,
            "outgoing_trail_s": min(overlap_s, 1.0),
            "time_since_prev_transition_s": 0.0,
            "is_same_deck_reload": is_reload,
        },
    )


@pytest.mark.requirement("SET-02")
def test_classify_session_writes_jsonl_with_rules_fallback(tmp_path: Path):
    transitions = [
        _mk_transition(0, overlap_s=0.1),   # cut
        _mk_transition(1, overlap_s=12.0),  # blend
        _mk_transition(2, overlap_s=0.5, is_reload=1.0),  # quick_double (fade<1)
    ]
    out = classify_session(
        "s-test",
        transitions=transitions,
        models_dir=tmp_path / "models",  # empty -> rules
        sets_root=tmp_path,
    )
    assert out.exists()
    rows = out.read_text().splitlines()
    assert len(rows) == 3
    parsed = [json.loads(r) for r in rows]
    assert [r["predicted_class"] for r in parsed] == ["cut", "blend", "quick_double"]
    assert all(r["model_version"] == "rules-v0" for r in parsed)


@pytest.mark.requirement("SET-02")
def test_classify_session_falls_back_to_rules_when_macro_f1_below_gate(tmp_path: Path):
    """A meta.json with macro_f1 < 0.6 must NOT use the trained model."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "transition_classifier.joblib").write_bytes(b"fake")
    (models / "transition_classifier.meta.json").write_text(
        json.dumps({"macro_f1": 0.4, "trained_at": "2026-04-17T00:00:00+00:00"})
    )
    transitions = [_mk_transition(0, overlap_s=0.1)]
    out = classify_session(
        "s-low-f1", transitions=transitions, models_dir=models, sets_root=tmp_path
    )
    rows = [json.loads(r) for r in out.read_text().splitlines()]
    assert rows[0]["model_version"] == "rules-v0"


@pytest.mark.requirement("SET-02")
def test_classify_session_force_rules_skips_model_load(tmp_path: Path):
    models = tmp_path / "models"
    models.mkdir()
    # A bogus "good" meta so the trained path would activate if not forced.
    (models / "transition_classifier.joblib").write_bytes(b"fake")
    (models / "transition_classifier.meta.json").write_text(
        json.dumps({"macro_f1": 0.9, "trained_at": "2026-04-17"})
    )
    transitions = [_mk_transition(0, overlap_s=0.1)]
    out = classify_session(
        "s-force",
        transitions=transitions,
        models_dir=models,
        force_rules=True,
        sets_root=tmp_path,
    )
    rows = [json.loads(r) for r in out.read_text().splitlines()]
    assert rows[0]["model_version"] == "rules-v0"


@pytest.mark.requirement("SET-02")
def test_read_transitions_roundtrips(tmp_path: Path):
    transitions = [_mk_transition(i, overlap_s=0.1) for i in range(3)]
    classify_session(
        "s-round",
        transitions=transitions,
        models_dir=tmp_path / "no-models",
        sets_root=tmp_path,
    )
    loaded = read_transitions("s-round", sets_root=tmp_path)
    assert len(loaded) == 3
    assert loaded[0]["idx"] == 0


@pytest.mark.requirement("SET-02")
def test_read_transitions_returns_empty_for_missing_file(tmp_path: Path):
    assert read_transitions("s-none", sets_root=tmp_path) == []
