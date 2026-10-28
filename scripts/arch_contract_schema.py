"""What a module contract may declare (ADR-NEW-module-contracts), checked on the live objects.

`scripts.arch_surface` holds a contract to its imports; this holds it to its declarations.
Every class a contract module defines is one of:

- a Pydantic model whose config is ``frozen=True`` and ``extra="forbid"``;
- a frozen dataclass (an immutable value type a TypeAdapter can still serialize);
- a ``typing.Protocol`` (the service interface);
- an exception with a typed ``__init__`` of its own or a contract base's (BaseException's
  ``*args`` take any object), which sets on the error only attributes it annotates;
- an ``enum.Enum`` that declares only members, each a plain str or int.

A Protocol method or attribute, a public function, and every field of a contract model or
dataclass carry only boundary types: None, bool, int, float, str, bytes,
``pathlib.Path``, dates and times, ``Literal``, ``pydantic.JsonValue``, unions and the
standard containers of these, and frozen models, frozen dataclasses and enums declared in a
contract. The same holds for a value type's public methods and properties, inherited from a
contract base or not, for a private value type a public one reaches, for a type alias the
contract exports, and for an exported constant's value (a subclass of a scalar or container
is no scalar or container). A contract re-exports no foreign module, no foreign class other
than an exception or enum, and no untyped callable. An open database connection, a file
handle, ``Any`` or a missing annotation is a finding, because nothing on the far side of a
process boundary can receive one.

Anything else a contract could carry is refused outright rather than read: a custom
metaclass, a module ``__getattr__``, a hand-written special method beyond the inert ones, a
class attribute that is no field, method or property (a ClassVar, an InitVar, a cached
property), attributes stored on a function, and an exception instance held as a constant.
No real contract needs one; a reader for a construct is added when one does.

The contract package also exports ``CONTRACT_VERSION = "<pkg>/<n>"``. A plain or mutable
class, a mutable dataclass, or a model left on Pydantic's defaults is a finding, because
later TypeScript and Rust generation relies on the models being closed and immutable.
"""

from __future__ import annotations

import abc
import ast
import builtins
import collections.abc
import contextvars
import dataclasses
import datetime
import enum
import functools
import importlib
import inspect
import operator
import pathlib
import re
import sys
import textwrap
import types
import typing
from types import ModuleType

from pydantic import BaseModel, ConfigDict, JsonValue, computed_field
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined

from scripts.arch_surface import CONTAINER, CONTRACT, NON_SOURCE_SUFFIXES


def contract_modules(pkg: str, container: str = CONTAINER) -> list[ModuleType]:
    """``apps.<pkg>.contract`` and every submodule of it, imported."""
    root = importlib.import_module(f"{container}.{pkg}.{CONTRACT}")
    return [root, *(importlib.import_module(name) for name in _submodule_names(root))]


def _submodule_names(root: ModuleType) -> list[str]:
    """Every module importable below ``root``, from its files.

    ``pkgutil.walk_packages`` skips a directory with no ``__init__.py``, yet Python imports
    its modules as a namespace package all the same, so the tree itself is read.
    """
    names: set[str] = set()
    for base in map(pathlib.Path, getattr(root, "__path__", [])):
        for path in base.rglob("*.py"):
            parts = path.relative_to(base).with_suffix("").parts
            parts = parts[:-1] if parts[-1] == "__init__" else parts
            # every directory on the way is a package (regular or namespace) Python imports
            names.update(".".join([root.__name__, *parts[:i]]) for i in range(1, len(parts) + 1))
    return sorted(names)


def _inherited_base(cls: type) -> type | None:
    """A base that is neither a contract's, the language's nor what a contract is written with."""
    for k in cls.__mro__[1:]:
        if k is object or _tooling(k) or _is_contract_module(k.__module__):
            continue
        if issubclass(cls, BaseException) and issubclass(k, BaseException):
            continue  # a contract error may refine a stdlib one: it carries no members of its own
        if issubclass(cls, enum.Enum) and k in _SCALARS:
            continue  # a str or int enum: each member is still one of its declared values
        return k  # a list or dict base, say, hands its whole mutable API across
    return None


def _metaclass_problem(cls: type) -> str | None:
    """A metaclass's own members reach the class, and the MRO never shows them."""
    meta = type(cls)
    if meta is type or _tooling(meta):  # by identity: a metaclass can name builtins as its home
        return None
    return (
        f"uses the metaclass {meta.__module__}.{meta.__qualname__}, which is neither the language's nor the tooling's"
    )


def _class_problem(cls: type) -> str | None:
    base = _inherited_base(cls)
    if base is not None:  # its inherited API would cross the boundary unread
        return f"inherits {base.__module__}.{base.__qualname__}, which is not a contract type"
    meta_problem = _metaclass_problem(cls) or _text_problem(cls)
    if meta_problem:
        return meta_problem
    if issubclass(cls, BaseModel):
        return _model_problem(cls)
    if dataclasses.is_dataclass(cls):
        return _dataclass_problem(cls)
    if issubclass(cls, enum.Enum):
        return _enum_problem(cls)
    if getattr(cls, "_is_protocol", False) or issubclass(cls, BaseException):
        return None
    return "a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums"


def _model_problem(cls: type[BaseModel]) -> str | None:
    config = cls.model_config
    if config.get("frozen") is not True or config.get("extra") != "forbid":
        return 'a Pydantic model needs model_config = ConfigDict(frozen=True, extra="forbid")'
    if cls.__private_attributes__:  # every instance carries them, and frozen=True does not freeze them
        return f"a Pydantic model declares private attributes ({', '.join(sorted(cls.__private_attributes__))})"
    held = _value_problem(dict(config))  # public on the class, so every setting is a value it hands out
    if held:
        return f"model_config {held}"
    computed = (_computed_field_problem(cls, name, info) for name, info in cls.model_computed_fields.items())
    return next(filter(None, computed), None)


def _computed_field_problem(cls: type[BaseModel], name: str, info: ComputedFieldInfo) -> str | None:
    """``model_computed_fields`` hands out each computed field's settings; its getter is read as a property.

    The property it keeps must be the one the class binds, which the member pass reads.
    """
    if info.wrapped_property is not inspect.getattr_static(cls, name, None):
        return f"computed field {name} keeps a property other than the one its class declares"
    if info.return_type is not PydanticUndefined:
        typed = _type_problem(info.return_type)
        if typed:
            return f"computed field {name}: return_type {typed}"
    settings = {f.name: getattr(info, f.name) for f in dataclasses.fields(info)}
    held = _value_problem({k: v for k, v in settings.items() if k not in ("wrapped_property", "return_type")})
    return f"computed field {name} {held}" if held else None


def _dataclass_problem(cls: type) -> str | None:
    # Read off the class itself: an undecorated subclass inherits frozen=True, yet the
    # generated __setattr__ freezes only the decorated class, so its instances take new names.
    params = vars(cls).get("__dataclass_params__")
    if params is None:
        return "subclasses a dataclass without its own @dataclass(frozen=True), so its instances take new attributes"
    # The class hands its parameters out, so each must still be the bool the decorator stored.
    kind = type(vars(_PlainRecord)["__dataclass_params__"])
    if type(params) is not kind:
        return f"holds a {type(params).__qualname__} as its dataclass parameters"
    held = {name: getattr(params, name, None) for name in kind.__slots__}
    odd = next((name for name, value in held.items() if type(value) is not bool), None)
    if odd is not None:
        return (
            f"its dataclass parameter {odd} holds a {type(held[odd]).__qualname__}, where the decorator stores a bool"
        )
    return None if params.frozen else "a dataclass needs frozen=True (or make it a frozen Pydantic model)"


