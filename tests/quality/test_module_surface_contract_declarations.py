"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

Contract declaration purity on fixtures and enforced packages.
"""

from __future__ import annotations

import configparser
import importlib
import importlib.machinery
import os
import py_compile
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.arch_contract_schema import declaration_problems
from scripts.arch_surface import (
    DYNAMIC,
    Violation,
    call_loads,
    compiled_modules,
    loaded_imports,
    package_of,
    pure_scope_imports,
    source_imports,
    statement_imports,
    surface_violations,
    unread_modules,
    unread_root_modules,
)

from tests.quality import module_surface_contract_support as sup


_check = sup._check
_section = sup._section
_enforced = sup._enforced
_packages = sup._packages
_tree = sup._tree
_findings = sup._findings
_fixture = sup._fixture
_contract_packages = sup._contract_packages
_dotted = sup._dotted
_hold_python = sup._holds_python
_importable = sup._importable
_module_name = sup._module_name
_namespace_dirs = sup._namespace_dirs
_outside_graph_sources = sup._outside_graph_sources
_outside_graph_violations = sup._outside_graph_violations
_python_sources = sup._python_sources
_top_level_modules = sup._top_level_modules
_TC_CONTRACT = sup._TC_CONTRACT
_DYNAMIC_ROOT = sup._DYNAMIC_ROOT
REPO = sup.REPO
CONTRACTS = sup.CONTRACTS
SECTION = sup.SECTION
DELIVERY = sup.DELIVERY
COMPOSITION_ROOTS = sup.COMPOSITION_ROOTS
PREDATES_ADR = sup.PREDATES_ADR
OWED_CONTRACT = sup.OWED_CONTRACT
NOT_PACKAGES = sup.NOT_PACKAGES
ROOT_NOT_PACKAGES = sup.ROOT_NOT_PACKAGES
ROOT_MODULES = sup.ROOT_MODULES
FIXTURES = sup.FIXTURES
LOOSE_BOUNDARIES = sup.LOOSE_BOUNDARIES


def test_a_closed_frozen_contract_declares_nothing_wrong() -> None:
    # Overshoot control for every rule below: models, frozen dataclasses with clean methods,
    # enums, exceptions, a private self-referencing type, Protocols over paths, containers,
    # literals, optionals, JsonValue and contract models, a clean setter and __repr__, a
    # function, type aliases, constants, a submodule, a re-export from another contract, a
    # stdlib exception, a private sqlite3 binding, a Literal of an enum member and the typing
    # and pydantic tooling, `Any` included, imported under their own names.
    assert _fixture("clean") == []


def test_open_or_mutable_declarations_are_findings() -> None:
    assert _fixture("open_types") == [
        '.Loose: a Pydantic model needs model_config = ConfigDict(frozen=True, extra="forbid")',
        ".Mutable: a dataclass needs frozen=True (or make it a frozen Pydantic model)",
        ".Plain: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
    ]


def test_a_frozen_type_with_an_open_field_is_a_finding() -> None:
    # Controls inside the fixture: Defaulted.name, Factoried.tags (an empty tuple) and DataAware.name have
    # clean defaults. Any other factory is refused: one call proves nothing about the next. A field
    # holds a tuple or frozenset only (Sol, PR #6182): Mutables.kept, .tags and .notes are controls.
    # JsonValue stays a boundary type for a parameter or return (the clean fixture's payload).
    assert _fixture("open_fields") == [
        ".BareContainers: field rows: typing.List has no element type",
        ".DataAware: field label: "
        "default comes from a factory other than an empty list, dict, set, tuple or frozenset; a contract default is a value",
        ".Defaulted.__pydantic_core_schema__: holds a Connection, which Pydantic does not put there",
        ".Defaulted: field conn: default holds a Connection, which is not a boundary value",
        ".Defaulted: field rows: "
        "default comes from a factory other than an empty list, dict, set, tuple or frozenset; a contract default is a value",
        ".Defaulted: field rows: list[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Factoried: field rows: "
        "default comes from a factory other than an empty list, dict, set, tuple or frozenset; a contract default is a value",
        ".HoldsConnection: field conn: Connection is not a boundary type",
        ".ModelWithAny: field rows: Any is not a boundary type",
        ".MutableRecord: field by_name: dict[str, int] "
        "can be changed in place; a contract field holds a tuple or frozenset",
        ".MutableRecord: field labels: list[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables.__pydantic_core_schema__: holds a _SpecialGenericAlias, which Pydantic does not put there",
        ".Mutables: field maybe: list[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field nested: set[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field payload: JsonValue can hold a list or dict, "
        "which can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field payloads: JsonValue can hold a list or dict, "
        "which can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field seq: collections.abc.Sequence[str] "
        "can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field table: collections.abc.Mapping[str, int] "
        "can be changed in place; a contract field holds a tuple or frozenset",
    ]


def test_a_protocol_member_that_leaks_an_open_type_is_a_finding() -> None:
    # Controls inside the fixture: Leaky.name(), InheritsOpen.name(), LeakyAttribute.path,
    # LeakySetter's getter, CallsOpen.__repr__ and Store.__hash__ (inert) are clean and stay
    # unreported. A hand-written special method beyond the inert ones is refused, not read.
    assert _fixture("protocols") == [
        ".BoundAttribute.label: binds a value to an annotated attribute; "
        "a contract declares only fields, methods and properties on a class",
        ".CallsOpen.__call__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".InheritsOpen.open(): return: Connection is not a boundary type",
        ".Leaky.open(): return: Connection is not a boundary type",
        ".LeakyAttribute.connection: Connection is not a boundary type",
        ".LeakyFactories.connect(): return: Connection is not a boundary type",
        ".LeakyFactories.open(): return: Connection is not a boundary type",
        ".LeakyProperty.conn(): return: Connection is not a boundary type",
        ".LeakySetter.db.setter(): value: Connection is not a boundary type",
        ".Store.__setitem__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".Unannotated.run(): return has no annotation",
        ".Unannotated.run(): x has no annotation",
        ".Untyped.load(): payload: Any is not a boundary type",
        ".Untyped.load(): return: Any is not a boundary type",
        "._OpensBase.open(): return: Connection is not a boundary type",
    ]


def test_value_types_functions_and_enums_are_read_through_or_refused() -> None:
    # Controls inside the fixture: OpensItself.name(), reads(), Pinned.side, ContractError.reason(),
    # StoreError (a refined sqlite3.Error with a typed constructor), TypedError, runs()'s limit
    # default, ContractError.code, RETRIES, BATCH (a bare Final), ROOT, NAMES, LOOKUP, UTC_STAMP,
    # Titled, Sealed, Resealed (a frozen dataclass's decorated subclass), Restated (a plain value
    # over an inherited field's default), bounded() and
    # TaggedModel.size (Annotated tooling and plain metadata), Tone (a StrEnum),
    # Compares.__hash__ (inert), DIAMOND (one list reached twice), Normalized's __post_init__
    # (it resets only its own declared field), TypedError's __init__ (it sets only the attribute
    # it annotates) and every generated __init__ are clean. A __post_init__, or an error's
    # __init__ (Sol, PR #6182), that sets an undeclared or computed name, or hands its instance
    # on, is refused. An enum with more than its
    # members (an attribute stored on a member included), an undecorated dataclass subclass, a
    # ClassVar, an InitVar, a cached property, a nested class, a function attribute, a held
    # exception and a custom metaclass are refused, not read.
    assert _fixture("values") == [
        ".ATTACHED: holds a Attached that has field connection holds a Connection, which is not a boundary value",
        ".Annkeyed.__annotations__: is keyed by a Connection, where its tooling keys by name",
        ".Attached.__post_init__(): sets connection, which no field declares; "
        "a contract declares only fields, methods and properties on a class",
        ".CachesConnection.conn: binds a cached_property; "
        "a contract declares only fields, methods and properties on a class",
        ".Compares.__eq__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".Compiled.__repr__(): stores connection on the function; a contract declares only fields, methods and properties on a class",
        ".Computed: computed field label holds a Connection, which is not a boundary value",
        ".ComputedAs: computed field label: return_type Connection is not a boundary type",
        ".Configured: model_config holds a Connection, which is not a boundary value",
        ".ContractError.__init__(): is BaseException's, whose "
        "*args take any object and hand it out as .args; declare a typed __init__",
        ".ContractError.connection: Connection is not a boundary type",
        ".ContractError.open(): return: Connection is not a boundary type",
        ".Corecycled.__pydantic_core_schema__: holds itself through a dict, which no boundary can carry",
        ".Coreheld.__pydantic_core_schema__: holds a Connection, which Pydantic does not put there",
        ".Corekeyed.__pydantic_core_schema__: is keyed by a int, where Pydantic keys by name",
        ".Custom.__init__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".DISGUISED: holds a _Disguised, which is not a boundary value",
        ".Dcfaked.TAG: is annotated but no field; a contract declares only fields, methods and properties on a class",
        ".Dcfaked.__dataclass_fields__: maps 'TAG' to a _FakeClassVar the field scan never reads",
        ".Dcghost.__dataclass_fields__: maps 'ghost' to a _NotAField the field scan never reads",
        ".Dckeyed.__dataclass_fields__: is keyed by a Connection, where its tooling keys by name",
        ".Dcpseudo.__dataclass_fields__: maps 'ghost' to a Field the field scan never reads",
        ".Dcrenamed.__dataclass_fields__: maps 'alias' to a field of another name",
        ".Decorated.__pydantic_decorators__: validators is keyed by a Connection, where Pydantic keys by name",
        ".DefaultedRow: field conn: default holds a Connection, which is not a boundary value",
        ".Delegated.__post_init__(): hands its instance on; a contract declares only fields, methods and properties on a class",
        ".Described.cleared.deleter(): return: Connection is not a boundary type",
        ".Described.cursor: binds a builtin_function_or_method as an accessor; "
        "a contract declares only fields, methods and properties on a class",
        ".Described.held: binds a _Held; a contract declares only fields, methods and properties on a class",
        ".Described.label: its docstring holds a Connection, which is not a boundary value",
        ".Dundered.__connection__: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Envelope.Payload: binds a type; a contract declares only fields, methods and properties on a class",
        ".Envelope: field payload: Envelope.Payload has field conn: Connection is not a boundary type",
        ".EqSpoofed.__eq__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".Evaluated.__bool__(): return: annotation holds a Connection, which is not a boundary type",
        ".Evaluated.__format__(): held: default holds a Connection, which is not a boundary value",
        ".Evaluated.__str__(): its closure's held holds a Connection, which is not a boundary value",
        ".FieldMetadata: field name: metadata holds a Connection, which is not a boundary value",
        ".Fieldkeyed.__pydantic_fields__: is keyed by a Connection, where its tooling keys by name",
        ".Flagged: field name: its compare flag holds a Connection, where a bool belongs",
        ".Genericheld.__pydantic_generic_metadata__: names a type where Connection is not a boundary type",
        ".HELD_ERROR: holds a TypedError instance; a contract raises errors, it does not hold them",
        ".HandedError.__init__(): hands its instance on; a contract declares only fields, methods and properties on a class",
        ".HeldReceiver.read(): self: annotation holds a Connection, which is not a boundary value",
        ".HeldReceiver.write(): self: default holds a Connection, which is not a boundary value",
        ".Hidden: a Pydantic model declares private attributes (_connection)",
        ".Hoards._Hoards__pool: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Hoards._connection: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".HoldsHandle: field handle: _Handle.open(): return: Connection is not a boundary type",
        ".Hooked.__pydantic_core_schema__: holds a method, which Pydantic does not put there",
        ".LOOP: holds itself through a list, which no boundary can carry",
        ".ListPayload: inherits builtins.list, which is not a contract type",
        ".LiveConnection: inherits sqlite3.Connection, which is not a contract type",
        ".Machined._abc_impl: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".MachinedPort._abc_impl: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Matched.__match_args__: binds a tuple; a contract declares only fields, methods and properties on a class",
        ".MetaBuiltin: uses the metaclass builtins._BuiltinMeta, which is neither the language's nor the tooling's",
        ".MetaSpoofed: uses the metaclass abc._SpoofedMeta, which is neither the language's nor the tooling's",
        ".Metered: uses the metaclass "
        f"{FIXTURES}.values.contract._Meta, which is neither the language's nor the tooling's",
        ".Misnamed: is an enum with more than plain str or int members",
        ".Mixed: inherits pydantic._Mixin, which is not a contract type",
        ".Mode: is an enum with more than plain str or int members",
        ".Opener: is an enum with more than plain str or int members",
        ".OpensItself.open(): return: Connection is not a boundary type",
        ".Outer: field inner: _Inner has field conn: Connection is not a boundary type",
        ".Paramfaked: its dataclass parameter frozen holds a str, where the decorator stores a bool",
        ".Paramheld: its dataclass parameter init holds a Connection, where the decorator stores a bool",
        ".Paramswapped: holds a _Params as its dataclass parameters",
        ".Payload: re-exports typing.Payload, which is not a contract type",
        ".Pinned: field mode: holds a member of _Hidden, which is an enum with more than plain str or int members",
        ".PrivatePort._open(): return: Connection is not a boundary type",
        ".ROWS: holds a _Rows, which is not a boundary value",
        ".Raised.open(): return: Connection is not a boundary type",
        ".Reassigned: field label: Annotated metadata holds a Connection, which is not a boundary value",
        ".Reassigned: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Rebased.__init__(): its closure's __class__ holds a type, which is not a boundary value",
        ".Reclassed.__init__(): its closure's __class__ holds a Connection, which is not a boundary value",
        ".Recorded: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Redecorated.__pydantic_decorators__: computed_fields['size'] records a function other than the one its class binds under that name",
        ".Redescribed: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Redoc.__repr__(): its __doc__ holds a Connection, where text belongs",
        ".Redoc.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Redoc.__repr__(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        ".Redocumented: its __doc__ holds a Connection, where text belongs",
        ".Registered._abc_impl: binds a _abc_data; a contract declares only fields, methods and properties on a class",
        ".Rehomed: is an enum with more than plain str or int members",
        ".Rekeyed: is an enum with more than plain str or int members",
        ".Remapped: is an enum with more than plain str or int members",
        ".Renamed: is an enum with more than plain str or int members",
        ".Renumbered: model_fields names 'ghost', a field no model class in its MRO declares",
        ".Rerecorded: field name: Annotated metadata _attributes_set has a Connection key, where Pydantic names a setting by text",
        ".Rerepr.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Rerepr.__repr__(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        ".Rerouted.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Rerouted.__repr__(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        ".Reserialized.__pydantic_serializer__: holds a SchemaSerializer built from another schema than the class's own",
        ".Respelled.__post_init__(): sets a computed name 'na' + 'me', which no field declares; "
        "a contract declares only fields, methods and properties on a class",
        ".Retagged.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Retagged.__repr__(): stores __wrapped__, connection on the function; a contract declares only fields, methods and properties on a class",
        ".Retains.__format__(): held: default holds a Connection, which is not a boundary value",
        ".Retargeted: is an enum with more than plain str or int members",
        ".Retyped: field name: its retained type is a Connection, not the annotation its class wrote",
        ".Revalidated.__pydantic_validator__: holds a SchemaValidator built from another schema than the class's own",
        ".Revalued: is an enum with more than plain str or int members",
        ".Rewrapped: computed field label keeps a property other than the one its class declares",
        ".Rogue: is an enum with more than plain str or int members",
        ".Rows.__iter__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".SHARED: Connection is not a boundary type",
        ".SHOWN: holds a Connection, which is not a boundary type",
        ".SLOTTED: holds a Slotted that has field extra holds a Connection, which is not a boundary value",
        ".STAMP: holds a time zone _Zone; a contract time is naive or has a fixed datetime.timezone",
        ".Seeded.conn: is annotated but no field; a contract declares only fields, methods and properties on a class",
        ".Settings.CONN: is annotated but no field; a contract declares only fields, methods and properties on a class",
        ".Shadowed.token: shadows its field's default and holds a Connection, which is not a boundary value",
        ".Slotted.__post_init__(): sets extra, which no field declares; "
        "a contract declares only fields, methods and properties on a class",
        ".Slotted.extra: binds a member_descriptor; a contract declares only fields, methods and properties on a class",
        ".SpoofedError.__init__(): is BaseException's, whose *args take any object and hand it out as .args; declare a typed __init__",
        ".SpoofedError.connection: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Stamped: is an enum with more than plain str or int members",
        ".Stashed: is an enum with more than plain str or int members",
        ".StashedError.__init__(): sets connection, which no annotated attribute declares; "
        "a contract declares only fields, methods and properties on a class",
        ".StaticSelf.build(): cls: Connection is not a boundary type",
        ".Stored.__bool__(): stores connection on the function; a contract declares only fields, methods and properties on a class",
        ".Stored.__str__(): its closure's held holds a Connection, which is not a boundary value",
        ".Subscripts.__class_getitem__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".TABLE: holds a _Table, which is not a boundary value",
        ".TOKEN: holds a _Token, which is not a boundary value",
        ".TaggedModel.__pydantic_core_schema__: holds a Connection, which Pydantic does not put there",
        ".TaggedModel: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".TaggedModel: field note: Annotated metadata holds a Connection, which is not a boundary value",
        ".TaggedPort.name: Annotated metadata holds a Connection, which is not a boundary value",
        ".TaggedRow: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Unlisted: is an enum with more than plain str or int members",
        ".Unsealed: subclasses a dataclass without its own @dataclass(frozen=True), "
        "so its instances take new attributes",
        ".Unvalued: is an enum with more than plain str or int members",
        ".UsesOpener: field opener: Opener is not a closed frozen type",
        ".Validtyped.__pydantic_decorators__: field_validators['_stripped'] records an annotation where Connection is not a boundary type",
        ".Wired: is an enum with more than plain str or int members",
        "._Bare: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Bound: re-exports annotated_types._Bound, which is not a contract type",
        "._BuiltinMeta: re-exports builtins._BuiltinMeta, which is not a contract type",
        "._Callable: re-exports collections.abc.Callable, which is not a contract type",
        "._Connection: re-exports sqlite3.Connection, which is not a contract type",
        "._Disguised: re-exports builtins._Disguised, which is not a contract type",
        "._FakeClassVar._field_type: holds a _FIELD_BASE, which is not a boundary value",
        "._FakeClassVar.connection: holds a Connection, which is not a boundary value",
        "._FakeClassVar: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._FileHandle: inherits _io.FileIO, which is not a contract type",
        "._Handle.open(): return: Connection is not a boundary type",
        "._Held.connection: holds a Connection, which is not a boundary value",
        "._Held: inherits builtins.property, which is not a contract type",
        "._Hidden.CONN: holds a member of _Hidden, which is an enum with more than plain str or int members",
        "._Hidden: is an enum with more than plain str or int members",
        "._Inner: field conn: Connection is not a boundary type",
        "._Iterator: re-exports collections.abc.Iterator, which is not a contract type",
        "._Kind: inherits builtins.type, which is not a contract type",
        "._LIVE: holds a Connection, which is not a boundary value",
        "._LOOP: holds itself through a dict, which no boundary can carry",
        "._Lenient.__call__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Lenient.__eq__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Lenient: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        '._Loose: a Pydantic model needs model_config = ConfigDict(frozen=True, extra="forbid")',
        "._Made: uses the metaclass tests.quality.contract_fixtures.values.contract._Kind, which is neither the language's nor the tooling's",
        "._Meta.__getattr__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Meta: inherits builtins.type, which is not a contract type",
        "._Mixin: re-exports pydantic._Mixin, which is not a contract type",
        "._Mutable: a dataclass needs frozen=True (or make it a frozen Pydantic model)",
        "._Name.held: holds a Connection, which is not a boundary value",
        "._Name: inherits builtins.str, which is not a contract type",
        "._NotAField.connection: holds a Connection, which is not a boundary value",
        "._NotAField: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Opener.Inner.cursor(): return: Cursor is not a boundary type",
        "._Opener.Inner: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Opener.current(): return: Connection is not a boundary type",
        "._Opener.open(): return: Connection is not a boundary type",
        "._Opener.reopen(): return: Connection is not a boundary type",
        "._Opener: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Params.__init__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Params: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Registry: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Rows.connection: holds a Connection, which is not a boundary value",
        "._Rows: inherits builtins.list, which is not a contract type",
        "._Spoofed: re-exports _Spoofed, with connection: binds a Connection; a contract declares only fields, methods and properties on a class",
        "._SpoofedError: re-exports _SpoofedError, with open(): return: Connection is not a boundary type",
        "._SpoofedMeta: re-exports abc._SpoofedMeta, which is not a contract type",
        "._Table.connection: holds a Connection, which is not a boundary value",
        "._Table: inherits builtins.dict, which is not a contract type",
        "._Token.__new__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Token: inherits builtins.str, which is not a contract type",
        "._Zone.connection: holds a Connection, which is not a boundary value",
        "._Zone: inherits datetime.tzinfo, which is not a contract type",
        ".__annotations__['_hidden']: holds a Connection, which is not a boundary type",
        ".__annotations__['unbound']: holds a Connection, which is not a boundary type",
        ".__connection__: holds a Connection, which is not a boundary value",
        "._attach(): row: object is not a boundary type",
        "._cached_property: re-exports functools.cached_property, which is not a contract type",
        "._compiled: holds a type, which is not a boundary value",
        "._connect: re-exports connect, a callable with no typed signature",
        "._drop(): return: Connection is not a boundary type",
        "._drop(): self: object is not a boundary type",
        "._elsewhere(): self: object is not a boundary type",
        "._enclosing(): return: Callable is not a boundary type",
        "._guard(): its closure's user_function holds a function, which is not a boundary value",
        "._guard(): return has no annotation",
        "._guard(): self has no annotation",
        "._guard(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        "._name(): self: object is not a boundary type",
        "._port: holds a Receiver that has field connection holds a Connection, which is not a boundary value",
        "._pseudo: holds a Field, which is not a boundary value",
        "._rogue: holds a member of Rogue, which is an enum with more than plain str or int members",
        "._root: re-exports the module tests.quality.contract_fixtures.values, which is not this contract's",
        "._unlisted: holds a member of Unlisted, which is an enum with more than plain str or int members",
        ".annkeyed(): an __annotations__ key holds a Connection, which is not a boundary value",
        ".annotated(): annotates connection, naming no parameter",
        ".dispatched._Dispatching.__getattribute__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".dispatched._Dispatching: inherits builtins.module, which is not a contract type",
        ".dispatched: is a _Dispatching, where a contract is a plain module",
        ".documented(): its __doc__ holds a Connection, where text belongs",
        ".dynamic: defines a module __getattr__, whose names nothing can read",
        ".extra(): name: Annotated metadata holds a Connection, which is not a boundary value",
        ".gated(): limit: Annotated metadata holds a Connection, which is not a boundary value",
        ".gated(): name: Annotated metadata holds a Connection, which is not a boundary value",
        ".gated(): rows: Annotated metadata comes from a factory other than an empty list, dict, set, tuple or "
        "frozenset; a contract default is a value",
        ".hidden.leak.Unread: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        ".kwheld(): __kwdefaults__['connection']: default holds a Connection, which is not a boundary value",
        ".kwkeyed(): a __kwdefaults__ key of type Connection: default holds a Connection, which is not a boundary value",
        ".lenient(): rows: Annotated metadata comes from a factory other than an empty list, dict, set, tuple or "
        "frozenset; a contract default is a value",
        ".opened(): return: Connection is not a boundary type",
        ".opens(): return: Connection is not a boundary type",
        ".posheld(): __defaults__[0]: default holds a Connection, which is not a boundary value",
        ".receives(): self: Connection is not a boundary type",
        ".runs(): conn: default holds a Connection, which is not a boundary value",
        ".spoofed(): limit: Annotated metadata holds a _Bound, which is not a boundary value",
        ".tagged(): name: Annotated metadata holds a Connection, which is not a boundary value",
        ".wired(): stores connection on the function; "
        "a contract declares only fields, methods and properties on a class",
        ".wrapping(): stores _cache on the function; a contract declares only fields, methods and properties on a class",
    ]
