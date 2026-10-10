"""Delivery-layer entry to ``apps.library_wheel``: the service builder.

The FastAPI routes stay in ``apps/webui/server/routes/library.py`` for M1.
"""

from __future__ import annotations

from .contract import AXES, LibraryWheelError, LibraryWheelService
from .service import LibraryWheel


def build_service() -> LibraryWheelService:
    return LibraryWheel()


__all__ = ["AXES", "LibraryWheelError", "LibraryWheelService", "build_service"]
