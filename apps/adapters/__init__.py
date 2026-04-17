"""open-dj vendor adapters.

Each sub-package implements the ``apps.open_dj.Adapter`` Protocol:

  * ``apps.adapters.serato``  -- Serato DJ Pro (Phase 16, OPEN-02c)
  * ``apps.adapters.traktor`` -- Traktor (Phase 16, OPEN-02d)
  * Future: ``apps.adapters.rekordbox`` -- Phase 15, OPEN-02a
  * Future: ``apps.adapters.djay``      -- Phase 15, OPEN-02b

Capability descriptors live in ``<adapter>/capabilities.py``; write-safety
rails live in ``<adapter>/safety.py``.
"""

from __future__ import annotations
