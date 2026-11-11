"""Export, value and declaration-entry checks for module contracts.

Split from ``scripts/arch_contract_schema.py``. Import the public names
from ``scripts.arch_contract_schema``; this module is an implementation slice.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import functools
import inspect
import pathlib
import re
import sys
import typing
from types import ModuleType

from pydantic import BaseModel

from scripts.arch_contract_schema_kinds import _SCALAR_VALUES
from scripts.arch_surface import CONTAINER, CONTRACT, NON_SOURCE_SUFFIXES

# Names used across siblings are bound on scripts.arch_contract_schema after load;
# function globals are rebound to that module so a monkeypatch there is seen.
# ruff: noqa: F821

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
