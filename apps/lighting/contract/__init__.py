"""Public surface of ``apps.lighting`` (ADR-NEW-module-contracts).

Code outside the package imports only this module (delivery layers may also import
``apps.lighting.http`` for its router). Standard library and pydantic only. Later lighting
issues add the types and the service Protocol outsiders use here; nothing else is public.
"""

from __future__ import annotations

CONTRACT_VERSION = "lighting/1"

#: Feature flag that gates every lighting route and output (default off).
LIGHTING_FLAG_ID = "lighting.output"

__all__ = ["CONTRACT_VERSION", "LIGHTING_FLAG_ID"]
