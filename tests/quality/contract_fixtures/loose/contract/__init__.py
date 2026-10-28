"""A contract with a loose boundary, as an unenforced package may still have."""

from __future__ import annotations

from typing import Any as _Any
from typing import Protocol

CONTRACT_VERSION = "loose/1"


class LooseService(Protocol):
    def load(self, payload: _Any) -> _Any: ...
