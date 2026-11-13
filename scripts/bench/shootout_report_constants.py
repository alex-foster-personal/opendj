"""Shared paths and thresholds for the shootout report pipeline."""

from __future__ import annotations

from pathlib import Path

import shootout_spec

_BENCH_DIR: Path = Path(__file__).resolve().parent
_IN_JSON: Path = _BENCH_DIR / "model_shootout.json"
_OUT_MD: Path = _BENCH_DIR / "MODEL-SHOOTOUT.md"
# Claims about listening clips are checked against this directory rather than
# assumed: the report must never promise audio that was not written.
_CLIPS_DIR: Path = _BENCH_DIR / "clips-shootout"

# A model is suspected of buying SI-SDR with under-separation when it beats its
# own SIR ranking by at least this many places. Two places on a seven-model board
# is a real inversion, not measurement noise.
_RANK_GAP_FLAG: int = 2

# Below this many tracks the ranking, spread and production-decision sections are
# refused outright rather than rendered with degenerate values. This project has
# already published an N=1 measurement as a headline finding and had to retract
# it; a spread column reading 0.00 because there is nothing to vary is not a
# tighter result, it is an absent one, and a table looks equally authoritative
# either way. The gate exists so a partial run cannot be mistaken for a study.
_MIN_TRACKS_FOR_RANKING: int = 5

_provenance = shootout_spec.provenance
_is_clean = shootout_spec.is_test_set_clean
