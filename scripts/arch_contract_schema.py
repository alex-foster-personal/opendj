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

# Rebound functions look up these names on this module, as they did before the split.
import abc  # noqa: F401
import ast  # noqa: F401
import builtins  # noqa: F401
import collections.abc  # noqa: F401
import contextvars  # noqa: F401
import dataclasses  # noqa: F401
import datetime  # noqa: F401
import enum  # noqa: F401
import functools  # noqa: F401
import importlib  # noqa: F401
import inspect  # noqa: F401
import operator  # noqa: F401
import pathlib  # noqa: F401
import re  # noqa: F401
import sys  # noqa: F401
import textwrap  # noqa: F401
import types
import typing  # noqa: F401
from types import ModuleType  # noqa: F401

from pydantic import BaseModel, ConfigDict, JsonValue, computed_field  # noqa: F401
from pydantic.fields import ComputedFieldInfo, FieldInfo  # noqa: F401
from pydantic_core import PydanticUndefined  # noqa: F401

from scripts.arch_contract_schema_kinds import (  # noqa: F401  # re-exported: public surface
    _CONTAINER,
    _CONTAINERS,
    _ENUM_TOOLING,
    _FROZEN_CONTAINERS,
    _MEMBER_KEYS,
    _SCALAR_VALUES,
    _SCALARS,
    _TOOLING,
    _annotation_held,
    _attribute_problems,
    _behind_a_contract,
    _bookkeeping_problem,
    _builtin,
    _class_problem,
    _computed_field_problem,
    _contract_bases,
    _contract_type_problem,
    _dataclass_problem,
    _enum_machinery_intact,
    _enum_problem,
    _field_info_problem,
    _inherited_base,
    _is_contract_module,
    _joined,
    _machinery_intact,
    _metaclass_problem,
    _metadata_item_problem,
    _metadata_problem,
    _model_problem,
    _mutable_problem,
    _on_surface,
    _own_bases,
    _Plain,
    _PlainStr,
    _submodule_names,
    _tooling,
    _type_problem,
    contract_modules,
)
from scripts.arch_contract_schema_members import (  # noqa: F401  # re-exported: public surface
    _FACTORIES,
    _FACTORY,
    _FIELD_FLAGS,
    _REFUSED,
    _UNTYPED_ARGS,
    _approved,
    _binds,
    _class_code_problems,
    _class_members,
    _class_problems,
    _class_state,
    _dataclass_field_problem,
    _declared_annotation,
    _declared_defaults,
    _error_problems,
    _field_defaults,
    _field_names,
    _field_problems,
    _function_problems,
    _held_defaults,
    _inert_problems,
    _instance_state_problems,
    _member_problems,
    _method_function,
    _model_field_names_problem,
    _post_init_problems,
    _private_bindings,
    _private_class_problems,
    _property_problems,
    _retained_problems,
    _shadow_problems,
    _stored,
    _surface,
    _text_problem,
)
from scripts.arch_contract_schema_tooling import (  # noqa: F401  # re-exported: public surface
    _ENUM_MACHINERY,
    _INERT_SPECIAL_METHODS,
    _MODEL_MACHINERY,
    _PORT_MACHINERY,
    _SCHEMAS_READING,
    _TOOLING_CODES,
    _compiled_problem,
    _core_schema_problem,
    _dataclass_generated,
    _declared_fields,
    _decorated,
    _decorator_record_problem,
    _decorators_problem,
    _FieldlessModel,
    _FrozenModel,
    _generated,
    _kind_references,
    _machinery,
    _machinery_empty,
    _owner_fields,
    _plain_state,
    _PlainError,
    _PlainModel,
    _PlainPort,
    _PlainRecord,
    _pseudo_field,
    _schema_tree_problem,
    _SlottedRecord,
    _sunder,
    _surface_member,
    _tooling_closure,
    _tooling_dunder,
    _tooling_map_problem,
    _unwrapped,
)
from scripts.arch_contract_schema_values import (  # noqa: F401  # re-exported: public surface
    _MODULE_DUNDERS,
    _NON_SOURCE,
    _SCALAR_CLASSES,
    _SOURCE_ONLY,
    _annotation_like,
    _annotation_problem,
    _annotation_store_problems,
    _binding_problems,
    _declaration_problems,
    _export_problems,
    _instance_values,
    _is_type_alias,
    _non_source_problems,
    _offered_problem,
    _value_problem,
    _zone_problem,
    declaration_problems,
)
from scripts.arch_surface import CONTAINER, CONTRACT, NON_SOURCE_SUFFIXES  # noqa: F401


def _rebind_moved_functions() -> None:
    """Make moved functions look up names on this module.

    Patching scripts.arch_contract_schema.NAME then affects the moved code, not
    only the re-export binding on this module.
    """

    from scripts import arch_contract_schema_kinds as kinds_mod
    from scripts import arch_contract_schema_members as members_mod
    from scripts import arch_contract_schema_tooling as tooling_mod
    from scripts import arch_contract_schema_values as values_mod

    for mod in (kinds_mod, members_mod, tooling_mod, values_mod):
        for name, obj in vars(mod).items():
            if isinstance(obj, types.FunctionType) and obj.__module__ == mod.__name__:
                rebound = types.FunctionType(
                    obj.__code__,
                    globals(),
                    obj.__name__,
                    obj.__defaults__,
                    obj.__closure__,
                )
                rebound.__module__ = __name__
                rebound.__doc__ = obj.__doc__
                rebound.__qualname__ = obj.__qualname__
                rebound.__annotations__ = obj.__annotations__
                rebound.__kwdefaults__ = obj.__kwdefaults__
                rebound.__dict__.update(obj.__dict__)
                globals()[name] = rebound
                setattr(mod, name, rebound)


_rebind_moved_functions()
del _rebind_moved_functions
