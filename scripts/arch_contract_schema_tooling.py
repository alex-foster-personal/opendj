"""Enum, Protocol and model tooling, schemas and generated methods.

Split from ``scripts/arch_contract_schema.py``. Import the public names
from ``scripts.arch_contract_schema``; this module is an implementation slice.
"""

from __future__ import annotations

import abc
import contextvars
import dataclasses
import enum
import inspect
import operator
import sys
import types
import typing

from pydantic import BaseModel, ConfigDict, computed_field
from pydantic_core import PydanticUndefined

from scripts.arch_contract_schema_kinds import _Plain

# Names used across siblings are bound on scripts.arch_contract_schema after load;
# function globals are rebound to that module so a monkeypatch there is seen.
# ruff: noqa: F821

# Every hand-written special method is a way in or out (indexing, assigning, iterating,
# constructing, calling, comparing: Python lets ``__eq__`` return anything) except these,
# whose results the interpreter checks are strings, flags or sizes, or that take its own arguments.
_INERT_SPECIAL_METHODS = frozenset(
    {
        "__repr__", "__str__", "__format__", "__hash__", "__bool__", "__len__", "__sizeof__",
        "__exit__", "__aexit__",
        "__init_subclass__", "__post_init__", "__subclasshook__",
    }
)  # fmt: skip


@typing.runtime_checkable
class _PlainPort(typing.Protocol):
    def read(self) -> int: ...


class _PlainModel(BaseModel):
    pass


def _sunder(k: type) -> dict[str, type]:
    return {
        name: type(value)
        for name, value in vars(k).items()
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
    }


# The single-underscore names the tooling itself binds, per kind of class, with the type each holds.
# An enum's are read in full by _enum_machinery_intact; a Protocol's or model's are read by type.
_ENUM_MACHINERY = frozenset(_sunder(_Plain))
_PORT_MACHINERY = _sunder(_PlainPort)
_MODEL_MACHINERY = _sunder(_PlainModel)


def _machinery(cls: type) -> typing.Mapping[str, type | None]:
    """The machinery names ``cls``'s own kind binds, each with the type it must hold (None: read apart)."""
    if issubclass(cls, enum.Enum):
        return dict.fromkeys(_ENUM_MACHINERY)
    if typing.Protocol in cls.__mro__:
        return _PORT_MACHINERY
    if issubclass(cls, BaseModel):
        return _MODEL_MACHINERY
    return {}


def _machinery_empty(name: str, cls: type) -> bool:
    """An ABC's ``_abc_impl`` keeps a registry of virtual subclasses, which ``abc._get_dump`` hands out.

    A contract registers none, so the registry must be empty; the caches only remember checks made.
    """
    if name != "_abc_impl":
        return True
    dump = getattr(abc, "_get_dump", None)
    return dump is not None and not dump(cls)[0]


def _surface_member(name: str, member: object, cls: type) -> bool:
    """Whether a class binding is surface: anything but its kind's machinery or an inert special method.

    A private name is surface too: ``Payload._connection`` hands its value out all the same, and
    so does a machinery name that another kind of class binds, or one holding what no machinery does.
    """
    if not (name.startswith("__") and name.endswith("__")):
        machinery = _machinery(cls)
        held = machinery.get(name, object)
        return name not in machinery or not (held is None or type(member) is held) or not _machinery_empty(name, cls)
    if name in _INERT_SPECIAL_METHODS:
        return False
    fn = _unwrapped(member)
    if inspect.isfunction(fn):
        return not _generated(fn)
    return not _tooling_dunder(name, member, cls)


class _PlainError(Exception):
    pass


def _kind_references(cls: type) -> list[type]:
    """Plain classes of each kind ``cls`` is, built by the same tooling: what that tooling binds on them."""
    kinds = [
        (issubclass(cls, enum.Enum), (_Plain, _PlainStr)),
        (typing.Protocol in cls.__mro__, (_PlainPort,)),
        (issubclass(cls, BaseModel), (_PlainModel, _FrozenModel)),
        (dataclasses.is_dataclass(cls), (_PlainRecord, _SlottedRecord)),
        (issubclass(cls, BaseException), (_PlainError,)),
    ]
    return [_PlainError, *(k for matches, refs in kinds if matches for k in refs)]


