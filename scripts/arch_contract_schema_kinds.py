"""Class, model, enum and type-shape checks for module contracts.

Split from ``scripts/arch_contract_schema.py``. Import the public names
from ``scripts.arch_contract_schema``; this module is an implementation slice.
"""

from __future__ import annotations

import builtins
import collections.abc
import contextvars
import dataclasses
import datetime
import enum
import importlib
import inspect
import pathlib
import re
import sys
import types
import typing
from types import ModuleType

from pydantic import BaseModel, JsonValue
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined

from scripts.arch_surface import CONTAINER, CONTRACT

# Names used across siblings are bound on scripts.arch_contract_schema after load;
# function globals are rebound to that module so a monkeypatch there is seen.
# ruff: noqa: F821


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
