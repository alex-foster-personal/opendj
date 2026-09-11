"""PARITY-01 vocal lane: PVDI denominator, SPIKE-B2 IoU, probe reuse."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from apps.parity.score import render_report, score_payload
from apps.parity.vocal import vocal_iou
from apps.vocals.cli import pvdi_present
from scripts.parity.run_vocal import worker_stdout_to_payload
from tests.parity.payloads import MEASURED_AT, payload, round1_vocal_fixture, track

pytestmark = pytest.mark.requirement("PARITY-01")

_PVDI_FIXED_HEADER = bytes.fromhex("0000040056220001")


def _synthetic_2ex(envelope: bytes) -> bytes:
    section = (
        b"PVDI"
        + struct.pack(">II", 24, 24 + len(envelope))
        + _PVDI_FIXED_HEADER
        + struct.pack(">I", len(envelope))
        + envelope
    )
    head_len = 28
    header = b"PMAI" + struct.pack(">II", head_len, head_len + len(section))
    return header + b"\x00" * (head_len - len(header)) + section


def test_missing_pvdi_is_ungradable_not_agreement_or_miss() -> None:
    data = payload(
        [
            track(
                "has-pvdi",
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 1.0, "end_s": 2.0}],
                own_vocal_regions=[{"start_s": 1.0, "end_s": 2.0}],
                duration_s=10.0,
            ),
            track("no-pvdi", rb_pvdi=False),
        ]
    )
    vocal = score_payload(data).figure("vocal")
    assert "no-pvdi" in vocal.ungradable_ids
    assert "no-pvdi" not in vocal.agree_ids
    assert "no-pvdi" not in vocal.disagree_ids
    assert vocal.ungradable["missing_pvdi"] == 1
    assert vocal.denominator_n == 1


def test_no_own_stays_in_denominator() -> None:
    data = payload(
        [
            track(
                "no-own",
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 0.0, "end_s": 5.0}],
                own_vocal_regions=None,
            )
        ]
    )
    vocal = score_payload(data).figure("vocal")
    assert vocal.denominator_n == 1
    assert vocal.no_own_n == 1
    assert "no-own" in vocal.no_own_ids
    assert vocal.scored_n == 0


def test_empty_vs_empty_iou_is_one() -> None:
    iou, _, _ = vocal_iou([], [], duration_s=10.0)
    assert iou == 1.0


def test_disjoint_regions_iou_is_zero() -> None:
    iou, _, _ = vocal_iou(
        [(0.0, 3.0)],
        [(7.0, 10.0)],
        duration_s=10.0,
    )
    assert iou == 0.0


def test_overlap_iou_is_between_zero_and_one() -> None:
    iou, _, _ = vocal_iou(
        [(0.0, 8.0)],
        [(2.0, 10.0)],
        duration_s=10.0,
    )
    assert 0.0 < iou < 1.0


def test_missing_duration_is_failed_own() -> None:
    data = payload(
        [
            track(
                "failed",
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 1.0, "end_s": 2.0}],
                own_vocal_regions=[{"start_s": 1.0, "end_s": 2.0}],
                duration_s=None,
            )
        ]
    )
    vocal = score_payload(data).figure("vocal")
    assert vocal.failed_own_n == 1
    assert vocal.scored_n == 0


def test_round1_fixture_vocal_cases() -> None:
    report = score_payload(round1_vocal_fixture())
    vocal = report.figure("vocal")
    assert vocal.status == "scored"
    assert vocal.denominator_n == 7
    assert vocal.ungradable["missing_pvdi"] == 3
    assert "no-own-bpm" in vocal.no_own_ids
    assert "exact-key-c" in vocal.agree_ids
    assert "bpm-within-0.1" in vocal.disagree_ids
    assert vocal.iou_mean is not None
    assert vocal.exact_n == 1


def test_report_names_denominator_date_and_not_at_parity() -> None:
    text = render_report(score_payload(round1_vocal_fixture()))
    assert MEASURED_AT in text
    assert "tracks with rekordbox PVDI among present audio" in text
    assert "reporting, not a threshold" in text
    assert "at parity" not in text.lower()
    assert "9986" not in text
    assert "10479" not in text


def test_pvdi_present_seek_walk_on_synthetic_2ex(tmp_path: Path) -> None:
    path = tmp_path / "pvdi.2EX"
    path.write_bytes(_synthetic_2ex(bytes([0, 1, 2, 3])))
    assert pvdi_present(path) is True
    empty = tmp_path / "empty.2EX"
    empty.write_bytes(b"PMAI" + struct.pack(">II", 28, 28) + b"\x00" * 16)
    assert pvdi_present(empty) is False


def test_worker_stdout_remaps_to_payload_fields() -> None:
    mapped = worker_stdout_to_payload(
        {
            "duration_s": 123.4,
            "regions": [
                {"start_s": 1.0, "end_s": 2.0, "intensity": 3, "confidence": 0.9},
                {"start_s": 10.0, "end_s": 20.0},
            ],
        }
    )
    assert mapped["duration_s"] == 123.4
    assert mapped["own_vocal_regions"] == [
        {"start_s": 1.0, "end_s": 2.0},
        {"start_s": 10.0, "end_s": 20.0},
    ]
