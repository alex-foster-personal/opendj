#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.34"]
# ///
"""Thin CLI wrapper: implementation lives in :mod:`apps.stems.r2_migration`."""

from __future__ import annotations

from apps.stems.r2_migration import *  # noqa: F403

if __name__ == "__main__":
    raise SystemExit(main())
