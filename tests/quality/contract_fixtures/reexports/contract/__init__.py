"""Names offered without being declared here; ``__all__`` fences none of them off."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from sqlite3 import Connection, connect
from typing import Any, TypeAlias

from tests.quality.contract_fixtures.reexports import BaseFailure as _BaseFailure
from tests.quality.contract_fixtures.reexports import (
    Declared,
    Level,
    Metered,
    Mode,
    Opener,
    RootError,
    TypedError,
    UntypedError,
    Wired,
)
from tests.quality.contract_fixtures.reexports.impl import Engine

CONTRACT_VERSION = "reexports/1"
Rows = list[Any]
Payload: TypeAlias = Any
Handle: TypeAlias = "sqlite3.Connection"
Name: TypeAlias = "str"
GREETING = "sqlite3.Connection"  # control: a plain string constant, not an alias
CONN = connect(":memory:")
NESTED = (1, {"k": object()})

__all__ = ["CONTRACT_VERSION"]


@dataclass(frozen=True)
class _Payload:
    conn: Connection


DEFAULT = _Payload(conn=CONN)


@dataclass(frozen=True)
class Boxed:
    value: str


BOXED = Boxed(CONN)  # type: ignore[arg-type]  # a dataclass stores what it is handed
PLAIN = Boxed("control")
assert sqlite3  # bound on purpose: a foreign module in the surface
assert Level is not None  # a clean enum of the package root, offered as is
assert Mode is not None  # an enum of the package root whose member holds a connection
assert Opener is not None  # an enum of the package root whose method opens a connection
assert RootError is not None  # an exception of the package root whose method opens a connection
assert UntypedError is not None  # a root exception constructed only through BaseException's untyped *args
assert TypedError is not None  # a root exception with a typed constructor of its own
assert Wired is not None  # a root enum whose members store a connection
assert Declared is not None  # a root enum that declares a connection attribute
assert Metered is not None  # a root enum whose metaclass hands out a connection
assert Engine is not None  # an implementation class of this very package, frozen or not


class Failure(_BaseFailure):
    """Refines a root exception, so what it inherits from there is offered too."""
