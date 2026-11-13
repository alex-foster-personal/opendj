"""Values contract leak fixtures: annotated and Pydantic surfaces."""

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


def tagged(name: Annotated[str, _LIVE]) -> str:
    return name


def bounded(limit: Annotated[int, _Ge(0), "rows", _Field(ge=0, description="rows", default_factory=list)]) -> int:
    return limit  # control: tooling constraints with boundary values, and plain metadata


def extra(name: Annotated[str, _Field(json_schema_extra={"connection": _LIVE})]) -> str:
    return name


def gated(
    limit: Annotated[int, _Ge(_LIVE)],
    name: Annotated[str, _Field(default=_LIVE)],
    rows: Annotated[list[str], _Field(default_factory=_sqlite3.Row)],
) -> int:
    return limit


@dataclass(frozen=True)
class TaggedRow:
    name: Annotated[str, _LIVE]


class TaggedModel(_BaseModel):
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: Annotated[str, _LIVE]
    size: Annotated[int, _Ge(0)]  # control
    note: str = _Field("", json_schema_extra={"connection": _LIVE})  # type: ignore[dict-item]


class TaggedPort(Protocol):
    name: Annotated[str, _LIVE]


class Hidden(_BaseModel):
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str
    _connection: _sqlite3.Connection = _PrivateAttr(default_factory=lambda: _sqlite3.connect(":memory:"))


class _Disguised(str):
    __module__ = "builtins"  # claims to be the language's own str
    connection = _sqlite3.connect(":memory:")


DISGUISED = _Disguised("x")


class Payload:  # Codex, PR #6182: names typing as its home, which typing does not hold
    __module__ = "typing"
    connection = _sqlite3.connect(":memory:")


def opened() -> _sqlite3.Connection:
    return _sqlite3.connect(":memory:")


opened.__module__ = "typing"


class _Mixin:
    __module__ = "pydantic"
    connection = _sqlite3.connect(":memory:")


@dataclass(frozen=True)
class Mixed(_Mixin):
    name: str


class _Bound:
    __module__ = "annotated_types"

    def __init__(self) -> None:
        self.connection = _sqlite3.connect(":memory:")


def spoofed(limit: Annotated[int, _Bound()]) -> int:
    return limit


class _SpoofedMeta(type):
    __module__ = "abc"

    def __getattr__(cls, name: str) -> _sqlite3.Connection:
        return _sqlite3.connect(":memory:")


@dataclass(frozen=True)
class MetaSpoofed(metaclass=_SpoofedMeta):
    name: str


@dataclass(frozen=True, eq=False)
class EqSpoofed:
    name: str

    def __eq__(self, other: _sqlite3.Connection) -> _sqlite3.Connection:  # type: ignore[override]
        return other

    __eq__.__module__ = "typing"

    def __hash__(self) -> int:
        return hash(self.name)


class _SpoofedError(Exception):
    __module__ = "typing"

    def open(self) -> _sqlite3.Connection:
        return _sqlite3.connect(":memory:")


