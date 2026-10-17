"""``ReconcileService`` implementation: delegates to the existing reconcile code.

No logic lives here. Module attributes (``locate.find_candidates``,
``locate.FsIndex``, ``FingerprintEvidence``) are looked up at call time so
existing monkeypatch points keep working.
"""

from __future__ import annotations

from pathlib import Path

from apps.reconcile import locate
from apps.reconcile.contract import Candidate, RelocateQuery
from apps.reconcile.fingerprint_evidence import FingerprintEvidence
from apps.shared import audio_files


class LocalReconcileService:
    """In-process ``apps.reconcile.contract.ReconcileService``.

    ``music_roots`` and ``dedup_db`` default to the app's configured locations
    (``paths.MUSIC_ROOTS``, ``paths.DEDUP_FALLBACK_DB``); a composition root may
    pass its own.
    """

    def __init__(self, *, music_roots: list[Path] | None = None, dedup_db: Path | None = None) -> None:
        self._music_roots = music_roots
        self._dedup_db = dedup_db

    def find_relocate_candidates(self, query: RelocateQuery, *, limit: int) -> list[Candidate]:
        index = locate.FsIndex.build(audio_files.scan_music_files(self._music_roots))
        id3_cache: dict[Path, audio_files.AudioMetadata | None] = {}
        return locate.find_candidates(
            query.as_row(),
            index,
            id3_cache,
            limit=limit,
            fingerprints=FingerprintEvidence.open(self._dedup_db),
        )


__all__ = ["LocalReconcileService"]