# What the enum machinery itself stores on every member, read off a plain one.
class _Plain(enum.Enum):
    A = "a"


_MEMBER_KEYS = frozenset(vars(_Plain.A))


def _machinery_intact(cls: type[enum.Enum], member: enum.Enum) -> bool:
    """The machinery's own keys still hold boundary values: ``.name`` hands ``_name_`` out."""
    state = vars(member)
    held = (_value_problem(value) for key, value in state.items() if key != "__objclass__")
    return state.get("__objclass__") is cls and not any(held)


class _PlainStr(enum.StrEnum):
    A = "a"


# What the enum machinery may hold on a str, int or plain enum: functions and types by identity.
_ENUM_TOOLING: dict[str, tuple[object, ...]] = {
    "_generate_next_value_": tuple(vars(k)["_generate_next_value_"] for k in (_Plain, _PlainStr)),
    "_new_member_": (object.__new__, str.__new__, int.__new__, vars(_PlainStr)["_new_member_"]),
    "_member_type_": (object, str, int),
    "_value_repr_": (None, str.__repr__, int.__repr__),
    "_use_args_": (True, False),
}


def _enum_machinery_intact(cls: type[enum.Enum]) -> bool:
    """Every machinery binding on an enum holds what the machinery puts there.

    ``Mode.NAME`` and ``Mode(value)`` read the member maps, and every other list, dict and
    hook the machinery binds is as readable on the exported class, so none may hold more.
    """
    machinery = {name: value for name, value in vars(cls).items() if name in _ENUM_MACHINERY}
    members = list(cls)
    registered = {id(m) for m in members}  # aliases map to these too; anything else iteration never shows
    names = machinery.get("_member_names_")
    by_name = machinery.get("_member_map_")
    by_value = machinery.get("_value2member_map_")
    structures = (
        type(names) is list
        and len(names) == len(members)
        and all(n is m._name_ for n, m in zip(names, members, strict=True)),
        type(by_name) is dict and all(type(n) is str and id(m) in registered for n, m in by_name.items()),
        type(by_value) is dict
        and all(type(v) in (str, int) and id(m) in registered and m.value == v for v, m in by_value.items()),
        type(machinery.get("_unhashable_values_")) is list and not machinery["_unhashable_values_"],
    )
    held = {"_member_names_", "_member_map_", "_value2member_map_", "_unhashable_values_"}
    hooks = all(
        name in held or any(value is approved for approved in _ENUM_TOOLING.get(name, ()))
        for name, value in machinery.items()
    )
    return all(structures) and hooks


def _enum_problem(cls: type[enum.Enum]) -> str | None:
    """An enum's members are its whole surface: a method, or an ``__init__`` storing attributes, is a second one."""
    # A member is an object too: whatever is stored on it after the class is built is surface.
    plain = all(
        type(member.value) in (str, int) and set(vars(member)) <= _MEMBER_KEYS and _machinery_intact(cls, member)
        for member in cls
    ) and _enum_machinery_intact(cls)
    bases = _own_bases(cls)
    bound = ((name, item) for k in bases for name, item in vars(k).items() if name not in cls.__members__)
    extra = any(_surface_member(name, item, cls) for name, item in bound) or any(map(inspect.get_annotations, bases))
    return None if plain and not extra else "is an enum with more than plain str or int members"


_SCALARS: frozenset[object] = frozenset(
    {
        type(None), bool, int, float, str, bytes, pathlib.Path, pathlib.PurePath,
        datetime.datetime, datetime.date, datetime.time, datetime.timedelta,
    }
)  # fmt: skip
_CONTAINERS: frozenset[object] = frozenset(
    {
        list, tuple, dict, set, frozenset, collections.abc.Sequence, collections.abc.Mapping,
        collections.abc.Set, collections.abc.Iterable, collections.abc.Iterator,
    }
)  # fmt: skip
_SCALAR_VALUES = tuple(t for t in _SCALARS if isinstance(t, type))
# The container being checked: ``apps`` on the tree, a fixture package in the tests.
_CONTAINER: contextvars.ContextVar[str] = contextvars.ContextVar("contract_container", default=CONTAINER)


def _behind_a_contract(name: str) -> bool:
    """A module of the container that is no contract: a package root or its implementation."""
    container = _CONTAINER.get()
    return (name == container or name.startswith(container + ".")) and not _is_contract_module(name)


def _is_contract_module(name: str) -> bool:
    """``<container>.<pkg>.contract`` or a submodule of it."""
    prefix = _CONTAINER.get() + "."
    parts = name[len(prefix) :].split(".") if name.startswith(prefix) else []
    return len(parts) >= 2 and parts[1] == CONTRACT


_TOOLING = re.compile(r"^(__future__|typing|typing_extensions|dataclasses|enum|abc|annotated_types|pydantic)(\.|$)")


def _tooling(obj: object) -> bool:
    """What a contract is written with, by identity: ``__module__`` is writable, so a class that
    names a tooling module is trusted only when that module really holds it under its name."""
    origin = getattr(obj, "__module__", None)
    if not (isinstance(origin, str) and _TOOLING.match(origin)):
        return False
    found: object = sys.modules.get(origin)
    path = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)  # a TypeAliasType has no qualname
    for part in str(path).split("."):
        found = getattr(found, part, None)
    return found is obj


def _type_problem(tp: object, seen: frozenset[type] = frozenset()) -> str | None:
    """Why ``tp`` cannot cross a module boundary, or None when it can.

    A private contract value type is not on any surface, so its fields are read here, once.
    """
    if tp is None or tp in _SCALARS or tp is JsonValue:
        return None
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:  # the metadata stays on the live annotation, so it is a value too
        base, *metadata = typing.get_args(tp)
        return _type_problem(base, seen) or _metadata_problem(metadata)
    if origin is typing.Literal:  # its arguments are values: `Literal[_Mode.CONN]` carries a connection
        return next((p for p in map(_value_problem, typing.get_args(tp)) if p), None)
    # A union's members, a container's elements, and what a ClassVar or Final qualifier wraps.
    if origin in (typing.Union, types.UnionType, typing.ClassVar, typing.Final) or origin in _CONTAINERS:
        args = [a for a in typing.get_args(tp) if a is not Ellipsis]
        bare = origin in _CONTAINERS and not args and tp != tuple[()]  # bare `typing.List` is list[Any]
        found = [f"{tp!s} has no element type"] if bare else [_type_problem(a, seen) for a in args]
        return next((p for p in found if p), None)
    if isinstance(tp, type) and _is_contract_module(tp.__module__):
        return _contract_type_problem(tp, seen)
    return f"{getattr(tp, '__qualname__', tp)!s} is not a boundary type"


# The containers whose instances cannot change: a field holding any other one (``list``, a dict, or
# a ``Sequence`` or ``Mapping``, which Pydantic and dataclasses keep as the list or dict they were
# handed) can be changed in place after validation, however frozen the type that holds it.
_FROZEN_CONTAINERS: frozenset[object] = frozenset({tuple, frozenset})


