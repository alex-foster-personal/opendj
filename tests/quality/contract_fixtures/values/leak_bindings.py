"""Values contract leak fixtures: dataclasses, enums, errors."""

from __future__ import annotations

import dataclasses as _dataclasses
import datetime as _dt
import io as _io
import sqlite3 as _sqlite3
from abc import ABC as _ABC
from collections.abc import Callable as _Callable
from collections.abc import Iterator as _Iterator
from dataclasses import InitVar, dataclass, field
from enum import Enum, StrEnum
from functools import cached_property as _cached_property
from pathlib import Path
from sqlite3 import Connection as _Connection  # noqa: F401 (Codex, PR #6182: ``_Connection(":memory:")`` opens one)
from sqlite3 import connect as _connect  # noqa: F401 (Codex, PR #6182: a private import is reached all the same)
from typing import Annotated, ClassVar, Final, Literal, Protocol

from annotated_types import Ge as _Ge
from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import PrivateAttr as _PrivateAttr
from pydantic import computed_field as _computed_field
from pydantic import field_validator as _field_validator


CONTRACT_VERSION = "values/1"
SHARED: _sqlite3.Connection | None = None
RETRIES: int = 3
BATCH: Final = 10


@dataclass(frozen=True)
class OpensItself:
    path: Path

    def open(self) -> _sqlite3.Connection:
        return _sqlite3.connect(self.path)

    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class CachesConnection:
    path: Path

    @_cached_property
    def conn(self) -> _sqlite3.Connection:
        return _sqlite3.connect(self.path)


@dataclass(frozen=True)
class _Handle:
    path: Path

    def open(self) -> _sqlite3.Connection:
        return _sqlite3.connect(self.path)


@dataclass(frozen=True)
class HoldsHandle:
    handle: _Handle


@dataclass(frozen=True)
class _Inner:
    conn: _sqlite3.Connection


@dataclass(frozen=True)
class Outer:
    inner: _Inner


class Mode(Enum):
    FAST = "fast"
    CONN = _sqlite3.connect(":memory:")


class Opener(Enum):
    MEMORY = ":memory:"

    def open(self) -> _sqlite3.Connection:
        return _sqlite3.connect(self.value)

    def label(self) -> str:
        return self.name.lower()


class _Hidden(Enum):
    CONN = _sqlite3.connect(":memory:")


@dataclass(frozen=True)
class Pinned:
    mode: Literal[_Hidden.CONN]
    side: Literal["left", "right"]


def opens(path: Path) -> _sqlite3.Connection:
    return _sqlite3.connect(path)


def reads(path: Path) -> list[str]:
    return [path.name]


@dataclass(frozen=True)
class Settings:
    CONN: ClassVar[_sqlite3.Connection] = _sqlite3.connect(":memory:")
    name: str


class ContractError(Exception):
    connection: _sqlite3.Connection
    code: int

    def open(self) -> _sqlite3.Connection:
        return _sqlite3.connect(":memory:")

    def reason(self) -> str:
        return str(self)


@dataclass(frozen=True)
class Rows:
    names: tuple[str, ...]

    def __iter__(self) -> _Iterator[_sqlite3.Connection]:
        return iter([_sqlite3.connect(":memory:")])


@dataclass(frozen=True, init=False)
class LiveConnection(_sqlite3.Connection):
    pass


class StoreError(_sqlite3.Error):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class TypedError(Exception):
    detail: str

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.detail = message


class StashedError(Exception):  # Sol, PR #6182: a typed constructor storing undeclared state
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.connection = _sqlite3.connect(message)


class HandedError(Exception):  # the error handed to a call that could set anything on it
    def __init__(self, message: str) -> None:
        Exception.__init__(self, message)


HELD_ERROR = TypedError("x")


@dataclass(frozen=True, init=False)
class Custom:
    name: str

    def __init__(self, conn: _sqlite3.Connection) -> None:
        object.__setattr__(self, "name", str(conn))


@dataclass(frozen=True)
class Envelope:
    @dataclass(frozen=True)
    class Payload:
        conn: _sqlite3.Connection

    payload: Envelope.Payload


@dataclass(frozen=True)
class Seeded:
    name: str
    conn: InitVar[_sqlite3.Connection]


@dataclass(frozen=True)
class UsesOpener:
    opener: Opener  # control: Opener is reported where it is declared, not again here


def runs(conn: str = _sqlite3.connect(":memory:"), limit: int = 3) -> None:  # type: ignore[assignment]
    return None


class _Token(str):
    __slots__ = ("connection",)

    def __new__(cls, text: str) -> _Token:
        token = super().__new__(cls, text)
        token.connection = _sqlite3.connect(":memory:")  # type: ignore[attr-defined]
        return token


class _Rows(list[str]):
    connection = _sqlite3.connect(":memory:")


class _Table(dict[str, str]):
    connection = _sqlite3.connect(":memory:")


