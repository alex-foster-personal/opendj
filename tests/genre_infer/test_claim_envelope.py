"""ENRICH-12086: versioned enrichment claim envelope and store round-trip."""

from __future__ import annotations

import sqlite3

import pytest

from apps.analysis import pcm_fingerprint
from apps.genre_infer import findings, providers
from apps.genre_infer.claim_envelope import (
    ClaimEnvelopeError,
    ClaimScope,
    EnrichmentClaim,
    SegmentScope,
    deterministic_fact,
    vendor_observation,
)
from apps.genre_infer.tag_map import OPEN_DJ_UNKNOWN


def _base_claim(**overrides) -> EnrichmentClaim:
    defaults = dict(
        stable_id="t-demo",
        dimension="mood",
        kind="semantic_tag",
        source="cyanite",
        provider_model="auto-tagging-2",
        schema_version=providers.CYANITE_EXPLORER_SCHEMA,
        scope=ClaimScope(kind="track"),
        value="Energetic",
        confidence=0.91,
        generated_at="2026-11-10T12:00:00+00:00",
        input_fingerprint="abc",
        raw_evidence_ptr="blob://cyanite/raw/1",
    )
    defaults.update(overrides)
    return EnrichmentClaim(**defaults)


@pytest.mark.requirement("ENRICH-12086")
def test_stored_claim_round_trips_with_full_envelope_fields():
    """[if] a claim is stored [then] envelope fields survive JSON round-trip, [else stop]."""
    claim = _base_claim(
        scope=ClaimScope(kind="segment", segment=SegmentScope(10_000, 20_000)),
    )
    payload = EnrichmentClaim.from_json(claim.to_json())
    assert payload.source == "cyanite"
    assert payload.provider_model == "auto-tagging-2"
    assert payload.schema_version == providers.CYANITE_EXPLORER_SCHEMA
    assert payload.scope.kind == "segment"
    assert payload.scope.segment is not None and payload.scope.segment.start_ms == 10_000
    assert payload.value == "Energetic"
    assert payload.confidence == 0.91
    assert payload.generated_at == claim.generated_at
    assert payload.input_fingerprint == "abc"
    assert payload.raw_evidence_ptr == "blob://cyanite/raw/1"


@pytest.mark.requirement("ENRICH-12086")
def test_own_bpm_key_meter_are_deterministic_facts_not_semantic_tags():
    """[if] BPM/key/meter from own analysis [then] kind is deterministic_fact only, [else stop]."""
    fp = pcm_fingerprint.claim_input_fingerprint(
        decode_fingerprint="deadbeef",
        stable_id="t-own",
        analysis_lane="own_beatgrid",
    )
    bpm = deterministic_fact(
        stable_id="t-own",
        dimension="bpm",
        value=128.0,
        source="open-dj-tool",
        provider_model="own_beatgrid",
        schema_version="own-beatgrid/v1",
        input_fingerprint=fp,
        raw_evidence_ptr="state://analysis/own_beatgrid/t-own",
        confidence=1.0,
    )
    assert bpm.kind == "deterministic_fact"
    prose = vendor_observation(
        stable_id="t-own",
        dimension="description",
        value="A driving techno track.",
        source="cyanite",
        provider_model="description",
        schema_version=providers.CYANITE_EXPLORER_SCHEMA,
        input_fingerprint=fp,
        raw_evidence_ptr="blob://cyanite/desc/1",
        generated=True,
    )
    assert prose.kind == "generated_description"
    with pytest.raises(ClaimEnvelopeError):
        vendor_observation(
            stable_id="t-own",
            dimension="bpm",
            value=130,
            source="cyanite",
            provider_model="bpm",
            schema_version=providers.CYANITE_BPM_V2_SCHEMA,
            input_fingerprint=fp,
            raw_evidence_ptr="blob://cyanite/bpm/1",
        )


@pytest.mark.requirement("ENRICH-12086")
def test_enrichment_layers_load_without_paid_vendor_keys():
    """[if] no vendor account [then] taxonomy map and layers still load, [else stop]."""
    assert providers.enrichment_layers_available() is True
    assert providers.missing_keys({}) == [providers.OPENROUTER_KEY_ENV, providers.SERPER_KEY_ENV]


@pytest.mark.requirement("ENRICH-12086")
def test_bpm_importer_keeps_raw_value_under_bound_schema_version():
    """[if] explorer BPM 60-280 disagrees with BpmV2 [then] raw value kept per schema, [else stop]."""
    fp = pcm_fingerprint.claim_input_fingerprint(
        decode_fingerprint="cafebabe",
        stable_id="t-bpm",
        analysis_lane="vendor_bpm",
    )
    explorer = deterministic_fact(
        stable_id="t-bpm",
        dimension="bpm",
        value=240,
        source="cyanite",
        provider_model="taxonomy-explorer",
        schema_version=providers.CYANITE_BPM_EXPLORER_SCHEMA,
        input_fingerprint=fp,
        raw_evidence_ptr="blob://cyanite/bpm/explorer",
    )
    api = deterministic_fact(
        stable_id="t-bpm",
        dimension="bpm",
        value=240,
        source="cyanite",
        provider_model="BpmV2",
        schema_version=providers.CYANITE_BPM_V2_SCHEMA,
        input_fingerprint=fp,
        raw_evidence_ptr="blob://cyanite/bpm/v2",
    )
    assert explorer.value == 240 and api.value == 240
    assert explorer.schema_version != api.schema_version


@pytest.mark.requirement("ENRICH-12086")
def test_put_claim_round_trips_through_sqlite():
    """[if] a claim is stored [then] put_claim and claims_for return the same envelope, [else stop]."""
    conn = sqlite3.connect(":memory:")
    findings.ensure_schema(conn)
    claim = _base_claim(dimension="movement", value="Pulsing")
    stored = findings.put_claim(conn, claim)
    assert stored.claim_id
    loaded = findings.claims_for(conn, "t-demo")
    assert len(loaded) == 1
    assert loaded[0].to_json() == stored.to_json()


@pytest.mark.requirement("ENRICH-12086")
def test_unknown_vendor_mood_stays_unknown_not_coerced():
    """[if] vendor tag unknown [then] open_dj_id is unknown with raw spelling kept, [else stop]."""
    from apps.genre_infer import tag_map

    blob = tag_map.load_taxonomy_map()
    resolved = blob.resolve(vendor="cyanite", category="Simple Moods", vendor_value="Agressive")
    assert resolved.vendor_value == "Agressive"
    assert resolved.open_dj_id == "mood.aggressive"
    alien = blob.resolve(vendor="cyanite", category="Simple Moods", vendor_value="Zzyx")
    assert alien.vendor_value == "Zzyx"
    assert alien.open_dj_id == OPEN_DJ_UNKNOWN