def _mutable_problem(tp: object) -> str | None:
    """Why a field annotated ``tp`` could hold a value its holder's ``frozen=True`` cannot freeze."""
    if tp is JsonValue:  # an alias Pydantic expands at runtime, to lists and dicts among the rest
        return "JsonValue can hold a list or dict, which can be changed in place; a contract field holds a tuple or frozenset"
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:
        return _mutable_problem(typing.get_args(tp)[0])
    if origin in (typing.Union, types.UnionType) or origin in _FROZEN_CONTAINERS:
        return next(filter(None, map(_mutable_problem, typing.get_args(tp))), None)
    if origin in _CONTAINERS:  # a bare container is refused as having no element type
        return f"{tp!s} can be changed in place; a contract field holds a tuple or frozenset"
    return None


def _contract_type_problem(tp: type, seen: frozenset[type]) -> str | None:
    """A type declared in a contract: a closed value type, read here when no surface pass reads it."""
    if getattr(tp, "_is_protocol", False) or issubclass(tp, BaseException):
        return f"{tp.__qualname__} is not a value type"
    if _class_problem(tp):
        return f"{tp.__qualname__} is not a closed frozen type"
    if not _on_surface(tp) and tp not in seen:
        found = _class_problems(tp, seen | {tp})
        return next((f"{tp.__qualname__}{_joined(p)}" for p in found), None)
    return None


def _metadata_problem(metadata: typing.Iterable[object]) -> str | None:
    """``Annotated`` metadata: the tooling's constraints (``Field``, ``annotated_types``) or plain values."""
    found = (_metadata_item_problem(item) for item in metadata)
    return next((f"Annotated metadata {p}" for p in found if p), None)


def _field_info_problem(info: FieldInfo, *, defaults: bool) -> str | None:
    """What a ``Field()`` holds, not Pydantic's own bookkeeping; its constraints live in ``.metadata``.

    A model field's default is read with the field's (``_field_defaults``), so ``defaults=False`` there.
    """
    skipped = {"annotation", "default_factory", "metadata"}
    if not defaults:
        skipped.add("default")
    # The slots as they stand, not ``_attributes_set``: that records only what the constructor was given.
    slots = (k for k in FieldInfo.__slots__ if not k.startswith("_") and k not in skipped and hasattr(info, k))
    state = {k: v for k in slots if (v := getattr(info, k)) is not PydanticUndefined}
    factory = info.default_factory
    held = _FACTORY if defaults and factory is not None and not _approved(factory) else _value_problem(state)
    held = held or next(filter(None, map(_metadata_item_problem, info.metadata)), None)
    return held or _bookkeeping_problem(info, skipped, defaults=defaults)


def _bookkeeping_problem(info: FieldInfo, skipped: set[str], *, defaults: bool) -> str | None:
    """Pydantic's private slots, which ``model_fields`` hands out as readily as the public ones.

    Each holds what the tooling puts there: the constructor's record, flags, qualifier names, and
    the original annotation and assignment, which are the field's own or read like them.
    """
    recorded = getattr(info, "_attributes_set", {})
    if type(recorded) is not dict:
        return f"_attributes_set holds a {type(recorded).__qualname__}, where Pydantic keeps a dict"
    keys = (k for k in recorded if type(k) is not str)
    if (key := next(keys, None)) is not None:
        return f"_attributes_set has a {type(key).__qualname__} key, where Pydantic names a setting by text"
    state: dict[str, object] = {f"_attributes_set[{k!r}]": v for k, v in recorded.items() if k not in skipped}
    for name in ("_qualifiers", "_complete", "_final"):
        state[name] = getattr(info, name, None)
    for k in skipped:
        if k in recorded and recorded[k] is not getattr(info, k):
            state[f"_attributes_set[{k!r}]"] = _annotation_held(recorded[k]) if k == "annotation" else recorded[k]
    original = getattr(info, "_original_annotation", PydanticUndefined)
    if original is not info.annotation:
        state["_original_annotation"] = _annotation_held(original)
    assigned = getattr(info, "_original_assignment", PydanticUndefined)
    if isinstance(assigned, FieldInfo) and _tooling(type(assigned)):
        problem = _field_info_problem(assigned, defaults=defaults)
        if problem:
            return f"_original_assignment {problem}"
    elif assigned is not info.default:
        state["_original_assignment"] = assigned
    return _value_problem({k: v for k, v in state.items() if v is not PydanticUndefined})


def _annotation_held(annotation: object) -> object:
    """An annotation as a value: text, or None when it is a boundary type, else the object itself."""
    if isinstance(annotation, str) or annotation is PydanticUndefined:
        return None
    return None if _type_problem(annotation) is None else annotation


def _metadata_item_problem(item: object) -> str | None:
    """A plain value, or a tooling object read through the state it keeps (it hands that out too)."""
    if not _tooling(type(item)):
        return _value_problem(item)
    if isinstance(item, FieldInfo):
        return _field_info_problem(item, defaults=True)
    slots = (name for k in type(item).__mro__ for name in getattr(k, "__slots__", ()))
    state = {name: getattr(item, name) for name in slots if hasattr(item, name)}
    state.update(getattr(item, "__dict__", {}))
    return _value_problem(state)


def _on_surface(cls: type) -> bool:
    """A public class bound under its own name in its contract module gets its own surface pass.

    A private or nested one (``Outer.Payload``) does not, so whoever references it reads it.
    """
    module = sys.modules.get(cls.__module__)
    return not cls.__name__.startswith("_") and getattr(module, cls.__name__, None) is cls


def _joined(suffix: str, verb: str = "has") -> str:
    """A `_class_problems` suffix (": problem" or ".member...") as it reads after a type's name."""
    return suffix if suffix[0] == "." else f" {verb} {suffix[2:]}"


def _own_bases(cls: type) -> list[type]:
    """``cls`` and every base it inherits members from, bar the language's and the tooling's."""
    return [k for k in cls.__mro__ if not _builtin(k) and not _tooling(k)]


def _builtin(cls: type) -> bool:
    """A class of the language, by identity: a ``__module__`` of ``builtins`` can be written onto any class."""
    return vars(builtins).get(cls.__name__) is cls


def _contract_bases(cls: type) -> list[type]:
    """``cls`` and the bases it inherits members from that a contract declares (not Protocol or BaseModel)."""
    return [k for k in cls.__mro__ if _is_contract_module(k.__module__)]


def _attribute_problems(cls: type, bases: list[type], seen: frozenset[type]) -> list[str]:
    """A Protocol's annotated attributes."""
    try:
        attrs = typing.get_type_hints(cls, include_extras=True)
    except Exception as exc:  # an unresolvable annotation is itself the finding
        return [f"cannot resolve its attribute annotations ({type(exc).__name__}: {exc})"]
    names = dict.fromkeys(name for k in bases for name in inspect.get_annotations(k))
    found = {name: _type_problem(attrs[name], seen) for name in names}
    return [f"{name}: {problem}" for name, problem in found.items() if problem]


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


_REFUSED = "a contract declares only fields, methods and properties on a class"


