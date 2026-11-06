"""Public contract of ``apps.rekordbox_analyse`` (ADR-NEW-module-contracts).

The package is reached from outside only through ``apps.rekordbox_analyse.http``
(the plan, apply and adopt router) and its CLI; it exports no shared types yet.

-Claude
"""

from __future__ import annotations

CONTRACT_VERSION = "rekordbox_analyse/1"
