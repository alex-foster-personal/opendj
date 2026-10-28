"""A contract submodule that answers any name through a PEP 562 module __getattr__."""

from __future__ import annotations

import sqlite3 as _sqlite3


def __getattr__(name: str) -> _sqlite3.Connection:
    return _sqlite3.connect(":memory:")
