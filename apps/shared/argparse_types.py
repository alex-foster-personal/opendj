"""Shared :mod:`argparse` type converters."""
from __future__ import annotations

import argparse


def nonnegative_int(raw_value: str) -> int:
    """Parse a nonnegative CLI integer without Python slice semantics."""
    value = int(raw_value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value
