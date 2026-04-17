"""Traktor adapter (Phase 16, OPEN-02d).

Pure stdlib XML implementation reading + writing Native Instruments Traktor
``collection.nml``. No third-party Traktor parser. Preserves unknown
attributes + children on round-trip per open-dj section 9.

Public API:

  * ``TraktorAdapter``         -- top-level Adapter Protocol impl.
  * ``TraktorAdapterOptions``  -- runtime options.
  * ``capabilities``           -- static Capabilities descriptor.
"""

from __future__ import annotations

from apps.adapters.traktor.adapter import TraktorAdapter, TraktorAdapterOptions
from apps.adapters.traktor.capabilities import capabilities

__all__ = ["TraktorAdapter", "TraktorAdapterOptions", "capabilities"]
