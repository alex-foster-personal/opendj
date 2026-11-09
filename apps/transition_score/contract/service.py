"""The service interface ``apps.transition_score`` offers its callers (ADR-NEW-module-contracts).

Callers receive a :class:`TransitionScoreService` from ``apps.transition_score.http``
and never build the implementation themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import TryResult


@runtime_checkable
class TransitionScoreService(Protocol):
    """The try-er (TRYER-03): propose, render and score candidates for one pair, ranked.

    Build one with ``apps.transition_score.http.build_service``; the track source and the
    AutoDJ service are bound there, because a contract method carries only boundary types.
    """

    def try_pair(
        self,
        out_id: str,
        in_id: str,
        *,
        n: int = 6,
        context_bars: int = 4,
        data_dir: Path,
        binary: Path | None = None,
        keep_renders_in: Path | None = None,
    ) -> TryResult: ...
