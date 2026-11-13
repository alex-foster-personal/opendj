"""Values contract leak fixtures: shared live connection."""

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


_LIVE = _sqlite3.connect(":memory:")
