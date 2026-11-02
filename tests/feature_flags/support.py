"""Shared fixtures for feature-flag tests.

Keeps ``FlagDef`` test registries and JSON writers in one place so
``tests/entitlements/test_flags.py`` and ``tests/engine_core/test_account_api.py``
do not drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.feature_flags import FLAGS_FILE_ENV, FLAGS_FILENAME, FlagDef
from apps.feature_flags.profiles import BUILD_PROFILE_ENV

TEST_DEFS: tuple[FlagDef, ...] = (
    FlagDef(
        flag_id="example.off_by_default",
        default=False,
        owner="tests",
        note="exists only in the shared test registry",
        retire_by="never - test fixture",
        sandbox_gated=False,
    ),
    FlagDef(
        flag_id="example.on_by_default",
        default=True,
        owner="tests",
        note="exists only in the shared test registry",
        retire_by="never - test fixture",
        sandbox_gated=False,
    ),
)

FLAG_SELECTION_ENV_VARS: tuple[str, ...] = (BUILD_PROFILE_ENV, FLAGS_FILE_ENV)


def write_flags_file(data_dir: Path, payload: object) -> Path:
    """Write ``payload`` to the data dir's standard flags filename."""
    path = data_dir / FLAGS_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def clear_flag_selection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that change which flags file or profile is selected."""
    for var in FLAG_SELECTION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
