"""AnalysisRecord v2: producer identity, versioning, lane results, back-compat.

Spec: `specs/native-analysis-v1.md` section 3 ("Record") and section 8's
regression lines. Requirement: NATIVE-09.

Acceptance lines exercised here:
- [if] a record lands without a producer version [then] the write fails.
- [if] a model-using lane writes a record without that model's sha256, or a
  model-free lane invents one, [then] the write fails.
- [if] a pre-v2 row is loaded [then] it comes back with lanes={} and
  producer="backfill" rather than failing.
- [if] an own beatgrid claims status ok with no beats [then] that is a
  contract violation, not a state.

-Claude
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from apps.analysis.lanes import (
    LaneContractError,
    LaneResult,
    SemverError,
    own_backend,
    parse_own_backend,
    semver_key,
    validate_lane_payload,
    validate_lane_result,
)
from apps.analysis.record import (
    AnalysisRecord,
    RecordContractError,
    validate_record_contract,
)

from tests.analysis_contract.conftest import (
    beatgrid_payload,
    key_payload,
    loudness_payload,
    own_record,
)

pytestmark = pytest.mark.requirement("NATIVE-09")


#-----------------------------------------------------------------------------
# round trip and back-compat
#-----------------------------------------------------------------------------

def test_v2_record_round_trips_through_json() -> None:
    record = own_record()
    restored = AnalysisRecord.from_json(record.to_json())
    assert restored == record
    assert restored.lanes["beatgrid"].status == "ok"
    assert restored.lanes["beatgrid"].payload["bpm"] == 128.0


def test_pre_v2_row_loads_with_empty_lanes_and_backfill_producer() -> None:
    """The 347 rows already in state.db predate every v2 field."""
    legacy = json.dumps({
        "stable_id": "old-1",
        "backend": "librosa-only",
        "backend_version": "librosa==0.10.2.post1+downbeats=inferred",
        "analyzed_at": "2026-01-01T00:00:00Z",
        "duration_s": 210.0, "sample_rate": 44100,
        "bpm": 124.0, "bpm_confidence": 0.5,
        "key_camelot": "5A", "key_openkey": "12m", "key_confidence": 0.4,
        "energy": 5, "energy_source": "inferred",
    })
    record = AnalysisRecord.from_json(legacy)
    assert record.lanes == {}
    assert record.producer == "backfill"
    assert record.producer_version == ""
    assert record.model_sha256 is None


def test_pre_v1_backend_is_exempt_from_the_v2_contract() -> None:
    """The exemption is deliberate, so it is asserted rather than assumed."""
    record = AnalysisRecord.from_json(own_record().to_json())
    legacy = dataclasses.replace(
        record, backend="librosa-only", producer_version="", decode_fingerprint="",
        lanes={}, producer="backfill",
    )
    validate_record_contract(legacy)  # must not raise


#-----------------------------------------------------------------------------
# the required fields (NATIVE-09 mutation tests)
#-----------------------------------------------------------------------------

def test_own_record_without_producer_version_is_refused() -> None:
    bad = dataclasses.replace(own_record(), producer_version="", backend_version="")
    with pytest.raises(RecordContractError, match="producer_version"):
        validate_record_contract(bad)


def test_own_record_without_decode_fingerprint_is_refused() -> None:
    bad = dataclasses.replace(own_record(), decode_fingerprint="")
    with pytest.raises(RecordContractError, match="decode_fingerprint"):
        validate_record_contract(bad)


def test_model_lane_without_model_sha256_is_refused() -> None:
    bad = dataclasses.replace(own_record(), uses_model=True, model_sha256=None)
    with pytest.raises(RecordContractError, match="model_sha256"):
        validate_record_contract(bad)


def test_model_free_lane_that_invents_a_model_sha256_is_refused() -> None:
    """The opposite direction: a fabricated hash is worse than an absent one."""
    bad = dataclasses.replace(own_record(), uses_model=False, model_sha256="deadbeef")
    with pytest.raises(RecordContractError, match="model-free"):
        validate_record_contract(bad)


def test_model_lane_with_its_sha256_is_accepted() -> None:
    """Positive control: the check above must be about the pairing, not the flag."""
    good = own_record(uses_model=True, model_sha256="sha256:" + "a" * 64)
    validate_record_contract(good)


def test_model_free_lane_recording_null_is_accepted() -> None:
    validate_record_contract(own_record(uses_model=False, model_sha256=None))


def test_producer_in_the_body_must_match_the_backend_name() -> None:
    bad = dataclasses.replace(own_record(producer="inapp"), producer="backfill")
    with pytest.raises(RecordContractError, match="producer identity"):
        validate_record_contract(bad)


def test_own_record_must_carry_its_own_lane() -> None:
    bad = dataclasses.replace(
        own_record(lane="beatgrid"),
        lanes={"loudness": LaneResult(status="ok", payload=loudness_payload())},
    )
    with pytest.raises(RecordContractError, match="not its own lane"):
        validate_record_contract(bad)


def test_non_semver_producer_version_is_refused() -> None:
    bad = own_record(version="librosa==0.10.2.post1")
    with pytest.raises(RecordContractError, match="semver"):
        validate_record_contract(bad)


#-----------------------------------------------------------------------------
# lane payload shapes
#-----------------------------------------------------------------------------

def test_beatgrid_ok_with_no_beats_is_a_contract_violation() -> None:
    payload = beatgrid_payload()
    payload["beats"] = []
    with pytest.raises(LaneContractError, match="no pulse"):
        validate_lane_payload("beatgrid", payload)


def test_a_lane_that_found_no_pulse_says_so_with_failed_and_a_reason() -> None:
    validate_lane_result(
        "beatgrid", LaneResult(status="failed", reason="no_trackable_pulse")
    )


def test_failed_lane_without_a_reason_is_refused() -> None:
    with pytest.raises(LaneContractError, match="without a reason"):
        validate_lane_result("beatgrid", LaneResult(status="failed", reason=None))


def test_failed_lane_carrying_a_payload_is_refused() -> None:
    with pytest.raises(LaneContractError, match="carries a payload"):
        validate_lane_result(
            "beatgrid",
            LaneResult(status="failed", reason="no_trackable_pulse",
                       payload=beatgrid_payload()),
        )


def test_key_segments_block_carries_its_own_status() -> None:
    payload = key_payload()
    del payload["segments"]["status"]
    with pytest.raises(LaneContractError, match="key.segments"):
        validate_lane_payload("key", payload)


def test_key_segments_ok_with_no_segments_is_refused() -> None:
    payload = key_payload(segments=0)
    with pytest.raises(LaneContractError, match="no segments"):
        validate_lane_payload("key", payload)


def test_waveform_kind_must_be_tri_on_an_own_record() -> None:
    with pytest.raises(LaneContractError, match="tri"):
        validate_lane_payload("waveform", {
            "kind": "mono",
            "preview": {"length": 0, "low": [], "mid": [], "high": []},
            "detail": {"length": 0, "low": [], "mid": [], "high": []},
        })


def test_unknown_lane_is_refused() -> None:
    """Negative control: the validator must not accept a lane it cannot check."""
    with pytest.raises(LaneContractError, match="unknown lane"):
        validate_lane_payload("phrases", {})


def test_every_lane_payload_helper_validates() -> None:
    """Positive control: the fixtures the other tests mutate are themselves valid."""
    validate_lane_payload("beatgrid", beatgrid_payload())
    validate_lane_payload("key", key_payload())
    validate_lane_payload("loudness", loudness_payload())


#-----------------------------------------------------------------------------
# backend naming and semver
#-----------------------------------------------------------------------------

def test_own_backend_names_round_trip() -> None:
    assert own_backend("beatgrid", "inapp") == "own_beatgrid.inapp"
    assert own_backend("key", "cand", "skey") == "own_key.cand.skey"
    assert parse_own_backend("own_beatgrid.inapp").producer == "inapp"
    assert parse_own_backend("own_key.cand.skey").candidate == "skey"


def test_pre_v1_backend_names_parse_as_not_own() -> None:
    assert parse_own_backend("librosa-only") is None
    assert parse_own_backend("librosa+madmom") is None
    assert parse_own_backend("mik") is None


def test_a_malformed_own_backend_name_raises_rather_than_parsing_as_not_own() -> None:
    with pytest.raises(LaneContractError):
        parse_own_backend("own_beatgrid.wrongproducer")
    with pytest.raises(LaneContractError):
        parse_own_backend("own_phrases.inapp")


def test_semver_orders_numerically_not_lexically() -> None:
    assert semver_key("0.10.0") > semver_key("0.9.0")
    assert semver_key("1.0.0") > semver_key("1.0.0-rc1")


def test_semver_refuses_a_build_string_it_cannot_order() -> None:
    with pytest.raises(SemverError):
        semver_key("librosa==0.10.2.post1+downbeats=inferred")
