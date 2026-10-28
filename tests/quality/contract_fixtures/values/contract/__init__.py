"""Value types, functions and enums that leak open types through members or nesting."""

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


_LIVE = _sqlite3.connect(":memory:")


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

_compiled: dict[str, _Callable[..., str]] = {}
exec("def __repr__(self):\n    return 'x'\n", _compiled)


@dataclass(frozen=True)
class Compiled:  # Codex, PR #6182: exec names its code <string> too, so the file name proves nothing
    name: str


Compiled.__repr__ = _compiled["__repr__"]  # type: ignore[method-assign]
Compiled.__repr__.connection = _LIVE  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Machined:  # Codex, PR #6182: a Protocol's machinery name, on a class kind that binds no such thing
    name: str
    _abc_impl = _LIVE


class MachinedPort(Protocol):  # a Protocol's own machinery name, holding what no machinery does
    def read(self) -> str: ...


MachinedPort._abc_impl = _LIVE  # type: ignore[attr-defined]


exec(
    "def _make(held):\n"
    "    def __str__(self):\n"
    "        return str(held)\n"
    "    return __str__\n"
    "def __format__(self, spec, held=_LIVE):\n"
    "    return ''\n"
    "def __bool__(self):\n"
    "    return True\n",
    {"_LIVE": _LIVE},
    _compiled,
)


@dataclass(frozen=True)
class Evaluated:  # compiled from a string, each holding a connection another way
    name: str


Evaluated.__str__ = _compiled["_make"](_LIVE)  # type: ignore[assignment]
Evaluated.__format__ = _compiled["__format__"]  # type: ignore[method-assign]
Evaluated.__bool__ = _compiled["__bool__"]  # type: ignore[attr-defined]
Evaluated.__bool__.__annotations__["return"] = _LIVE  # type: ignore[attr-defined]


def kwkeyed() -> str:  # Codex, PR #6182: a __kwdefaults__ key is held as much as its value
    return "x"


kwkeyed.__kwdefaults__ = {_LIVE: "safe"}  # type: ignore[dict-item]


def annkeyed() -> str:  # and so is an __annotations__ key
    return "x"


annkeyed.__annotations__[_LIVE] = str  # type: ignore[index]


@dataclass(frozen=True)
class Redoc:  # Codex, PR #6182: the dataclass's own __repr__ guard, its docstring swapped for a connection
    name: str


Redoc.__repr__.__doc__ = _LIVE  # type: ignore[assignment]


class _Registry(_ABC):  # noqa: B024  # only its registry matters here
    pass


_Registry.register(_sqlite3.Connection)


class Registered(_BaseModel):  # Codex, PR #6182: another ABC's _abc_impl, its registry holding a class
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


type.__setattr__(Registered, "_abc_impl", vars(_Registry)["_abc_impl"])


@dataclass(frozen=True)
class Dundered:  # Codex, PR #6182: a dunder name the tooling never binds, holding a connection
    name: str
    __connection__ = _LIVE


@dataclass(frozen=True)
class Matched:  # a dunder the tooling does bind, holding what it never holds
    name: str


Matched.__match_args__ = (_LIVE,)  # type: ignore[misc,assignment]


class Redescribed(_BaseModel):  # Codex, PR #6182: field metadata rewritten after the class is built
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Redescribed.model_fields["name"].description = _LIVE  # type: ignore[assignment]


class _Spoofed(Exception):
    connection = _LIVE


_Spoofed.__module__ = "builtins"


class SpoofedError(_Spoofed):  # Codex, PR #6182: an exception base posing as the language's
    pass


class Reassigned(_BaseModel):  # Codex, PR #6182: Pydantic's private field bookkeeping holding a connection
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str
    label: str = "x"


Reassigned.model_fields["name"]._original_annotation = _LIVE
Reassigned.model_fields["label"]._original_assignment = _LIVE


class Recorded(_BaseModel):  # the constructor's record, rewritten after the class is built
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Recorded.model_fields["name"]._attributes_set["title"] = _LIVE

__annotations__["unbound"] = _LIVE  # Codex, PR #6182: an annotation that names no binding
_hidden = "safe"
__annotations__["_hidden"] = _LIVE  # Codex, PR #6182: a private binding's annotation holding a connection


class Fieldkeyed(_BaseModel):  # Codex, PR #6182: model_fields keyed by a connection
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Fieldkeyed.model_fields[_LIVE] = Fieldkeyed.model_fields["name"]  # type: ignore[index]


class Renumbered(_BaseModel):  # a field name no class declares
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Renumbered.model_fields["ghost"] = Renumbered.model_fields["name"]


class Rerecorded(_BaseModel):  # Codex, PR #6182: Pydantic's settings record keyed by a connection
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Rerecorded.model_fields["name"]._attributes_set[_LIVE] = "safe"  # type: ignore[index]
SHOWN = "safe"
__annotations__["SHOWN"] = _LIVE  # control: an offered name's annotation is read once, with its binding


