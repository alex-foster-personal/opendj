"""Classes a contract may not declare: open, mutable or plain."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

CONTRACT_VERSION = "open_types/1"


class Loose(BaseModel):
    x: int


@dataclass
class Mutable:
    x: int


class Plain:
    pass
