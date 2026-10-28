"""Everything here crosses a module boundary, so the checker reports nothing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final as Final  # tooling re-exported under its own name
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from tests.quality.contract_fixtures.shared.contract import Shared

from . import sub

CONTRACT_VERSION = "clean/1"
LIMIT = 3
Lane = Literal["a", "b"]
Err = KeyError

__all__ = ["CONTRACT_VERSION", "Err", "Lane", "Shared", "sub"]


class Good(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    x: int


@dataclass(frozen=True)
class Value:
    x: int

    def doubled(self) -> int:
        return self.x * 2


class Mode(Enum):
    FAST = "fast"


class NotFoundError(ValueError):
    """An exception declared in the contract."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class _Node:
    """Private and self-referencing: read through once, and clean."""

    label: str
    children: tuple[_Node, ...]


@dataclass(frozen=True)
class Tree:
    root: _Node


@dataclass(frozen=True)
class Picked:
    mode: Literal[Mode.FAST]


DEFAULTS = {"lanes": ("a", "b"), "root": Path("x"), "good": Good(x=1), "mode": Mode.FAST}


class Service(Protocol):
    def ok(
        self, path: Path, ids: list[int], mode: Literal["a", "b"], v: Value | None, payload: dict[str, JsonValue]
    ) -> tuple[Good, ...]: ...

    @property
    def db(self) -> Path: ...

    @db.setter
    def db(self, value: Path) -> None: ...

    def __repr__(self) -> str: ...


def reads(path: Path) -> list[Good]:
    return [Good(x=len(path.name))]


def _helper(path: Path) -> int:  # a private helper is read like a public one, and this one is clean
    return len(path.name)
