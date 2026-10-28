"""Closed frozen types whose fields cannot cross a boundary."""

from __future__ import annotations

import collections.abc as _abc
import sqlite3 as _sqlite3
import typing as _typing
from dataclasses import dataclass, field
from typing import Any as _Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

CONTRACT_VERSION = "open_fields/1"


@dataclass(frozen=True)
class HoldsConnection:
    conn: _sqlite3.Connection


class ModelWithAny(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: list[_Any]


@dataclass(frozen=True)
class BareContainers:
    rows: _typing.List  # noqa: UP006  # unparameterized on purpose: list[Any]
    empty: tuple[()]  # control: an empty tuple is closed


class Defaulted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conn: str = _sqlite3.connect(":memory:")  # type: ignore[assignment]  # neither Pydantic nor mypy stops it at runtime
    name: str = "control"
    rows: list[str] = Field(default_factory=lambda: [_sqlite3.connect(":memory:")])  # type: ignore[arg-type]


@dataclass(frozen=True)
class Factoried:
    rows: tuple[str, ...] = field(default_factory=lambda: (_sqlite3.connect(":memory:"),))  # type: ignore[arg-type,return-value]
    tags: tuple[str, ...] = field(default_factory=tuple)


class DataAware(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = "control"
    label: str = Field(default_factory=lambda data: data["name"].upper())


class Mutables(BaseModel):
    # Sol, PR #6182: frozen=True stops assignment, not a list or dict changed in place. Pydantic
    # keeps a Sequence or Mapping as the list or dict it was handed, so those are refused too.
    model_config = ConfigDict(frozen=True, extra="forbid")
    seq: _abc.Sequence[str]
    table: _abc.Mapping[str, int]
    maybe: list[str] | None
    nested: tuple[set[str], ...]
    kept: tuple[str, ...]  # control: a tuple cannot change
    tags: frozenset[str]  # control
    payload: JsonValue  # Sol, PR #6182: an alias that expands to lists and dicts among the rest
    payloads: tuple[JsonValue | None, ...]
    notes: _typing.Annotated[tuple[int, ...], "control"]


@dataclass(frozen=True)
class MutableRecord:
    by_name: dict[str, int]
    labels: _typing.Annotated[list[str], "a dataclass keeps Annotated on the field"]
    kept: _typing.Annotated[tuple[str, ...], "control"]
