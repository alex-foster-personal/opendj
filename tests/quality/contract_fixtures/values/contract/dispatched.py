"""A contract submodule whose module class answers attribute lookups itself (Codex, PR #6182)."""

from __future__ import annotations

import sqlite3 as _sqlite3
import sys as _sys
import types as _types


class _Dispatching(_types.ModuleType):
    def __getattribute__(self, name: str) -> object:
        if name == "connection":
            return _sqlite3.connect(":memory:")
        return super().__getattribute__(name)


_sys.modules[__name__].__class__ = _Dispatching
