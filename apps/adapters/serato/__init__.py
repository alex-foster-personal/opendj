"""Serato DJ Pro adapter (Phase 16, OPEN-02c).

Clean-room Python implementation based on public documentation only
(triseratops MPL-2 README + serato-tags CC-BY-SA-4.0 docs). No source code
copied in. Apache-2.0 licensed file-by-file; any future embed of
triseratops-derived code lives under a separate MPL-2 sub-package.

Public API:

  * ``SeratoAdapter``           -- top-level ``Adapter`` Protocol impl.
  * ``SeratoAdapterOptions``    -- runtime options (rating column, memory->hot).
  * ``TagStream``               -- Serato tag-stream codec (re-used by GEOB).
  * ``capabilities``            -- static Capabilities descriptor.
  * ``is_serato_running``       -- pgrep rail for write-safety.
"""

from __future__ import annotations

from apps.adapters.serato.adapter import SeratoAdapter, SeratoAdapterOptions
from apps.adapters.serato.capabilities import capabilities
from apps.adapters.serato.safety import is_serato_running
from apps.adapters.serato.tagstream import RawTag, TagStream

__all__ = [
    "RawTag",
    "SeratoAdapter",
    "SeratoAdapterOptions",
    "TagStream",
    "capabilities",
    "is_serato_running",
]