def _tooling_map_problem(name: str, member: object, cls: type) -> str | None:
    """A tooling map (annotations, dataclass fields) keyed or filled otherwise than its tooling does it.

    Iterating the map hands out its keys, so each must be a name, and a dataclass's the name
    of the ``Field`` it maps to. Its values are handed out too, and ``dataclasses.fields()``
    (which the field scan reads) skips any entry that is not a field, so each must be one it returns.
    """
    if name in ("__pydantic_validator__", "__pydantic_serializer__"):
        return _compiled_problem(cls, member)
    if name == "__pydantic_decorators__" and dataclasses.is_dataclass(member) and not isinstance(member, type):
        return _decorators_problem(cls, member)
    if type(member) is not dict:
        return None
    if name in ("__pydantic_core_schema__", "__pydantic_generic_metadata__"):
        return _core_schema_problem(cls, member)
    stray = next((k for k in member if type(k) is not str), None)
    if stray is not None:
        return f"is keyed by a {type(stray).__qualname__}, where its tooling keys by name"
    if name == "__dataclass_fields__":
        renamed = next((k for k, f in member.items() if getattr(f, "name", None) != k), None)
        if renamed is not None:
            return f"maps {renamed!r} to a field of another name"
        scanned = {id(f) for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
        annotated = {n for k in cls.__mro__ for n in inspect.get_annotations(k)}
        pseudo = {k for k, f in member.items() if k in annotated and _pseudo_field(f)}
        unread = next((k for k, f in member.items() if id(f) not in scanned and k not in pseudo), None)
        if unread is not None:
            return f"maps {unread!r} to a {type(member[unread]).__qualname__} the field scan never reads"
    return None


# The classes whose core schemas are being read, so a nested model's schema that names its parent ends.
_SCHEMAS_READING: contextvars.ContextVar[frozenset[type]] = contextvars.ContextVar(
    "contract_schemas_reading", default=frozenset()
)


def _compiled_problem(cls: type, member: object) -> str | None:
    """Pydantic's validator and serializer are opaque, but each keeps the core schema it was built
    from, which must be the one the class holds and the walk below reads, not another model's."""
    try:
        built = type(member).__reduce__(member)[1][0]  # type: ignore[index]
    except Exception:  # anything that cannot say what it was built from is refused
        built = None
    if built is vars(cls).get("__pydantic_core_schema__"):
        return None
    return f"holds a {type(member).__qualname__} built from another schema than the class's own"


def _core_schema_problem(cls: type, schema: dict[object, object]) -> str | None:
    """Pydantic's own description of a model, read through: a consumer can walk it and take what it holds.

    Its keys are names and its leaves are plain data, the types it validates, and the hooks,
    validators and default factories those types bind; anything else was stored there, not built by Pydantic.
    """
    leaves: list[object] = []
    problem = _schema_tree_problem(schema, frozenset(), set(), leaves)
    if problem:
        return problem
    reading = _SCHEMAS_READING.get() | {cls}  # a schema names its own class and the models it nests
    token = _SCHEMAS_READING.set(reading)
    try:
        owners = {*(t for t in leaves if isinstance(t, type) and _type_problem(t, reading) is None)}
    finally:
        _SCHEMAS_READING.reset(token)
    bound = {id(_decorated(v)) for o in owners for k in o.__mro__ for v in vars(k).values()}
    bound |= {id(f.default_factory) for o in owners for f in _declared_fields(o)}
    for leaf in leaves:
        if isinstance(leaf, type):
            problem = None if leaf in owners else _type_problem(leaf, reading)
            if problem:
                return f"names a type where {problem}"
            continue
        owner = getattr(leaf, "__self__", None) if inspect.ismethod(leaf) else None
        if id(_decorated(leaf)) in bound and (owner is None or owner in owners):
            continue  # a validator, schema hook or default factory one of those types binds
        return f"holds a {type(leaf).__qualname__}, which Pydantic does not put there"
    return None


def _schema_tree_problem(node: object, path: frozenset[int], done: set[int], leaves: list[object]) -> str | None:
    """Walk a schema's dicts, lists and tuples into ``leaves``; a container that holds its own ancestor
    is a cycle no boundary can carry, while one shared by two branches is read once."""
    if type(node) not in (dict, list, tuple):
        if _value_problem(node) is not None:
            leaves.append(node)
        return None
    if id(node) in path:
        return f"holds itself through a {type(node).__qualname__}, which no boundary can carry"
    if id(node) in done:
        return None
    if type(node) is dict:
        held = typing.cast("dict[object, object]", node)
        stray = next((k for k in held if type(k) is not str), None)
        if stray is not None:
            return f"is keyed by a {type(stray).__qualname__}, where Pydantic keys by name"
        items: typing.Iterable[object] = held.values()
    else:
        items = typing.cast("list[object] | tuple[object, ...]", node)
    inner = path | {id(node)}
    found = (_schema_tree_problem(item, inner, done, leaves) for item in items)
    problem = next((p for p in found if p), None)
    done.add(id(node))
    return problem


def _declared_fields(owner: type) -> list[typing.Any]:
    """The field records whose default factories a schema may carry: a model's or a dataclass's."""
    if issubclass(owner, BaseModel):
        held = getattr(owner, "__pydantic_fields__", None)  # BaseModel itself has none
        return list(held.values()) if type(held) is dict else []
    return list(dataclasses.fields(owner)) if dataclasses.is_dataclass(owner) else []


def _decorators_problem(cls: type, held: typing.Any) -> str | None:
    """``__pydantic_decorators__``: per kind, each entry is Pydantic's record of the method its class
    binds under that key, with settings that are plain data and annotations that are annotations."""
    for kind in dataclasses.fields(held):
        group = getattr(held, kind.name)
        if type(group) is not dict:
            return f"{kind.name} holds a {type(group).__qualname__}, where Pydantic keeps a dict"
        for key, record in group.items():
            if type(key) is not str:
                return f"{kind.name} is keyed by a {type(key).__qualname__}, where Pydantic keys by name"
            problem = _decorator_record_problem(cls, key, record)
            if problem:
                return f"{kind.name}[{key!r}] {problem}"
    return None


def _decorator_record_problem(cls: type, key: str, record: typing.Any) -> str | None:
    if not (_tooling(type(record)) and hasattr(record, "__dataclass_fields__") and _tooling(type(record.info))):
        return f"holds a {type(record).__qualname__}, where Pydantic keeps its own record"
    bound = next((vars(k)[key] for k in cls.__mro__ if key in vars(k)), None)
    if record.cls_var_name != key or record.shim is not None or type(record.cls_ref) is not str:
        return "is not the record Pydantic keeps for that name"
    if _decorated(record.func) is not _decorated(bound):
        return "records a function other than the one its class binds under that name"
    info = {f.name: getattr(record.info, f.name) for f in dataclasses.fields(record.info)}
    if "wrapped_property" in info and info.pop("wrapped_property") is not bound:
        return "records a property other than the one its class binds under that name"
    types = {k: info.pop(k) for k in ("return_type", "json_schema_input_type") if k in info}
    if any(t is not PydanticUndefined and not _annotation_like(t) for t in types.values()):
        return "records an annotation that is not one"
    # No other scan reads these, and a consumer can read them back, so they cross the boundary too.
    found = (_type_problem(t) for t in types.values() if t is not PydanticUndefined)
    problem = next((p for p in found if p), None)
    if problem:
        return f"records an annotation where {problem}"
    problem = _value_problem({k: v for k, v in info.items() if v is not PydanticUndefined})
    return problem and f"settings {problem}"


def _decorated(obj: object) -> object:
    """The function a method binding wraps: a bound method's, a classmethod's or a property's getter."""
    if isinstance(obj, (classmethod, staticmethod, types.MethodType)):
        return obj.__func__
    return obj.fget if isinstance(obj, property) else obj


def _pseudo_field(f: object) -> bool:
    """A real ``ClassVar`` or ``InitVar`` entry, which ``fields()`` skips; under an annotated name,
    `_member_problems` refuses it as annotated but no field."""
    markers = (getattr(dataclasses, "_FIELD_CLASSVAR", None), getattr(dataclasses, "_FIELD_INITVAR", None))
    return type(f) is dataclasses.Field and any(getattr(f, "_field_type", None) is m for m in markers if m)


def _tooling_dunder(name: str, member: object, cls: type) -> bool:
    """Whether a dunder data binding is one the tooling of ``cls``'s kind puts there, holding what it holds.

    It must have the type the same tooling binds under that name on a plain class of the kind,
    and where that holds plain data (a str, a tuple of names), it must hold plain data too.
    """
    if name in ("__doc__", "__module__"):  # text, read by _text_problem
        return True
    if _tooling_map_problem(name, member, cls):
        return False
    if name == "__annotations__":  # its entries are the fields and attributes, each read as one
        return type(member) is dict
    refs = [vars(k)[name] for k in _kind_references(cls) if name in vars(k)]
    return any(
        type(member) is type(ref) and (_value_problem(ref) is not None or _value_problem(member) is None)
        for ref in refs
    )


def _unwrapped(member: object) -> object:
    return member.__func__ if isinstance(member, (classmethod, staticmethod)) else member


@dataclasses.dataclass(frozen=True)
class _PlainRecord:
    x: int


@dataclasses.dataclass(frozen=True, slots=True)
class _SlottedRecord:
    x: int


# The tooling's own closures: a dataclass's recursion-guarded __repr__, a frozen dataclass's or
# model's field-tuple __hash__, and a Protocol's __subclasshook__. Their code is fixed; their cells are read.
class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: int

    @computed_field  # type: ignore[prop-decorator]  # so the computed-field machinery is a reference too
    @property
    def y(self) -> int:
        return self.x


class _FieldlessModel(BaseModel):  # a frozen model with no fields hashes through a constant getter
    model_config = ConfigDict(frozen=True)


_TOOLING_CODES = frozenset(
    {
        *(
            fn.__code__
            for k, name in (
                (_PlainRecord, "__repr__"),
                (_PlainRecord, "__hash__"),
                (_FrozenModel, "__hash__"),
                (_PlainPort, "__subclasshook__"),
            )
            if inspect.isfunction(fn := vars(k).get(name))
        ),
        *(
            cell.cell_contents.__code__
            for cell in getattr(vars(_FieldlessModel).get("__hash__"), "__closure__", None) or ()
            if inspect.isfunction(cell.cell_contents)
        ),
    }
)


def _generated(fn: typing.Callable[..., object]) -> bool:
    """A dataclass's generated method (its fields are read as fields) or typing's Protocol stub."""
    code = getattr(fn, "__code__", None)
    if code in _TOOLING_CODES and isinstance(fn, types.FunctionType):
        return _tooling_closure(fn)
    if code is not None and code.co_filename == "<string>" and isinstance(fn, types.FunctionType):
        return _dataclass_generated(fn)
    return _tooling(fn)


def _dataclass_generated(fn: types.FunctionType) -> bool:
    """Whether a function compiled from a string holds only what ``dataclasses`` gives its methods.

    The file name ``<string>`` is any ``exec``'s, so it only says where to look: no attributes,
    clean text, defaults that are boundary values, annotations that are boundary types, and closure
    cells holding ``object``, the frozen error, an approved factory or a class that binds this very
    function. A string annotation is text; the field-factory sentinel, and the defaults and factories
    of the dataclass that binds it, are its own (each field reads them).
    """
    sentinel = getattr(dataclasses, "_HAS_DEFAULT_FACTORY", None)
    fields = _owner_fields(fn)  # a field's own default or factory is read with the field

    def given(v: object) -> bool:
        return v is sentinel or any(v is f for f in fields)

    cells = [cell.cell_contents for cell in fn.__closure__ or ()]
    cells_ok = all(
        given(c) or c is object or c is dataclasses.FrozenInstanceError or _approved(c)
        or (isinstance(c, type) and _binds(c, fn))
        for c in cells
    )  # fmt: skip
    return not _stored(fn) and cells_ok and _plain_state(fn, given)


def _plain_state(fn: types.FunctionType, given: typing.Callable[[object], bool] = lambda _: False) -> bool:
    """A generated function's text, defaults and annotations: what any function object hands out.

    Text is a str or None, each default a boundary value (or ``given``, what its tooling put
    there), each annotation keyed by a str and holding text, None or a boundary type.
    """
    defaults_ok = all(given(v) or _value_problem(v) is None for _, v in _held_defaults(fn))
    annotations_ok = all(
        type(k) is str and (v is None or type(v) is str or _type_problem(v) is None)
        for k, v in fn.__annotations__.items()
    )
    return _text_problem(fn) is None and defaults_ok and annotations_ok


def _owner_fields(fn: types.FunctionType) -> list[object]:
    """The defaults and factories of the dataclass that binds ``fn``, found by name and checked by identity."""
    owner: object = sys.modules.get(fn.__module__)
    for part in fn.__qualname__.split(".")[:-1]:
        owner = getattr(owner, part, None)
    if not (isinstance(owner, type) and dataclasses.is_dataclass(owner) and _binds(owner, fn)):
        return []
    return [v for f in dataclasses.fields(owner) for v in (f.default, f.default_factory)]


def _tooling_closure(fn: types.FunctionType) -> bool:
    """Whether a tooling closure holds only what the tooling puts there.

    That is a generated method it wraps (kept as ``__wrapped__`` too), a field getter, a class
    that binds this very function or a boundary value; anything else makes it a hand-written method.
    """
    cells = dict(zip(fn.__code__.co_freevars, [cell.cell_contents for cell in fn.__closure__ or ()], strict=True))
    held = [
        c
        for c in cells.values()
        if not (
            (inspect.isfunction(c) and _generated(c))
            or type(c) is operator.itemgetter
            or (isinstance(c, type) and _binds(c, fn))
            or _value_problem(c) is None  # a boundary value: the repr guard's set of ids in progress
        )
    ]
    stored = _stored(fn)
    wraps = cells.get("user_function")
    wrapped = stored.keys() <= {"__wrapped__"} and stored.get("__wrapped__", wraps) is wraps
    return not held and wrapped and _plain_state(fn)
