"""ENRICH-13622: segment-level enrichment claims on own structure windows."""

from __future__ import annotations

import json

import pytest

from apps.genre_infer.segment_claims import (
    OwnStructureWindow,
    VendorSegmentObservation,
    aggregate_track_summary,
    attach_vendor_observations,
    claims_covering_dimensions,
    global_metadata_safety,
    m_with_semantic_overlay,
    overlay_inputs_for_phrase,
    phrase_windows_from_sections,
)
from dataclasses import dataclass


@dataclass(frozen=True)
class _VetoStub:
    name: str
    threshold: float = 20.0


@pytest.mark.requirement("ENRICH-13622")
def test_phrase_evidence_yields_segment_scoped_five_dimensions():
    """[if] phrase evidence exists [then] five segment dimensions attach with scope, [else stop]."""
    windows = phrase_windows_from_sections(
        [{"start_s": 0.0, "end_s": 16.0, "start_bar": 0}, {"start_s": 16.0, "end_s": 32.0, "start_bar": 16}]
    )
    observations = [
        VendorSegmentObservation("mood", "Energetic", 100, 9000, confidence=0.9),
        VendorSegmentObservation("energy", 0.8, 500, 12000, confidence=0.8),
        VendorSegmentObservation("movement", "Driving", 200, 11000, confidence=0.7),
        VendorSegmentObservation("vocals", "Female", 1000, 15000, confidence=0.6),
        VendorSegmentObservation("instrumentation", "Synth", 0, 16000, confidence=0.5),
    ]
    claims = attach_vendor_observations(
        stable_id="t-seg",
        windows=windows,
        observations=observations,
        input_fingerprint="fp",
    )
    assert len(claims) == 5
    assert claims_covering_dimensions(claims) == {
        "mood",
        "energy",
        "movement",
        "vocals",
        "instrumentation",
    }
    for claim in claims:
        assert claim.scope.kind == "segment"
        assert claim.scope.segment is not None
        assert claim.scope.segment.start_ms == 0
        assert claim.scope.segment.end_ms == 16000


@pytest.mark.requirement("ENRICH-13622")
def test_vendor_times_stay_evidence_own_bounds_publish_scope():
    """[if] vendor times disagree [then] own bounds publish and vendor stays in ptr, [else stop]."""
    own = OwnStructureWindow(start_ms=0, end_ms=32000)
    obs = VendorSegmentObservation("mood", "Dark", vendor_start_ms=500, vendor_end_ms=45000, confidence=0.5)
    claims = attach_vendor_observations(
        stable_id="t-edge",
        windows=[own],
        observations=[obs],
        input_fingerprint="fp2",
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim.scope.segment is not None
    assert (claim.scope.segment.start_ms, claim.scope.segment.end_ms) == (0, 32000)
    ptr = json.loads(claim.raw_evidence_ptr)
    assert ptr["vendor_start_ms"] == 500
    assert ptr["vendor_end_ms"] == 45000
    assert ptr["own_start_ms"] == 0
    assert ptr["own_end_ms"] == 32000


@pytest.mark.requirement("ENRICH-13622")
def test_layer0_veto_keeps_m_zero_despite_semantic_alignment():
    """[if] beat offset veto fires [then] semantic overlay cannot raise M, [else stop]."""
    vetoes = (_VetoStub("beat_offset_p95", 20.0),)
    lifted = m_with_semantic_overlay(score=0.85, vetoes=vetoes, semantic_factor=1.0)
    assert lifted == 0.0


@pytest.mark.requirement("ENRICH-13622")
def test_vocal_phrase_overlay_is_not_whole_track_compatibility():
    """[if] voice vacates a phrase [then] overlay inputs exist without compat score, [else stop]."""
    from apps.genre_infer.claim_envelope import ClaimScope, SegmentScope, vendor_observation

    window = OwnStructureWindow(start_ms=16000, end_ms=32000)
    claims = [
        vendor_observation(
            stable_id="t-vox",
            dimension="vocals",
            value="absent",
            source="cyanite",
            provider_model="auto-tagging-2",
            schema_version="cyanite-taxonomy-explorer/2026-09-13",
            input_fingerprint="fp",
            raw_evidence_ptr="blob://vendor/vocals/1",
            scope=ClaimScope(kind="segment", segment=SegmentScope(15000, 50000)),
        )
    ]
    overlay = overlay_inputs_for_phrase(claims, window)
    assert overlay.voice_value == "absent"
    summary = aggregate_track_summary(claims)
    assert summary["kind"] == "segment_aggregate"
    assert "compatibility" not in json.dumps(summary)


@pytest.mark.requirement("ENRICH-13622")
@pytest.mark.requirement("ENRICH-13622")
def test_score_render_semantic_hint_respects_layer0_veto():
    """[if] render has veto and semantic hint [then] M stays 0, [else stop]."""
    pytest.importorskip("scipy")
    from tests.transition_score.test_mutations import MUTATIONS, _score

    vetoed = _score(MUTATIONS["beat +60 ms"])
    assert vetoed.score == 0.0
    assert any(v.name == "beat_offset_p95" for v in vetoed.vetoes)


def test_disagreeing_segment_bpm_marks_global_metadata_unsafe():
    """[if] segment BPM disagrees [then] global metadata is unsafe not overwritten, [else stop]."""
    safety = global_metadata_safety(
        track_bpm=128.0,
        track_key="8A",
        segment_bpms=(128.0, 140.0),
        segment_keys=("8A", "8A"),
    )
    assert safety.safe is False
    assert safety.reason == "segment_bpm_disagreement"
