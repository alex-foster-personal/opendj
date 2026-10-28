"""A contract that forgot its CONTRACT_VERSION."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Good(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: int
