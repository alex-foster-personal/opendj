"""apps.sync -- cross-app sync adapters (playlist / matcher / djay).

Submodules are imported lazily so a partially-shipped sibling (e.g. Phase 2's
matcher that is still being written) cannot break everybody else's imports.
"""
from __future__ import annotations

__all__: list[str] = []

# Phase 2 matcher re-exports, best-effort. When Phase 2 commits its matcher
# module these names become available for `from apps.sync import ...`; until
# then we keep the import tolerant so Phase 3+ modules can still import.
try:  # pragma: no cover - import-time guard
    from apps.sync.matcher import (  # type: ignore[attr-defined]
        WEIGHTS,
        MatchedPair,
        MatchResult,
        Signal,
        UnifiedTrack,
        _normalise_for_match,
        match_tracks,
        score_pair,
    )
except Exception:  # noqa: BLE001
    pass
else:
    __all__.extend(
        [
            "MatchResult",
            "MatchedPair",
            "Signal",
            "UnifiedTrack",
            "WEIGHTS",
            "_normalise_for_match",
            "match_tracks",
            "score_pair",
        ]
    )
