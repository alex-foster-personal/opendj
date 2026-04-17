"""open-dj vendor adapters.

Thin translators between vendor DB readers and open-dj JSON documents.
Each adapter exposes:

- ``export_library(...)`` -- vendor -> open-dj dict (caller canonicalises).
- ``import_library(...)`` -- open-dj dict -> vendor. Scope per spec D3:
  at v0.2 / v0.3 we ship read-first and a minimal write (ratings only on
  djay; ratings + tracks_ordered on Rekordbox). Cue writes are explicitly
  deferred to Phase 4's cautious write path.

See :mod:`apps.open_dj.adapters.rekordbox` and
:mod:`apps.open_dj.adapters.djay`.
"""
from __future__ import annotations

from apps.open_dj.adapters._base import ExportResult, ImportResult, OpenDjAdapter

__all__ = ["OpenDjAdapter", "ExportResult", "ImportResult"]