TOKEN = _Token("x")
ROWS = _Rows(["a"])
TABLE = _Table(a="b")
ROOT = Path("/srv")  # control: a PosixPath is the standard library's own
NAMES = ["a", "b"]  # control
LOOKUP = {"a": "b"}  # control


class Wired(Enum):
    A = "a"

    def __init__(self, path: str) -> None:
        self.connection = _sqlite3.connect(":memory:")


def wired(path: Path) -> str:
    return path.name


wired.connection = _sqlite3.connect(":memory:")  # type: ignore[attr-defined]


@dataclass(frozen=True, init=False)
class ListPayload(list[str]):
    pass


class Tone(StrEnum):  # control: a str enum's members are still its values
    HIGH = "high"


class Stamped(Enum):
    A = "a"


Stamped.A.connection = _sqlite3.connect(":memory:")  # type: ignore[attr-defined]


class Renamed(Enum):
    A = "a"


Renamed.A._name_ = _sqlite3.connect(":memory:")  # type: ignore[assignment]  # what Renamed.A.name hands out


class Rehomed(Enum):
    A = "a"


Rehomed.A.__objclass__ = _sqlite3.connect(":memory:")  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Sealed:  # control
    name: str


class Unsealed(Sealed):  # inherits frozen=True, but its instances take new attributes
    pass


@dataclass(frozen=True)
class Resealed(Sealed):  # control: decorated itself, so frozen like its base
    token: str = "safe"


@dataclass(frozen=True)
class Shadowed(Resealed):
    token = _sqlite3.connect(":memory:")  # type: ignore[assignment]


@dataclass(frozen=True)
class Restated(Resealed):  # control: a plain value over the inherited default
    token = "other"


@dataclass(frozen=True)
class DefaultedRow:  # its default is reported once, as the field's, not again as a binding
    conn: str = _sqlite3.connect(":memory:")  # type: ignore[assignment]


@dataclass(frozen=True, eq=False)
class Compares:
    name: str

    def __eq__(self, other: _sqlite3.Connection) -> _sqlite3.Connection:  # type: ignore[override]
        return other

    def __hash__(self) -> int:
        return hash(self.name)


@dataclass(frozen=True)
class Attached:
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "connection", _sqlite3.connect(":memory:"))


@dataclass(frozen=True)
class Slotted:
    __slots__ = ("extra", "name")
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _sqlite3.connect(":memory:"))


@dataclass(frozen=True)
class Normalized:  # control: resets its own declared field, reading only declared fields
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.tags) > 9:
            raise ValueError("too many tags")
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True)
class Respelled:
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "na" + "me", self.name)


def _attach(row: object) -> None:
    object.__setattr__(row, "connection", _sqlite3.connect(":memory:"))


@dataclass(frozen=True)
class Delegated:
    name: str

    def __post_init__(self) -> None:
        _attach(self)


ATTACHED = Attached("x")
SLOTTED = Slotted("x")
LOOP = []  # type: ignore[var-annotated]
LOOP.append(LOOP)
ROW = ["a"]
DIAMOND = (ROW, ROW)  # control: one list reached twice is no cycle


class _Meta(type):
    def __getattr__(cls, name: str) -> _sqlite3.Connection:
        return _sqlite3.connect(":memory:")


@dataclass(frozen=True)
class Metered(metaclass=_Meta):
    name: str


class _Zone(_dt.tzinfo):
    """A time zone that carries a connection along with every time stamped in it."""

    connection = _sqlite3.connect(":memory:")

    def utcoffset(self, dt: _dt.datetime | None) -> _dt.timedelta:
        return _dt.timedelta(0)

    def dst(self, dt: _dt.datetime | None) -> _dt.timedelta:
        return _dt.timedelta(0)

    def tzname(self, dt: _dt.datetime | None) -> str:
        return "Z"


STAMP = _dt.datetime(2026, 1, 1, tzinfo=_Zone())
UTC_STAMP = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)  # control: a fixed datetime.timezone


@dataclass(frozen=True)
class Subscripts:
    name: str

    def __class_getitem__(cls, item: _sqlite3.Connection) -> _sqlite3.Connection:
        return item


def receives(self: _sqlite3.Connection) -> None:  # a function's `self` is an argument like any other
    return None


@dataclass(frozen=True)
class StaticSelf:
    name: str

    @staticmethod
    def build(cls: _sqlite3.Connection) -> str:  # noqa: PLW0211  # a staticmethod has no receiver to skip
        return str(cls)


class Configured(_BaseModel):
    model_config = _ConfigDict(
        frozen=True, extra="forbid", plugin_settings={"connection": _sqlite3.connect(":memory:")}
    )
    name: str


class Titled(_BaseModel):  # control: a plain-value setting is a boundary value
    model_config = _ConfigDict(frozen=True, extra="forbid", title="titled")
    name: str
