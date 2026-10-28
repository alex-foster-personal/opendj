"""Package root: Python runs it whenever the contract is imported, so it may import only stdlib."""

from __future__ import annotations

import enum
import sqlite3


class Mode(enum.Enum):
    """A root enum the contract re-exports; a member holding a connection leaks it."""

    CONN = sqlite3.connect(":memory:")


class Level(enum.Enum):
    """A clean root enum the contract also re-exports."""

    LOW = 1


class Opener(enum.Enum):
    """A root enum with clean members whose method hands out a connection."""

    MEMORY = ":memory:"

    def open(self) -> sqlite3.Connection:
        return sqlite3.connect(self.value)


class RootError(Exception):
    """A root exception whose method hands out a connection."""

    connection: sqlite3.Connection

    def open(self) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")


class BaseFailure(Exception):
    """A root exception a contract exception refines; its method hands out a connection."""

    def reopen(self) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")


class UntypedError(Exception):
    """A root exception whose only way in is BaseException's untyped constructor."""


class TypedError(Exception):
    """A root exception with a typed constructor of its own: the control."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class Wired(enum.Enum):
    """A root enum whose members each store a connection their __init__ opened."""

    A = "a"

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path and ":memory:")


class Declared(enum.Enum):
    """A root enum that declares a connection attribute on its members."""

    connection: sqlite3.Connection

    A = "a"


class _ConnectingMeta(enum.EnumMeta):
    @property
    def connection(cls) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")


class Metered(enum.Enum, metaclass=_ConnectingMeta):
    """A root enum with clean members whose metaclass hands out a connection."""

    A = "a"
