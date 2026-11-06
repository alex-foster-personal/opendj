"""Rekordbox ANALYSE-ME routes (RBXAN-8322c), mounted from the package router.

``apps.rekordbox_analyse`` is an enforced module (ADR-NEW-module-contracts), so the
web UI reaches it only through ``apps.rekordbox_analyse.http``.

-Claude
"""

from __future__ import annotations

from apps.rekordbox_analyse.http import router

__all__ = ["router"]
