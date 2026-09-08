"""Contract checks for the portable local-stems research harness."""

from __future__ import annotations

import pytest

from scripts.bench.local.run_local_stems_benchmark import (
    AUDIBILITY_DB,
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
