"""Contract checks for the portable local-stems research harness."""

from __future__ import annotations

import pytest

from scripts.bench.local.fixtures import EXPECTED_TRACKS, SOURCE_VERSION
from scripts.bench.local.run_local_stems_benchmark import (
    AUDIBILITY_DB,
    _fixture_provenance,
    _parse_candidate_json,
    output_parity,
)


def test_parse_candidate_json_reads_the_final_machine_record():
    stdout = 'diagnostic line\n{"candidate": "htdemucs_torch", "device": "cpu"}\n'

    assert _parse_candidate_json(stdout) == {"candidate": "htdemucs_torch", "device": "cpu"}


def test_parse_candidate_json_refuses_a_candidate_without_machine_output():
    with pytest.raises(RuntimeError, match="JSON result"):
        _parse_candidate_json("download started\n")


def test_output_parity_uses_the_existing_calibrated_stem_tier_bar():
    assert output_parity(10.0, 10.0 + AUDIBILITY_DB - 0.001) == "within-audibility-bar"
    assert output_parity(10.0, 10.0 + AUDIBILITY_DB + 0.001) == "material-difference"


def test_fixture_provenance_preserves_source_version_and_selected_checksums():
    # The manifest content is the CANONICAL pinned data (fixtures.py's own
    # SOURCE_VERSION and an EXPECTED_TRACKS entry), not invented placeholder
    # strings: an invented manifest would still pass even if the real pinned
    # data regressed, since nothing here would be touching it.
    canonical_track = EXPECTED_TRACKS[0]
    manifest = {
        "source": "musdb18-7s-sample",
        "source_version": SOURCE_VERSION,
        "tracks": [{"name": canonical_track["name"], "sha256": canonical_track["sha256"]}],
    }

    assert _fixture_provenance(manifest) == manifest


def test_local_stems_fixture_is_pinned_to_package_dataset_and_checksums():
    assert SOURCE_VERSION.startswith("musdb==0.4.0;")
    assert len(EXPECTED_TRACKS) == 2
    for track in EXPECTED_TRACKS:
        assert len(track["source_sha256"]) == 64
        assert set(track["sha256"]) == {"mixture.wav", "vocals.wav", "drums.wav", "bass.wav", "other.wav"}
