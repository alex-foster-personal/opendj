"""Shared constants and helpers for tree-read census tests.

Split from ``test_ci_tree_read_census.py`` for issue #10939.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import threading
import venv
from pathlib import Path

import pytest

from scripts import pytest_tree_read_census as plugin
from scripts.affected_tests import REPO
from scripts.build_reqs_json import FRAGMENTS_REL
from scripts.ci_tree_read_census import (
    APPROVED_READS,
    DECIDED_OFF_ONLY_ON,
    DECIDED_OFF_THE_LIST,
    FINISHED_RUN_FILE,
    Census,
    _children_that_never_checked_in,
    _children_the_run_never_checked,
    _exit_code,
    collectable_test_modules,
    compare,
    excused_modules,
    load_census,
    tracked_directories,
)
from scripts.ci_tree_read_census import main as census_main
from scripts.pytest_tree_read_census import OUT_DIR_ENV, SESSION_READS, directories_listed, tree_directory
from tests.scripts.tree_read_census_runs import run_with_the_plugin

HOT_CUE_GUARD = "(private path)"
# The root conftest builds the gitignored reqs.json from its per-id fragments at configure (issue #10541),
# before any module is collected: a listing of the fragments directory every session makes by design.
ROOT_CONFTEST_READS = frozenset({FRAGMENTS_REL})
TMP_ONLY = "tests/analysis_beatgrid/test_weights.py"  # imports from apps/, lists only tmp_path


EMPTY_PART: dict[str, object] = {"measured": [], "incomplete": [], "reads": {}, "collected": {}, "ran": {}}


def _snapshots(snapshot: str | None = None) -> dict[str, object]:
    """The controller's snapshot fields: the current checkout's, unless a test names another, and this platform."""
    taken = snapshot if snapshot is not None else plugin.checkout_snapshot()
    return {"snapshot": taken, "snapshot_at_end": taken, "platform": sys.platform}


def _git(root: Path, *args: str) -> None:
    """Fixture git, run the way the census runs its own: the git PATH resolves, named absolutely, without any
    inherited GIT_* variable (`git_output`)."""
    plugin.git_output("-c", "user.name=c", "-c", "user.email=c@c", *args, root=root)
