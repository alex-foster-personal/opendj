"""Another contract, whose model ``clean`` re-exports."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "shared/1"


class Shared(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: int
