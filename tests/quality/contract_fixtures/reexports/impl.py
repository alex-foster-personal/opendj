"""The package's implementation side, which its contract must not hand out."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Engine:
    name: str
