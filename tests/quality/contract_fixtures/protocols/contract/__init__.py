"""Service Protocols whose members leak open types; each also has a clean member as control."""

from __future__ import annotations

import sqlite3 as _sqlite3
from pathlib import Path
from typing import Any as _Any
from typing import Protocol

CONTRACT_VERSION = "protocols/1"


class Leaky(Protocol):
    def open(self, path: Path) -> _sqlite3.Connection: ...

    def name(self) -> str: ...


class Untyped(Protocol):
    def load(self, payload: _Any) -> dict[str, _Any]: ...


class LeakyAttribute(Protocol):
    connection: _sqlite3.Connection
    path: Path


class BoundAttribute(Protocol):
    label: str = _sqlite3.connect(":memory:")  # type: ignore[assignment]  # a safe annotation over a live value


class LeakyProperty(Protocol):
    @property
    def conn(self) -> _sqlite3.Connection: ...


class LeakyFactories(Protocol):
    @classmethod
    def open(cls, path: Path) -> _sqlite3.Connection: ...

    @staticmethod
    def connect(path: Path) -> _sqlite3.Connection: ...


class Unannotated(Protocol):
    def run(self, x): ...  # type: ignore[no-untyped-def]


class LeakySetter(Protocol):
    @property
    def db(self) -> Path: ...

    @db.setter
    def db(self, value: _sqlite3.Connection) -> None: ...


class _OpensBase(Protocol):
    def open(self, path: Path) -> _sqlite3.Connection: ...


class InheritsOpen(_OpensBase, Protocol):
    def name(self) -> str: ...


class CallsOpen(Protocol):
    def __call__(self, path: Path) -> _sqlite3.Connection: ...

    def __repr__(self) -> str: ...


class Store(Protocol):
    def __setitem__(self, key: str, value: _sqlite3.Connection) -> None: ...

    def __hash__(self) -> int: ...
