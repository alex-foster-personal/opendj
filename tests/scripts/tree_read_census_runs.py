"""Runs a pytest session under the tree-read census plugin, shared by the census test modules."""

from __future__ import annotations

import subprocess
import sys
import tempfile

from scripts.affected_tests import REPO


def run_with_the_plugin(targets: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as basetemp:  # its own: concurrent nested runs prune a shared one's links
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--basetemp={basetemp}",
                "-p",
                "scripts.pytest_tree_read_census",
                # The probe sessions leave children running on purpose (the census measures
                # what they list after the run), so #8630's leak gate stays out of them.
                "-p",
                "no:tests.support.process_leaks",
                *targets,
            ],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