def _member_problems(
    cls: type, *, attributes: bool, seen: frozenset[type] = frozenset(), bases: list[type] | None = None
) -> list[str]:
    """A class's public methods and properties, inherited ones included; any other binding is refused.

    For a Protocol its annotated attributes are read too; a value type's annotations are its
    fields, read apart, so one that is no field (a ClassVar, an InitVar) is refused. An
    error's own ``__init__`` is read: it is how the error is built. ``bases`` defaults to the
    classes in its MRO that a contract declares.
    """
    bases = _contract_bases(cls) if bases is None else bases
    # A key that is not a name is reported with the map itself (`_tooling_map_problem`).
    annotated = dict.fromkeys(name for k in bases for name in inspect.get_annotations(k) if type(name) is str)
    declared: dict[str, object] = {}
    if attributes:
        problems = _attribute_problems(cls, bases, seen)
    else:
        fields = _field_names(cls)
        problems = [f"{name}: is annotated but no field; {_REFUSED}" for name in annotated if name not in fields]
        declared = _declared_defaults(cls)
    members: dict[str, object] = {}
    for k in bases:
        for name, member in vars(k).items():
            members.setdefault(name, member)  # the nearest definition wins, as attribute lookup does
    problems.extend(_shadow_problems(members, declared))
    for name, member in members.items():
        if name == "__init__" and issubclass(cls, BaseException) and inspect.isfunction(member):
            problems.extend(_function_problems(name, member, seen, bound=True))
            # Sol, PR #6182: its signature is typed, but what it stores on the error is read too.
            problems.extend(_instance_state_problems(name, member, set(annotated), "annotated attribute"))
        elif not _surface_member(name, member, cls) or (name in annotated and not attributes) or name == "model_config":
            # private, inert, a field's default (read with the field), or the model's config;
            # an inert method a contract wrote is still read for what it holds
            problems.extend(_inert_problems(name, member))
            problems.extend(_post_init_problems(member, _field_names(cls)) if name == "__post_init__" else [])
        elif name in annotated:  # a Protocol's or an error's attribute: its annotation is read, its value is not
            problems.append(f"{name}: binds a value to an annotated attribute; {_REFUSED}")
        elif name.startswith("__") and inspect.isfunction(_unwrapped(member)):
            problems.append(f"{name}(): is a hand-written special method; {_REFUSED}")
        elif type(member) is property:
            problems.extend(_property_problems(name, member, seen))
        elif isinstance(member, (classmethod, staticmethod)):
            problems.extend(_function_problems(name, member.__func__, seen, bound=isinstance(member, classmethod)))
        elif inspect.isfunction(member):
            problems.extend(_function_problems(name, member, seen, bound=True))
        else:  # a class attribute, a nested class, a cached property, a partial, a rekeyed tooling map
            keyed = _tooling_map_problem(name, member, cls)
            problems.append(f"{name}: {keyed or f'binds a {type(member).__qualname__}; {_REFUSED}'}")
    return problems


def _property_problems(name: str, member: property, seen: frozenset[type]) -> list[str]:
    """A property's accessors, each a function read like a method, and the docstring it keeps."""
    accessors = {name: member.fget, f"{name}.setter": member.fset, f"{name}.deleter": member.fdel}
    problems = []
    for label, fn in accessors.items():
        if inspect.isfunction(fn):
            problems.extend(_function_problems(label, fn, seen, bound=True))
        elif fn is not None:
            problems.append(f"{label}: binds a {type(fn).__qualname__} as an accessor; {_REFUSED}")
    held = None if member.__doc__ is None else _value_problem(member.__doc__)
    return [*problems, *([f"{name}: its docstring {held}"] if held else [])]


def _shadow_problems(members: dict[str, object], declared: dict[str, object]) -> list[str]:
    """A binding over a field's name that is not the field's default: what ``Child.name`` hands out.

    A slotted dataclass binds its own slot descriptor there, which hands out nothing.
    """
    shadows = {
        name: _value_problem(member)
        for name, member in members.items()
        if name in declared and member is not declared[name] and not isinstance(member, types.MemberDescriptorType)
    }
    return [f"{name}: shadows its field's default and {held}" for name, held in shadows.items() if held]


def _function_problems(
    name: str, fn: typing.Callable[..., object], seen: frozenset[type] = frozenset(), *, bound: bool = False
) -> list[str]:
    """A Protocol method's or a contract function's parameters and return, as boundary types.

    ``bound`` drops the first parameter, the receiver a method or classmethod is called on,
    whatever it is named; a function or staticmethod has none, so every parameter is read.
    """
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception as exc:  # an unresolvable annotation is itself the finding
        return [f"{name}(): cannot resolve its annotations ({type(exc).__name__}: {exc})"]
    problems: list[str] = []
    parameters = list(inspect.signature(fn).parameters.items())
    signature = dict(parameters[1 if bound else 0 :])
    for receiver, _ in parameters[:1] if bound else []:  # its call shape is the language's, what it holds is not
        # A string annotation is text; anything else is an object held for whoever reads __annotations__.
        annotations = getattr(fn, "__annotations__", {})
        held = _value_problem(annotations[receiver]) if receiver in annotations else None
        problems.extend([f"{name}(): {receiver}: annotation {held}"] if held else [])
    for param in [*signature, "return"]:
        if param not in hints:
            problems.append(f"{name}(): {param} has no annotation")
            continue
        problem = _type_problem(hints[param], seen)
        if problem:
            problems.append(f"{name}(): {param}: {problem}")
    # An annotation that names no parameter is held by the function and handed out as __annotations__.
    named = {k for k in getattr(fn, "__annotations__", {}) if type(k) is str}  # other keys: _retained_problems
    stray = sorted(named - {*inspect.signature(fn).parameters, "return"})
    problems.extend([f"{name}(): annotates {', '.join(stray)}, naming no parameter"] if stray else [])
    return [*problems, *_retained_problems(name, fn)]


def _retained_problems(name: str, fn: typing.Callable[..., object]) -> list[str]:
    """What a function object holds beyond its code: anyone holding the function can read it.

    That is its docstring and module name, attributes set on it, and its closure's cells. A
    ``__class__`` cell (what ``super()`` reads) passes only when it holds a class that binds
    this very function.
    """
    text = _text_problem(fn)
    problems = [f"{name}(): {text}"] if text else []
    stored = sorted(_stored(fn))
    problems.extend([f"{name}(): stores {', '.join(stored)} on the function; {_REFUSED}"] if stored else [])
    for label, value in _held_defaults(fn):  # a default is a value the contract holds and hands out
        held = _value_problem(value)
        problems.extend([f"{name}(): {label}: default {held}"] if held else [])
    for key in getattr(fn, "__annotations__", {}):  # only a str names a parameter; any other key is held
        held = None if type(key) is str else _value_problem(key)
        problems.extend([f"{name}(): an __annotations__ key {held}"] if held else [])
    code = getattr(fn, "__code__", None)
    cells = zip(code.co_freevars, getattr(fn, "__closure__", None) or (), strict=True) if code is not None else ()
    for var, cell in cells:
        try:
            held = cell.cell_contents
        except ValueError:  # an empty cell holds nothing
            continue
        if var == "__class__" and isinstance(held, type) and _binds(held, fn):
            continue
        problem = _value_problem(held)
        problems.extend([f"{name}(): its closure's {var} {problem}"] if problem else [])
    return problems


def _binds(cls: type, fn: object) -> bool:
    """Whether ``cls`` itself binds ``fn``, directly or as a classmethod's or staticmethod's function."""
    return any(_unwrapped(m) is fn for m in vars(cls).values())


def _inert_problems(name: str, member: object) -> list[str]:
    """An inert special method's signature is the interpreter's; what it retains is still read."""
    fn = _unwrapped(member)
    if name not in _INERT_SPECIAL_METHODS or not inspect.isfunction(fn) or _generated(fn):
        return []
    # Its annotations are read raw: a string is text, anything else must be a boundary type.
    held = {k: v for k, v in fn.__annotations__.items() if not (v is None or type(v) is str) and _type_problem(v)}
    return [
        *(
            f"{name}(): {k}: annotation holds a {type(v).__qualname__}, which is not a boundary type"
            for k, v in held.items()
        ),
        *_retained_problems(name, fn),
    ]


