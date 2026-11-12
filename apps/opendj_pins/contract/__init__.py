"""Public contract of ``apps.opendj_pins`` (ADR-NEW-module-contracts).

Mountable feedback pin router and SQLite store. Code outside the package
imports only this module (delivery layers may also import ``apps.opendj_pins.http``).
"""

from __future__ import annotations

CONTRACT_VERSION = "opendj_pins/1"