class Raised(_SpoofedError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class _BuiltinMeta(type):
    __module__ = "builtins"  # Codex, PR #6182: claims to be the language's own metaclass

    @property
    def connection(cls) -> _sqlite3.Connection:
        return _sqlite3.connect(":memory:")


@dataclass(frozen=True)
class MetaBuiltin(metaclass=_BuiltinMeta):
    name: str


@dataclass(frozen=True)
class FieldMetadata:
    name: str = field(metadata={"connection": _LIVE})
    size: int = field(default=0, metadata={"unit": "rows"})  # control


def _name(self: object) -> str:
    return "x"


def _drop(self: object) -> _sqlite3.Connection:
    return _sqlite3.connect(":memory:")


class _Held(property):
    connection = _LIVE


@dataclass(frozen=True)
class Described:
    name: str
    label = property(_name, doc=_LIVE)  # type: ignore[arg-type]
    title = property(_name, doc="a title")  # control
    cleared = property(_name, None, _drop)  # type: ignore[arg-type]
    cursor = property(_LIVE.cursor)
    held = _Held(_name)  # a property subclass carries state of its own


def annotated(path: Path) -> str:  # Codex, PR #6182: an annotation that names no parameter
    return path.name


annotated.__annotations__["connection"] = _LIVE


class Receiver(Protocol):
    def read(self, path: Path) -> str: ...  # control: the receiver's own name is a parameter


@dataclass(frozen=True)
class Flagged:  # Codex, PR #6182: dataclasses.fields() hands out every flag a field keeps
    name: str = field(default="ok", compare=_LIVE)  # type: ignore[call-overload]
    size: int = field(default=0, repr=False, hash=None, kw_only=True)  # control


@dataclass(frozen=True)
class Retyped:  # Codex, PR #6182: dataclasses.fields() hands out the Field's own type
    name: str


Retyped.__dataclass_fields__["name"].type = _LIVE  # type: ignore[assignment]


class Remapped(Enum):  # Codex, PR #6182: Remapped.CONNECTION reads the class's member map
    A = "a"


Remapped._member_map_["CONNECTION"] = _LIVE  # type: ignore[assignment]


class Revalued(Enum):
    A = "a"


Revalued._value2member_map_["b"] = _LIVE  # type: ignore[assignment]


class Rekeyed(Enum):  # Codex, PR #6182: iterating Rekeyed.__members__ hands out its keys
    A = "a"


Rekeyed._member_map_[_LIVE] = Rekeyed.A  # type: ignore[index]


class Unvalued(Enum):
    A = "a"


Unvalued._value2member_map_[_LIVE] = Unvalued.A


def documented(path: Path) -> str:  # Codex, PR #6182: read.__doc__ is handed out too
    return path.name


documented.__doc__ = _LIVE  # type: ignore[assignment]


def wrapping(path: Path) -> str:
    """A plain docstring: the control."""
    return path.name


wrapping._cache = _LIVE  # type: ignore[attr-defined]  # a private name hands its value out too


@dataclass(frozen=True)
class Redocumented:
    name: str


Redocumented.__doc__ = _LIVE  # type: ignore[assignment]


class Computed(_BaseModel):  # Codex, PR #6182: model_computed_fields hands out each setting
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str

    @_computed_field(json_schema_extra={"connection": _LIVE})  # type: ignore[prop-decorator,dict-item]
    @property
    def label(self) -> str:
        return self.name

    @_computed_field(description="a size")  # type: ignore[prop-decorator]  # control
    @property
    def size(self) -> int:
        return len(self.name)


class ComputedAs(_BaseModel):
    model_config = _ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    name: str

    @_computed_field(return_type=_sqlite3.Connection)  # type: ignore[prop-decorator]
    @property
    def label(self) -> str:
        return self.name


@dataclass(frozen=True)
class Hoards:  # Codex, PR #6182: Hoards._connection hands its value out like a public name
    name: str
    _connection = _LIVE
    __pool = _LIVE  # name-mangled to _Hoards__pool, and just as readable


class PrivatePort(Protocol):
    def read(self) -> str: ...

    def _open(self) -> _sqlite3.Connection: ...


class _Lenient:  # Codex, PR #6182: says it equals list, so `in` would approve it
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = object.__hash__

    def __call__(self) -> _sqlite3.Connection:
        return _LIVE


def lenient(rows: Annotated[list[str], _Field(default_factory=_Lenient())]) -> int:  # type: ignore[arg-type]
    return len(rows)


class Rewrapped(_BaseModel):  # Codex, PR #6182: model_computed_fields keeps its own property
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str

    @_computed_field  # type: ignore[prop-decorator]
    @property
    def label(self) -> str:
        return self.name


Rewrapped.model_computed_fields["label"].wrapped_property = property(lambda self: _LIVE)


@dataclass(frozen=True)
class Retains:  # Codex, PR #6182: an inert special method still hands out what it holds
    name: str

    def __repr__(self) -> str:  # the control: an inert method that holds nothing
        return self.name

    def __format__(self, spec: str, held: _sqlite3.Connection = _LIVE) -> str:
        return self.name


def _enclosing() -> _Callable[..., str]:
    held = _LIVE

    def __str__(self: object) -> str:
        return str(held)

    return __str__


@dataclass(frozen=True)
class Stored:
    name: str

    def __bool__(self) -> bool:
        return True

    __str__ = _enclosing()


Stored.__bool__.connection = _LIVE  # type: ignore[attr-defined]


class Reclassed(Exception):  # super()'s __class__ cell, pointed at the connection instead
    def __init__(self, message: str) -> None:
        super().__init__(message)


Reclassed.__init__.__closure__[0].cell_contents = _LIVE  # type: ignore[index]


@dataclass(frozen=True)
class Rerepr:  # the dataclass's own __repr__ guard, wrapping a hand-written function instead
    name: str


_guard = Rerepr.__repr__
_guard.__closure__[_guard.__code__.co_freevars.index("user_function")].cell_contents = lambda self: str(_LIVE)  # type: ignore[index]


class Rebased(Exception):  # super()'s __class__ cell, pointed at a class that does not bind it
    def __init__(self, message: str) -> None:
        super().__init__(message)


Rebased.__init__.__closure__[0].cell_contents = _Lenient  # type: ignore[index]


@dataclass(frozen=True)
class Retagged:  # the dataclass's own __repr__ guard, with state set on it
    name: str


Retagged.__repr__.connection = _LIVE  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Rerouted:  # the dataclass's own __repr__ guard, its __wrapped__ pointed at another function
    name: str


Rerouted.__repr__.__wrapped__ = lambda self: str(_LIVE)  # type: ignore[attr-defined]


class Stashed(StrEnum):  # Codex, PR #6182: the enum machinery's own lists are on the class too
    A = "a"


Stashed._unhashable_values_.append(_LIVE)  # type: ignore[attr-defined]


class Retargeted(Enum):  # the machinery's member type swapped for an open one
    A = "a"


Retargeted._member_type_ = _sqlite3.Connection  # type: ignore[attr-defined]


class _Name(str):
    held = _LIVE


class Misnamed(Enum):  # the machinery's name list, holding a str subclass that carries state
    A = "a"


Misnamed._member_names_ = [_Name("A")]  # type: ignore[attr-defined]


class HeldReceiver(Protocol):  # Codex, PR #6182: Port.read.__annotations__ and __defaults__ hand the receiver's out
    def read(self) -> str: ...

    def write(self=_LIVE) -> str: ...  # type: ignore[assignment]


HeldReceiver.read.__annotations__["self"] = _LIVE


def kwheld() -> str:  # Codex, PR #6182: __kwdefaults__ can hold a key no parameter takes
    return "x"


kwheld.__kwdefaults__ = {"connection": _LIVE}


def posheld() -> str:  # and __defaults__ an entry no parameter takes
    return "x"


posheld.__defaults__ = (_LIVE,)