def _post_init_problems(member: object, fields: set[str]) -> list[str]:
    """A hand-written ``__post_init__`` may only reset declared fields, so no instance carries more."""
    return _instance_state_problems("__post_init__", member, fields, "field")


def _instance_state_problems(label: str, member: object, fields: set[str], kind: str) -> list[str]:
    """What a hand-written ``__post_init__``, or an error's ``__init__``, does with its instance:
    it may only set the names its type declares (a ``kind``), so no instance carries more.

    Read from its source, failing closed: ``self`` may appear only as ``self.<name>`` or as the
    first argument of ``object.__setattr__(self, "<name>", ...)``. Any other use (an undeclared
    name, a computed one, ``self`` handed to a call or rebound) could attach undeclared state.
    ``super()`` names no instance, so ``super().__init__(message)`` hands on only what it is passed.
    """
    fn = _unwrapped(member)
    if not inspect.isfunction(fn) or _generated(fn):
        return []
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError) as exc:
        return [f"{label}(): its source cannot be read ({type(exc).__name__}); {_REFUSED}"]
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.args.args:
        return [f"{label}(): takes no instance to read; {_REFUSED}"]
    me = node.args.args[0].arg
    allowed: set[int] = set()
    sets: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == me:
            allowed.add(id(sub.value))
            sets.extend([] if sub.attr in fields else [sub.attr])
        elif (
            isinstance(sub, ast.Call)
            and ast.unparse(sub.func) == "object.__setattr__"
            and sub.args
            and isinstance(sub.args[0], ast.Name)
            and sub.args[0].id == me
        ):
            allowed.add(id(sub.args[0]))
            target = sub.args[1] if len(sub.args) > 1 else None
            text = target.value if isinstance(target, ast.Constant) and type(target.value) is str else None
            named = text or (f"a computed name {ast.unparse(target)}" if target else "nothing")
            sets.extend([] if text in fields else [named])
    loose = any(isinstance(sub, ast.Name) and sub.id == me and id(sub) not in allowed for sub in ast.walk(node))
    problems = [f"{label}(): sets {', '.join(sorted(set(sets)))}, which no {kind} declares; {_REFUSED}"] if sets else []
    return [*problems, *([f"{label}(): hands its instance on; {_REFUSED}"] if loose else [])]


def _held_defaults(fn: typing.Callable[..., object]) -> list[tuple[str, object]]:
    """Every default a function stores, raw: ``__kwdefaults__`` can hold a key no parameter takes.

    Each is labeled with the parameter it serves, or with where it sits when it serves none.
    """
    code = getattr(fn, "__code__", None)
    if code is None:
        return []
    positional = code.co_varnames[: code.co_argcount]
    keyword = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
    defaults = tuple(getattr(fn, "__defaults__", None) or ())
    start = len(positional) - len(defaults)
    held = [(positional[start + i] if start + i >= 0 else f"__defaults__[{i}]", v) for i, v in enumerate(defaults)]
    kwdefaults = list((getattr(fn, "__kwdefaults__", None) or {}).items())
    named = [(k if k in keyword else f"__kwdefaults__[{k!r}]", v) for k, v in kwdefaults if type(k) is str]
    # A key is held too, and only a str can name a parameter: any other key is read as the value it is.
    keys = [(f"a __kwdefaults__ key of type {type(k).__qualname__}", k) for k, _ in kwdefaults if type(k) is not str]
    values = [(f"__kwdefaults__ under a {type(k).__qualname__} key", v) for k, v in kwdefaults if type(k) is not str]
    return [*held, *named, *keys, *values]


def _stored(obj: object) -> dict[str, object]:
    """Every attribute set on a function: a private or dunder name hands its value out as well."""
    return dict(vars(obj))


def _text_problem(owner: object) -> str | None:
    """A docstring or module name kept on a function or class: anyone can read it, so it is text."""
    for name in ("__doc__", "__module__"):
        value = vars(owner).get(name) if isinstance(owner, type) else getattr(owner, name, None)
        if not (value is None or type(value) is str):
            return f"its {name} holds a {type(value).__qualname__}, where text belongs"
    return None


def _field_names(cls: type) -> set[str]:
    if issubclass(cls, BaseModel):
        return set(cls.model_fields)
    if dataclasses.is_dataclass(cls):
        return {f.name for f in dataclasses.fields(cls)}
    return set()


_FIELD_FLAGS = ("repr", "hash", "init", "compare", "kw_only")


def _declared_annotation(cls: type, name: str) -> object:
    """The annotation a class body wrote for ``name``, from the nearest class that wrote one."""
    for k in cls.__mro__:
        written = vars(k).get("__annotations__", {})
        if name in written:
            return written[name]
    return dataclasses.MISSING


def _dataclass_field_problem(cls: type, f: dataclasses.Field[object]) -> str | None:
    """What ``dataclasses.fields()`` hands out besides the default: its type, flags and metadata."""
    if f.type is not _declared_annotation(cls, f.name):  # the annotation is read; the Field must hold it
        return f"its retained type is a {type(f.type).__qualname__}, not the annotation its class wrote"
    for flag in _FIELD_FLAGS:
        value = getattr(f, flag)
        if not any(value is allowed for allowed in (True, False, None, dataclasses.MISSING)):
            return f"its {flag} flag holds a {type(value).__qualname__}, where a bool belongs"
    held = _value_problem(dict(f.metadata))
    doc = getattr(f, "doc", None)  # Python 3.13 keeps field(doc=...) too
    doc_held = None if doc is None else _value_problem(doc)
    return (held and f"metadata {held}") or (doc_held and f"its doc {doc_held}")


def _model_field_names_problem(cls: type[BaseModel]) -> str | None:
    """``model_fields`` is handed out whole, keys included: each names a field some model class declares."""
    declared = {n for k in cls.__mro__ if issubclass(k, BaseModel) for n in inspect.get_annotations(k)}
    for key in cls.model_fields:
        if type(key) is not str:
            continue  # reported with the map it is in, `__pydantic_fields__` (`_tooling_map_problem`)
        if key not in declared:
            return f"model_fields names {key!r}, a field no model class in its MRO declares"
    return None


def _field_problems(cls: type, seen: frozenset[type] = frozenset()) -> list[str]:
    if issubclass(cls, BaseModel):
        if problem := _model_field_names_problem(cls):
            return [problem]
        fields = {name: info.annotation for name, info in cls.model_fields.items()}
    elif dataclasses.is_dataclass(cls):
        try:
            hints = typing.get_type_hints(cls, include_extras=True)
        except Exception as exc:  # an unresolvable annotation is itself the finding
            return [f"cannot resolve its field annotations ({type(exc).__name__}: {exc})"]
        fields = {f.name: hints.get(f.name) for f in dataclasses.fields(cls)}
    else:
        return []
    found = {name: _type_problem(tp, seen | {cls}) or _mutable_problem(tp) for name, tp in fields.items()}
    if dataclasses.is_dataclass(cls):  # field(metadata=...) is handed out by dataclasses.fields()
        kept = {f.name: _dataclass_field_problem(cls, f) for f in dataclasses.fields(cls)}
        found = {name: found[name] or kept[name] for name in found}
    if issubclass(cls, BaseModel):  # Pydantic moves Annotated metadata and Field() off the annotation
        held = {name: _field_info_problem(info, defaults=False) for name, info in cls.model_fields.items()}
        held = {name: problem and f"Annotated metadata {problem}" for name, problem in held.items()}
        found = {name: found[name] or held[name] for name in found}
    defaults = {
        name: _FACTORY if value is _FACTORY else _value_problem(value) for name, value in _field_defaults(cls).items()
    }
    return [
        *(f"field {name}: {problem}" for name, problem in found.items() if problem),
        *(f"field {name}: default {problem}" for name, problem in defaults.items() if problem),
    ]