class Rogue(Enum):  # Codex, PR #6182: a member map entry iteration never shows
    ON = "on"


_rogue = object.__new__(Rogue)
_rogue.__dict__["connection"] = _LIVE
Rogue._member_map_["ROGUE"] = _rogue


class Unlisted(Enum):  # the same through the value map: Unlisted("off") hands it out
    ON = "on"


_unlisted = object.__new__(Unlisted)
_unlisted._value_ = "off"
_unlisted.__dict__["connection"] = _LIVE
Unlisted._value2member_map_["off"] = _unlisted

_port = object.__new__(Receiver)  # type: ignore[type-abstract]  # Codex, PR #6182: a Protocol instance stores what is set on it
_port.__dict__["connection"] = _LIVE
import tests.quality.contract_fixtures.clean.contract as _other  # noqa: E402, F401 (control: a contract)
import tests.quality.contract_fixtures.values as _root  # noqa: E402, F401 (Codex, PR #6182: the package root)

_bare_port = object.__new__(Receiver)  # type: ignore[type-abstract]  # control: one that stores nothing


@dataclass(frozen=True)
class Dckeyed:  # Codex, PR #6182: __dataclass_fields__ keyed by a connection
    name: str


Dckeyed.__dataclass_fields__[_LIVE] = Dckeyed.__dataclass_fields__["name"]  # type: ignore[index]


@dataclass(frozen=True)
class Dcrenamed:  # a key that is not the name of the Field it maps to
    name: str


Dcrenamed.__dataclass_fields__["alias"] = Dcrenamed.__dataclass_fields__["name"]


@dataclass(frozen=True)
class Annkeyed:  # a class's own annotations keyed by a connection
    name: str


Annkeyed.__annotations__[_LIVE] = int  # type: ignore[index]


class _NotAField:
    name = "ghost"
    _field_type = None
    connection = _LIVE


@dataclass(frozen=True)
class Dcghost:  # Codex, PR #6182: a field map entry dataclasses.fields() never returns
    name: str


Dcghost.__dataclass_fields__["ghost"] = _NotAField()  # type: ignore[assignment]


@dataclass(frozen=True)
class Dcpseudo:  # a genuine ClassVar entry under a name the class never annotates
    name: str


_pseudo = field(default=_LIVE)
_pseudo.name = "ghost"  # type: ignore[attr-defined]
_pseudo._field_type = _dataclasses._FIELD_CLASSVAR  # type: ignore[attr-defined]
Dcpseudo.__dataclass_fields__["ghost"] = _pseudo  # type: ignore[assignment]


class _FakeClassVar:
    name = "TAG"
    _field_type = _dataclasses._FIELD_CLASSVAR  # type: ignore[attr-defined]
    connection = _LIVE


@dataclass(frozen=True)
class Dcfaked:  # an annotated ClassVar's entry swapped for an object that only looks like its Field
    TAG: ClassVar[str] = "tag"
    name: str = "x"


Dcfaked.__dataclass_fields__["TAG"] = _FakeClassVar()  # type: ignore[assignment]


class Decorated(_BaseModel):  # Codex, PR #6182: Pydantic's decorator records keyed by a connection
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Decorated.__pydantic_decorators__.validators[_LIVE] = "safe"  # type: ignore[index]


def _elsewhere(self: object) -> int:
    return 0


class Redecorated(_BaseModel):  # a decorator record pointing at a function its class never binds
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str

    @_computed_field  # type: ignore[prop-decorator]
    @property
    def size(self) -> int:
        return 1


object.__setattr__(Redecorated.__pydantic_decorators__.computed_fields["size"], "func", _elsewhere)


class Decorations(_BaseModel):  # control: a validator and a computed field, as Pydantic records them
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str

    @_field_validator("name")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return value.strip()

    @_computed_field  # type: ignore[prop-decorator]
    @property
    def size(self) -> int:
        return len(self.name)


class Validtyped(_BaseModel):  # Codex, PR #6182: a validator record that keeps an implementation type
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str

    @_field_validator("name")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return value.strip()


object.__setattr__(
    Validtyped.__pydantic_decorators__.field_validators["_stripped"].info, "json_schema_input_type", _sqlite3.Connection
)


class Coreheld(_BaseModel):  # Codex, PR #6182: Pydantic's core schema with a live value stored in it
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Coreheld.__pydantic_core_schema__["connection"] = _sqlite3.connect(":memory:")  # type: ignore[typeddict-unknown-key]


class Corekeyed(_BaseModel):  # Pydantic's core schema keyed by something other than a name
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Corekeyed.__pydantic_core_schema__[1] = "safe"  # type: ignore[index]


class _Loose(_BaseModel):  # not frozen, so not a type a contract hands out
    name: str


class Hooked(_BaseModel):  # Pydantic's schema hook, rebound to a class no contract may offer
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Hooked.__pydantic_core_schema__["metadata"]["pydantic_js_functions"].append(  # type: ignore[typeddict-item]
    _Loose.__get_pydantic_json_schema__
)


