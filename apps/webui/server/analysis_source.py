"""Per-feature rbx-vs-own analysis source selection (PARITY-02).

In-memory only, one instance per running app (``app.state.analysis_source``):
nothing here is ever written to disk. This is a deliberate TESTING/DEV
affordance, not a persisted user preference -- a normal relaunch starts a
fresh :class:`AnalysisSourceStore`, so every feature is back on
``"rekordbox"`` with no migration or cleanup step required.

A feature is only listed in :data:`FEATURES` once a same-shape own-rolled
source genuinely exists to compare against rekordbox's. Today that is just
``"beatgrid"`` (:mod:`apps.webui.server.routes.analysis`'s
``/beatgrid-fallback``, which already returns the apps.analysis grid in the
exact ``/anlz`` ``beatgrid`` shape). See PARITY-TODO.md "Honest starting
state" -- widen this tuple only once another lane grows a real own-rolled
counterpart, never to advertise a source that would silently fall back.
"""
from __future__ import annotations

from typing import Literal

AnalysisSource = Literal["rekordbox", "own"]

FEATURES: tuple[str, ...] = ("beatgrid",)

DEFAULT_SOURCE: AnalysisSource = "rekordbox"


class AnalysisSourceStore:
    """One process's worth of per-feature rbx-vs-own selection.

    Every feature starts (and, since nothing here persists, always
    restarts) on :data:`DEFAULT_SOURCE`.
    """

    def __init__(self) -> None:
        self._sources: dict[str, AnalysisSource] = {
            feature: DEFAULT_SOURCE for feature in FEATURES
        }

    def snapshot(self) -> dict[str, AnalysisSource]:
        return dict(self._sources)

    def get(self, feature: str) -> AnalysisSource:
        return self._sources[feature]

    def set(self, feature: str, source: AnalysisSource) -> dict[str, AnalysisSource]:
        if feature not in self._sources:
            raise KeyError(feature)
        self._sources[feature] = source
        return self.snapshot()


__all__ = ["DEFAULT_SOURCE", "FEATURES", "AnalysisSource", "AnalysisSourceStore"]