# A factory may return something new on every call, so one sampled result proves nothing:
# only the language's empty containers are taken, anything else is refused.
_FACTORIES = (list, dict, set, tuple, frozenset)


def _approved(factory: object) -> typing.TypeGuard[type]:
    """By identity: ``in`` would ask the factory's own ``__eq__``, which can say yes to anything."""
    return any(factory is approved for approved in _FACTORIES)


_FACTORY = "comes from a factory other than an empty list, dict, set, tuple or frozenset; " + (
    "a contract default is a value"
)


def _declared_defaults(cls: type) -> dict[str, object]:
    """Each field's plain default object, the one the class binds under the field's name."""
    if issubclass(cls, BaseModel):
        return {name: info.default for name, info in cls.model_fields.items()}
    if dataclasses.is_dataclass(cls):
        return {f.name: f.default for f in dataclasses.fields(cls)}
    return {}


def _field_defaults(cls: type) -> dict[str, object]:
    """Each field's default, as declared: neither Pydantic nor a dataclass checks one."""
    declared: dict[str, tuple[object, object]]
    if issubclass(cls, BaseModel):
        declared = {name: (info.default_factory, info.default) for name, info in cls.model_fields.items()}
        missing: object = PydanticUndefined
    else:
        declared = {f.name: (f.default_factory, f.default) for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        missing = dataclasses.MISSING
    defaults: dict[str, object] = {}
    for name, (factory, default) in declared.items():
        if factory not in (None, dataclasses.MISSING):
            defaults[name] = factory() if _approved(factory) else _FACTORY
        elif default is not missing:
            defaults[name] = default
    return defaults


# BaseException's own constructor takes ``*args: object`` and hands them back as ``.args``,
# so an error without a typed ``__init__`` of its own carries whatever a raiser passes.
_UNTYPED_ARGS = (
    "__init__(): is BaseException's, whose *args take any object and hand it out as .args; declare a typed __init__"
)


def _error_problems(cls: type, seen: frozenset[type] = frozenset()) -> list[str]:
    """An error's constructor, annotated attributes and methods: it is raised across the boundary.

    An error may refine a non-contract one, so what it inherits from there is read too.
    """
    bases = _own_bases(cls)
    # A builtin error is the language's: any code can raise it, contract or not.
    typed = not bases or any(inspect.isfunction(vars(k).get("__init__")) for k in bases)
    untyped = [] if typed else [_UNTYPED_ARGS]
    return [*_member_problems(cls, attributes=True, seen=seen, bases=bases), *untyped]


def _class_problems(cls: type, seen: frozenset[type] = frozenset()) -> list[str]:
    """Findings on a declared class, each as the suffix to its dotted name."""
    seen = seen | {cls}
    problem = _class_problem(cls)
    if problem:
        return [f": {problem}"]
    if issubclass(cls, BaseException):
        return [f".{p}" for p in _error_problems(cls, seen)]
    if issubclass(cls, enum.Enum):
        return []  # its members are plain str or int values, or `_class_problem` said so
    if getattr(cls, "_is_protocol", False):
        return [f".{p}" for p in _member_problems(cls, attributes=True, seen=seen)]
    # A value type's fields are read through its model or dataclass, its methods like a Protocol's.
    fields = (f": {p}" for p in _field_problems(cls, seen))
    return [*fields, *(f".{p}" for p in _member_problems(cls, attributes=False, seen=seen))]


def _surface(module: ModuleType) -> dict[str, object]:
    """What a contract module offers: its ``__all__`` plus every public name bound in it.

    ``__all__`` is not a fence: ``from apps.x.contract import name`` reaches any binding.
    """
    exported = vars(module).get("__all__") or []  # read as bound: a module __getattr__ would answer too
    public = [name for name in vars(module) if not name.startswith("_")]
    return {name: getattr(module, name) for name in sorted({*exported, *public})}


def _private_bindings(module: ModuleType) -> dict[str, object]:
    """A contract's own underscore and dunder functions, classes and data: ``from apps.x.contract
    import _helper`` (or ``_Hidden``, ``_connection``, ``__connection__``) reaches them all the same,
    so each is read like a public one, imported routines included. What a contract is written with
    (a module, the tooling's own forms) is left out."""
    found: dict[str, object] = {}
    for name, obj in vars(module).items():
        if not name.startswith("_") or name in _MODULE_DUNDERS:
            continue
        if isinstance(obj, type) and obj.__module__ == module.__name__:
            found.update(_class_state(name, obj))  # its code and declaration are read on their own
        elif isinstance(obj, ModuleType) and _behind_a_contract(obj.__name__):
            # ``import apps.x as _root`` hands out the package outside code may not import.
            found[name] = obj
        elif not (isinstance(obj, ModuleType) or _tooling(obj)):
            # An imported class too: ``from sqlite3 import Connection as _Connection`` hands out
            # ``_Connection(":memory:")`` to anyone who imports it, as a re-exported one would.
            # A routine likewise: ``from sqlite3 import connect as _connect`` hands out a connection.
            found[name] = obj
    return found


def _class_state(name: str, cls: type) -> dict[str, object]:
    """The data a private class declared in a contract holds in its own body, as dotted names:
    ``_Hidden.connection`` is reached through ``from apps.x.contract import _Hidden``. Its
    methods, descriptors (slots included) and nested classes are code, read by
    `_private_class_problems`; its dunders, enum ``_sunder_`` names and ``abc``'s
    ``_abc_impl`` are the class machinery's."""
    code = (staticmethod, classmethod, property, functools.cached_property, type, types.MemberDescriptorType)
    return {
        f"{name}.{attr}": value
        for attr, value in _class_members(cls)
        if not (inspect.isroutine(value) or isinstance(value, code) or _tooling(value))
    }


def _class_members(cls: type) -> list[tuple[str, object]]:
    return [
        (attr, value)
        for attr, value in vars(cls).items()
        if not (attr.startswith("_") and attr.endswith("_")) and attr != "_abc_impl"
    ]


def _private_class_problems(module: ModuleType) -> list[str]:
    """What a private class declared in a contract hands out through its code: ``_Hidden.open()``
    is called through ``from apps.x.contract import _Hidden`` like any contract function, so each
    method, property and nested class is read as one, with the data `_class_state` reads."""
    found: list[str] = []
    for name, obj in vars(module).items():
        private = name.startswith("_") and name not in _MODULE_DUNDERS
        if private and isinstance(obj, type) and obj.__module__ == module.__name__:
            found += _class_code_problems(module, name, obj)
    return list(dict.fromkeys(found))  # a value type's methods are read by both readers


def _class_code_problems(module: ModuleType, name: str, cls: type) -> list[str]:
    # ``from apps.x.contract import _Hidden`` reaches it like a public type: a plain class takes
    # any live state, and a base or metaclass hands its whole API across (``class _Hidden(io.FileIO)``
    # opens a file through the inherited constructor), so it is declared like a public one.
    found = [f"{module.__name__}.{name}{p}" for p in _class_problems(cls)]
    for attr, value in vars(cls).items():
        # As on an offered class, a hand-written special method beyond the inert ones is a way in or
        # out (``_Hidden()`` runs ``__new__``), so it is refused rather than read.
        special = attr.startswith("__") and attr.endswith("__") and attr not in _INERT_SPECIAL_METHODS
        fn = _unwrapped(value)
        if special and inspect.isfunction(fn) and not _generated(fn):
            found.append(f"{module.__name__}.{name}.{attr}(): is a hand-written special method; {_REFUSED}")
    for attr, value in _class_members(cls):
        where = f"{name}.{attr}"
        if isinstance(value, type) and value.__qualname__ == f"{cls.__qualname__}.{attr}":
            found += _class_code_problems(module, where, value)  # nested here, so read the same way
            found += [p for k, v in _class_state(where, value).items() for p in _binding_problems(module, k, v)]
            continue
        fn, bound = _method_function(value)
        if fn is not None:
            found += [f"{module.__name__}.{p}" for p in _function_problems(where, fn, bound=bound)]
    return found


def _method_function(value: object) -> tuple[typing.Callable[..., object] | None, bool]:
    """The function a class attribute runs when called or read, and whether it takes a receiver."""
    if isinstance(value, staticmethod):
        return value.__func__, False
    if isinstance(value, classmethod):
        return value.__func__, True
    if isinstance(value, property):
        return value.fget, True
    if isinstance(value, functools.cached_property):
        return value.func, True
    return (value, True) if inspect.isfunction(value) else (None, False)


# What the import system and the compiler bind on a module; any other dunder a contract
# binds (``__connection__ = ...``) is reached like any name, so it is read like one.
# ``__all__`` and ``__getattr__`` are read on their own, and ``__annotations__`` by the store check.
_MODULE_DUNDERS = frozenset(
    {
        "__name__", "__doc__", "__package__", "__loader__", "__spec__", "__path__", "__file__",
        "__cached__", "__builtins__", "__annotations__", "__annotate__", "__all__", "__getattr__",
    }
)  # fmt: skip


def _is_type_alias(annotation: object) -> bool:
    """``TypeAlias``, read as an object or as the string ``from __future__ import annotations`` leaves."""
    if isinstance(annotation, str):
        return annotation.rsplit(".", maxsplit=1)[-1] == "TypeAlias"
    return annotation is typing.TypeAlias


def _export_problems(module: ModuleType, name: str, obj: object) -> list[str]:
    """Findings on one name a contract module offers, each in full."""
    where = f"{module.__name__}.{name}"
    annotation = inspect.get_annotations(module).get(name)
    if _is_type_alias(annotation) and isinstance(obj, str):  # `Payload: TypeAlias = "Connection"` is a type
        problem = _annotation_problem(module, obj)
        return [f"{where}: {problem}"] if problem else []
    # `CONN: Connection | None = None` advertises its annotation as well as holding its value.
    advertised = None if annotation is None or _is_type_alias(annotation) else _annotation_problem(module, annotation)
    return [f"{where}: {p}" for p in (advertised,) if p] + _binding_problems(module, name, obj)


def _annotation_store_problems(module: ModuleType) -> list[str]:
    """``__annotations__`` entries no offered name covers: the mapping hands out what they hold all the same.

    An entry for an offered name is read with that binding (`_export_problems`); one for a private
    binding must still be an annotation, though it may name any type, as the binding may hold anything.
    """
    where = f"{module.__name__}.__annotations__"
    held = vars(module).get("__annotations__", {})
    if type(held) is not dict:
        return [f"{where}: holds a {type(held).__qualname__}, where a dict of annotations belongs"]
    found = []
    offered = _surface(module)
    for key, annotation in held.items():
        if type(key) is not str:
            found.append(f"{where}: has a {type(key).__qualname__} key; an annotation is named by text")
            continue
        if key in offered:
            continue  # read with what it annotates
        # A private binding may hold anything, so its annotation may name any type, but still a type.
        read = key not in vars(module) or not _annotation_like(annotation)
        problem = _annotation_problem(module, annotation) if read else None
        found += [f"{where}[{key!r}]: {problem}"] if problem else []
    return found


def _annotation_problem(module: ModuleType, annotation: object) -> str | None:
    """A module-level annotation, resolved in the module that wrote it, read as a boundary type."""
    if isinstance(annotation, str):
        try:
            annotation = eval(annotation, vars(module))  # the module's own annotation, already imported
        except Exception as exc:
            return f"cannot resolve its annotation ({type(exc).__name__}: {exc})"
    if annotation in (typing.Final, typing.ClassVar):
        return None  # a bare qualifier names no type; the value is read on its own
    if not _annotation_like(annotation):
        return f"holds a {type(annotation).__qualname__}, which is not a boundary type"
    return _type_problem(annotation)


def _annotation_like(annotation: object) -> bool:
    """A type, text, None, the tooling's own forms or a parameterized one: what annotations hold."""
    plain = annotation is None or isinstance(annotation, (type, str)) or _tooling(annotation)
    return plain or typing.get_origin(annotation) is not None


def _binding_problems(module: ModuleType, name: str, obj: object) -> list[str]:
    """Findings on what a name is bound to, each in full."""
    where = f"{module.__name__}.{name}"
    origin = getattr(obj, "__module__", None)
    future = origin == "__future__" and getattr(sys.modules["__future__"], name, None) is obj
    if (future or _tooling(obj)) and typing.get_origin(obj) is None:
        own = getattr(obj, "__name__", None) or getattr(obj, "_name", None)
        if future or (own == name and obj is not typing.Any):
            return []  # what a contract is written with, imported under its own name
        # Sol, PR #6182: ``contract.Any`` is offered like any public name (``__all__`` is no
        # fence), so ``Any`` is refused under its own name too; import it as ``_Any`` to write with.
        problem = _type_problem(obj)  # a renamed one is an alias: `Payload = Any` exports Any
        return [f"{where}: {problem}"] if problem else []
    if isinstance(origin, str) and _is_contract_module(origin) and isinstance(obj, type):
        # Declared here or re-exported from another contract: checked on every surface it is
        # offered from, so an enforced contract cannot hand out a loose contract's Protocol.
        return [f"{where}{p}" for p in _class_problems(obj)]
    if inspect.isfunction(obj):
        return [f"{module.__name__}.{p}" for p in _function_problems(name, obj)]
    problem = _offered_problem(module, obj)
    return [f"{where}: {problem}"] if problem else []


def _offered_problem(module: ModuleType, obj: object) -> str | None:
    """Why a module, alias, foreign class, untyped callable or constant cannot be offered."""
    if isinstance(obj, ModuleType):
        if obj.__name__.startswith(f"{module.__name__}."):
            return None  # one of its own submodules, checked as a module in its own right
        return f"re-exports the module {obj.__name__}, which is not this contract's"
    if typing.get_origin(obj) is not None:  # a type alias such as Literal[...] or list[Model]
        return _type_problem(obj)
    if isinstance(obj, type):
        if issubclass(obj, (enum.Enum, BaseException)):  # declared elsewhere, so read here
            problem = _class_problem(obj)
            members = [] if problem or issubclass(obj, enum.Enum) else _error_problems(obj)
            found = [f"which {problem}"] if problem else [f"with {p}" for p in members]
            return f"re-exports {obj.__qualname__}, {found[0]}" if found else None
        if obj in _SCALARS:
            return None
        return f"re-exports {obj.__module__}.{obj.__qualname__}, which is not a contract type"
    if inspect.isroutine(obj) or isinstance(obj, functools.partial):
        return f"re-exports {getattr(obj, '__qualname__', obj)!s}, a callable with no typed signature"
    return _value_problem(obj)


# The exact classes a scalar value may have, by identity: ``__module__`` is writable, so a
# subclass can claim to be the standard library's. pathlib builds its concrete flavors.
_SCALAR_CLASSES = frozenset(
    {
        *_SCALAR_VALUES, pathlib.PosixPath, pathlib.WindowsPath, pathlib.PurePosixPath, pathlib.PureWindowsPath,
    }
)  # fmt: skip


def _zone_problem(value: object) -> str | None:
    """A datetime or time carries its zone object along, and a custom zone can carry anything."""
    zone = getattr(value, "tzinfo", None)
    if zone is None or type(zone) is datetime.timezone:
        return None
    return f"holds a time zone {type(zone).__qualname__}; a contract time is naive or has a fixed datetime.timezone"


def _value_problem(value: object, path: frozenset[int] = frozenset()) -> str | None:
    """Why a contract constant cannot cross a module boundary, or None when it can.

    ``path`` holds the containers and instances being read, so a value that holds itself is a finding.
    """
    if id(value) in path:
        return f"holds itself through a {type(value).__qualname__}, which no boundary can carry"
    inner = path | {id(value)}
    if isinstance(value, enum.Enum):  # a private enum is on no surface, so read its class here
        problem = _class_problem(type(value))
        return f"holds a member of {type(value).__qualname__}, which {problem}" if problem else None
    if isinstance(value, BaseException):
        return f"holds a {type(value).__qualname__} instance; a contract raises errors, it does not hold them"
    # Only the language's own scalars and containers: a subclass can carry any state it likes.
    if value is None or type(value) in _SCALAR_CLASSES:
        return _zone_problem(value)
    if isinstance(value, (list, tuple, set, frozenset)) and type(value) in (list, tuple, set, frozenset):
        return next((p for p in (_value_problem(item, inner) for item in value) if p), None)
    if isinstance(value, dict) and type(value) is dict:
        items = [*value.keys(), *value.values()]
        return next((p for p in (_value_problem(item, inner) for item in items) if p), None)
    kind = type(value)
    if _is_contract_module(kind.__module__) and not _class_problem(kind):
        # An instance of a closed frozen contract type; a private one is on no surface, so read it here.
        found = [] if _on_surface(kind) else _class_problems(kind)
        # Neither a dataclass nor a default checks what was stored, so read the instance itself.
        stored = {name: _value_problem(item, inner) for name, item in _instance_values(value).items()}
        found += [f": field {name} {problem}" for name, problem in stored.items() if problem]
        return next((f"holds a {kind.__qualname__}{_joined(p, 'that has')}" for p in found), None)
    return f"holds a {kind.__qualname__}, which is not a boundary value"


def _instance_values(value: object) -> dict[str, object]:
    """What an instance stores: its fields, and anything set beside them (``object.__setattr__``, a slot)."""
    if isinstance(value, BaseModel):
        names: list[str] = list(type(value).model_fields)
    elif dataclasses.is_dataclass(value):
        names = [f.name for f in dataclasses.fields(value)]
    else:
        names = []  # a Protocol instance declares no fields, yet stores whatever is set on it
    kind: type = type(value)
    for k in kind.__mro__:
        slots = getattr(k, "__slots__", ())
        names += [slots] if isinstance(slots, str) else list(slots)
    stored = {name: getattr(value, name) for name in names if hasattr(value, name)}
    return {**stored, **getattr(value, "__dict__", {})}


_SOURCE_ONLY = "a contract is Python source, the only form these gates read"
_NON_SOURCE = NON_SOURCE_SUFFIXES  # every platform's, not this interpreter's


def _non_source_problems(root: ModuleType) -> list[str]:
    """A compiled extension or bytecode file below a contract imports as a module no gate reads,
    and so does anything below a directory link, which Python follows and ``rglob`` does not."""
    found = []
    for base in map(pathlib.Path, getattr(root, "__path__", [])):
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(base)
            if "__pycache__" in rel.parts:
                continue
            if path.is_symlink() and path.is_dir():
                found.append(f"{root.__name__}: holds {rel.as_posix()}/, a directory link these gates do not walk")
            elif path.name.endswith(_NON_SOURCE):
                found.append(f"{root.__name__}: holds {rel.as_posix()}; {_SOURCE_ONLY}")
    # Importing the contract runs every package above it first (``apps``, then its package root), and
    # a compiled initializer there wins over the __init__.py beside it; a compiled contract module
    # would shadow a namespace one.
    parts = root.__name__.split(".")
    for depth in range(len(parts) - 1, 0, -1):
        package = sys.modules.get(".".join(parts[:depth]))
        stems = ("__init__", CONTRACT) if depth == len(parts) - 1 else ("__init__",)
        for base in map(pathlib.Path, getattr(package, "__path__", [])):
            for path in sorted(base.iterdir()):
                if str(path) == root.__file__:
                    continue  # the contract itself, reported as loaded from that file
                if path.name.split(".", 1)[0] in stems and path.name.endswith(_NON_SOURCE):
                    found.append(f"{getattr(package, '__name__', '')}: holds {path.name}; {_SOURCE_ONLY}")
    return found


def declaration_problems(pkg: str, container: str = CONTAINER) -> list[str]:
    """Every name a contract offers (``apps.<pkg>.contract`` and its submodules) that breaks the rule above."""
    token = _CONTAINER.set(container)
    try:
        return _declaration_problems(pkg, container)
    finally:
        _CONTAINER.reset(token)


def _declaration_problems(pkg: str, container: str) -> list[str]:
    modules = contract_modules(pkg, container)
    problems: set[str] = set()
    version = getattr(modules[0], "CONTRACT_VERSION", None)
    if not (isinstance(version, str) and re.fullmatch(rf"{re.escape(pkg)}/\d+", version)):
        problems.add(f"{modules[0].__name__}: CONTRACT_VERSION must be a string '{pkg}/<n>', got {version!r}")
    problems.update(_non_source_problems(modules[0]))
    for module in modules:
        if not (module.__file__ is None or module.__file__.endswith(".py")):
            problems.add(f"{module.__name__}: is loaded from {pathlib.Path(module.__file__).name}; {_SOURCE_ONLY}")
        if type(module) is not ModuleType:  # its class's __getattribute__ answers names the walk never sees
            problems.add(f"{module.__name__}: is a {type(module).__qualname__}, where a contract is a plain module")
        exported = vars(module).get("__all__", [])
        if not (type(exported) in (list, tuple) and all(type(name) is str for name in exported)):
            # Only its names are read as exports; anything else it holds is reached as ``contract.__all__``.
            problems.add(f"{module.__name__}.__all__: is a {type(exported).__qualname__}; a contract lists names")
        if "__getattr__" in vars(module):  # PEP 562: it answers any name the walk below never sees
            problems.add(f"{module.__name__}: defines a module __getattr__, whose names nothing can read")
        for name, obj in _surface(module).items():
            problems.update(_export_problems(module, name, obj))
        problems.update(
            p for name, obj in _private_bindings(module).items() for p in _binding_problems(module, name, obj)
        )
        problems.update(_private_class_problems(module))
        problems.update(_annotation_store_problems(module))
    return sorted(problems)
