"""A contract that hands out another contract's types, loose and clean."""

from __future__ import annotations

from tests.quality.contract_fixtures.loose.contract import LooseService
from tests.quality.contract_fixtures.shared.contract import Shared

CONTRACT_VERSION = "relay/1"

__all__ = ["CONTRACT_VERSION", "LooseService", "Shared"]
