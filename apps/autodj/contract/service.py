"""The service interface ``apps.autodj`` offers its callers (ADR-NEW-module-contracts).

Domain consumers receive an :class:`AutoDjService` from ``apps.autodj.http``
and never build the implementation themselves.
"""

from __future__ import annotations

from typing import Protocol

from .models import StyleSummary


class AutoDjService(Protocol):
    def list_styles(self) -> list[StyleSummary]: ...
