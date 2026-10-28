"""``__all__`` read as names only: its values are reached as ``contract.__all__["Point"]``."""

from __future__ import annotations

import sqlite3 as _sqlite3

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "listing/1"


class Point(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: int


__all__ = {"CONTRACT_VERSION": None, "Point": _sqlite3.connect(":memory:")}  # type: ignore[misc]  # noqa: PLE0605
