"""Pre-LAZY-STEMS deck-load rows must not silently inflate the serial term.

A separate module from test_amdahl_report.py on purpose: that file sits at
the 600-line file_size ratchet floor (ops/quality/baseline.json), so adding
here avoids tripping file_size.over_limit_python for no functional reason.

EAGER_STEM_LOAD is a SYNTHETIC shape, not a locked capture: no ring on this
machine has ever held a pre-LAZY-STEMS `deck-load` row (one recording
`fetchStems`/`decodeStems`/`stemProcessorCreate` directly, before stem work
moved onto its own `deck-stems` row), and none can be captured now that
lazy-stems has shipped. Both tests below are skipped as UNAVAILABLE rather
than asserting classification of an invented shape as validated coverage
(Codex P1/BLOCKING, #705, discussion_r3915607020; AGENTS.md L55-59).
Tracked in MERGE-QUEUE.md.
"""

from __future__ import annotations

import pytest

from scripts.perf.amdahl_report import aggregate, build_records

EAGER_STEM_LOAD = {
    "t": "2026-09-02T00:00:00.000Z",
    "kind": "deck-load sid=aaaaaaaaaaaa",
    "deck": 1,
    "stages": {
        "fetchWall": 200,
        "totalBeforeSwap": 9600,
        "total": 9800,
        "stemmed": 1,
        "fetchStems": 4000,
        "decodeStems": 5000,
        "stemProcessorCreate": 1350,
    },
}

UNAVAILABLE_REASON = (
    "UNAVAILABLE: no ring here has ever held a pre-LAZY-STEMS deck-load row, "
    "and none can be captured now that lazy-stems has shipped. EAGER_STEM_LOAD "
    "is an invented shape backed by no real payload; asserting the tool "
    "classifies it correctly manufactures coverage of a format nothing "
    "confirms is accurate (Codex P1/BLOCKING, #705, discussion_r3915607020)."
)


@pytest.mark.skip(reason=UNAVAILABLE_REASON)
def test_eager_stem_load_is_flagged_anomalous_not_silently_pooled() -> None:
    """If broken: the load's 9000ms of real parallel stem work folds into
    `unattributed-pre-swap` and counts SERIAL with no warning, so the
    aggregate ceiling is materially understated for exactly the rings that
    still hold a pre-LAZY-STEMS row. Skipped -- see UNAVAILABLE_REASON."""
    records, notes = build_records([EAGER_STEM_LOAD])
    assert len(records) == 1
    assert records[0].anomalous is True
    assert notes["unclassified_stages"] == [
        "decodeStems",
        "fetchStems",
        "stemProcessorCreate",
    ]


@pytest.mark.skip(reason=UNAVAILABLE_REASON)
def test_eager_stem_load_is_excluded_from_the_aggregate_ceiling() -> None:
    """If broken: the anomalous-but-still-flagged load leaks back into the
    pooled parallel fraction anyway. Skipped -- see UNAVAILABLE_REASON."""
    records, _notes = build_records([EAGER_STEM_LOAD])
    summary = aggregate(records)
    assert summary["loads"] == 0
    assert summary["anomalous_loads_excluded"] == 1
