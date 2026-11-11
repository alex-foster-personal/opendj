"""Member, field and class-state checks for module contracts.

Split from ``scripts/arch_contract_schema.py``. Import the public names
from ``scripts.arch_contract_schema``; this module is an implementation slice.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import functools
import inspect
import textwrap
import types
import typing
from types import ModuleType

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

# Names used across siblings are bound on scripts.arch_contract_schema after load;
# function globals are rebound to that module so a monkeypatch there is seen.
# ruff: noqa: F821

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
