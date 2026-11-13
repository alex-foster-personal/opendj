"""Daemon compatibility entrypoint for the public lighting clock router."""

from apps.lighting.http import create_clock_router as create_router

__all__ = ["create_router"]