class _Nested(_BaseModel):  # control: a private model a public one nests, read through its schema
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


class Nests(_BaseModel):  # control
    model_config = _ConfigDict(frozen=True, extra="forbid")
    inner: _Nested


class _Ping(_BaseModel):  # control: private models that nest each other, each schema naming the other
    model_config = _ConfigDict(frozen=True, extra="forbid")
    pong: _Pong | None = None


class _Pong(_BaseModel):
    model_config = _ConfigDict(frozen=True, extra="forbid")
    ping: _Ping | None = None


_Ping.model_rebuild()


class Rally(_BaseModel):  # control
    model_config = _ConfigDict(frozen=True, extra="forbid")
    ping: _Ping


class Genericheld(_BaseModel):  # Pydantic's generic metadata naming an implementation type
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Genericheld.__pydantic_generic_metadata__["args"] = (_sqlite3.Connection,)  # type: ignore[typeddict-item]


class Corecycled(_BaseModel):  # Codex, PR #6182: Pydantic's core schema holding a dict that holds itself
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


_LOOP: dict[str, object] = {}
_LOOP["self"] = _LOOP
Corecycled.__pydantic_core_schema__["loop"] = _LOOP  # type: ignore[typeddict-unknown-key]


class Coreshared(_BaseModel):  # control: one dict reached from two branches is shared, not a cycle
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


_SHARED_BRANCH = {"note": "shared"}
Coreshared.__pydantic_core_schema__["metadata"]["left"] = _SHARED_BRANCH  # type: ignore[typeddict-unknown-key]
Coreshared.__pydantic_core_schema__["metadata"]["right"] = [_SHARED_BRANCH]  # type: ignore[typeddict-unknown-key]


@dataclass(frozen=True)
class Paramheld:  # Codex, PR #6182: a frozen dataclass whose retained parameters hold a live value
    name: str


object.__setattr__(Paramheld.__dataclass_params__, "init", _sqlite3.connect(":memory:"))  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Paramfaked:  # a truthy stand-in for frozen=True, kept where the decorator stores a bool
    name: str


object.__setattr__(Paramfaked.__dataclass_params__, "frozen", "yes")  # type: ignore[attr-defined]


class _Params:  # every field a bool, but not the record the decorator keeps
    __slots__ = ("connection", "eq", "frozen", "init", "order", "repr", "unsafe_hash")

    def __init__(self) -> None:
        for name in self.__slots__:
            setattr(self, name, True)
        self.connection = _sqlite3.connect(":memory:")


@dataclass(frozen=True)
class Paramswapped:  # Codex, PR #6182: dataclass parameters replaced by another object
    name: str


Paramswapped.__dataclass_params__ = _Params()  # type: ignore[attr-defined,assignment]


class _Elsewhere(_BaseModel):  # a model no contract offers, with a field Revalidated does not declare
    model_config = _ConfigDict(frozen=True, extra="forbid")
    secret: str


class Revalidated(_BaseModel):  # Codex, PR #6182: another model's validator and serializer, swapped in
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Revalidated.__pydantic_validator__ = _Elsewhere.__pydantic_validator__


class Reserialized(_BaseModel):  # the same, for the serializer alone
    model_config = _ConfigDict(frozen=True, extra="forbid")
    name: str


Reserialized.__pydantic_serializer__ = _Elsewhere.__pydantic_serializer__


__connection__ = _LIVE  # Codex, PR #6182: a dunder the import system never binds is reached like any name
__version__ = "1.0"  # control: a plain value under a dunder name is read and passes


class _Opener:  # Codex, PR #6182: a private class's code hands out what it returns
    @staticmethod
    def open() -> _sqlite3.Connection:
        return _LIVE

    @classmethod
    def reopen(cls) -> _sqlite3.Connection:
        return _LIVE

    @property
    def current(self) -> _sqlite3.Connection:
        return _LIVE

    def label(self) -> str:  # control: a method that returns a boundary type
        return "opener"

    def __repr__(self) -> str:  # control: an inert special method
        return "_Opener()"

    class Inner:
        def cursor(self) -> _sqlite3.Cursor:
            return _LIVE.cursor()

        def name(self) -> str:  # control
            return "inner"


class _FileHandle(_io.FileIO):  # Codex, PR #6182: ``_FileHandle(path)`` opens a file through what it inherits
    pass


class _Kind(type):  # a metaclass's members reach every class it makes
    pass


class _Made(metaclass=_Kind):
    pass


class _Bare:  # Sol, PR #6182: a plain private class takes any live state a consumer attaches
    pass


@dataclass
class _Mutable:  # a private mutable dataclass carrying a connection
    conn: _sqlite3.Connection


@dataclass(frozen=True)
class _Point:  # control: a private frozen dataclass of plain values is declared like a public one
    x: int
    y: int
