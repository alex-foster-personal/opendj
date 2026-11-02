"""The outside surface of ``apps.set_notes``: the router builder and the DDL entry point.

Standard library at import time, because the schema-drift gate imports this module and
may pull in no third-party package; FastAPI loads only when the router is built.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .schema import apply_set_notes_migrations

if TYPE_CHECKING:
    from fastapi import APIRouter

# The file the schema-drift scan classifies, and this package's DDL entry point beside it.
SCHEMA_FILE = "apps/set_notes/schema.py"

__all__ = ["SCHEMA_FILE", "apply_set_notes_migrations", "create_router"]


def create_router(write_guard: Callable[..., object]) -> APIRouter:
    from .routes import create_router as build_router

    return build_router(write_guard)
