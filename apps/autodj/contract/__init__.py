"""The public surface of ``apps.autodj`` (ADR-NEW-module-contracts).

Code outside ``apps.autodj`` imports only this package (and its submodules).
The contract imports only the standard library, pydantic and other contracts.
"""

from __future__ import annotations

CONTRACT_VERSION = "autodj/1"

__all__ = ["CONTRACT_VERSION"]
