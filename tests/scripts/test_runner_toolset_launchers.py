"""The launcher table's value-taking options against the PINNED uv and pnpm.

Split out of tests/scripts/test_runner_toolset_complete.py. The reference is the
tool the toolset pins, never whatever a host happens to carry: pnpm runs at the
packageManager pin through corepack with the network off, and a host uv is
compared only inside the version range the table was verified against. A host
that cannot provide the pinned tool reports UNKNOWN (a skip naming why): that is
a toolset question for the host verifier (fleet-af runner_toolset/verify.py), not a table mismatch,
and it must never turn a PR red on one runner and green on the next.

Regression lines:
  - if a value-taking `uv run` / `pnpm exec` option the pinned tool's help lists
    is missing from the launcher table, or a boolean one is listed, then broken
  - if the pnpm check reads the host's own pnpm (corepack's default, a separate
    install) instead of the packageManager pin then broken
  - if a uv outside the table's verified range is compared instead of reported
    UNKNOWN then broken
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import runner_toolset_shell_lex as lex
from scripts.runner_toolset_scan import REPO_ROOT

FRONTEND_PACKAGE = REPO_ROOT / "apps" / "webui" / "frontend" / "package.json"
OFFLINE_COREPACK = {"COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
# An option at the option column (a deeper indent is description text), then either
# one space and a value (`<PATTERN>`, `!<selector>`, `.`) or the description gap.
OPTION_LINE_RE = re.compile(r"^ {2,8}((?:-\w, )?--[\w\[\]-]+)( \S)?")


def _help_options(help_text: str) -> tuple[set[str], set[str]]:
    """(value-taking, boolean) option names in a clap- or pnpm-style help listing."""
    value: set[str] = set()
    boolean: set[str] = set()
    for line in help_text.splitlines():
        found = OPTION_LINE_RE.match(line)
        if found:
            names = {name.strip() for name in found.group(1).split(",")}
            (value if found.group(2) else boolean).update(names)
    return value, boolean


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, f"{argv} exited {proc.returncode}: {proc.stderr[-400:]}"
    return proc.stdout


def pinned_pnpm() -> str:
    manager = json.loads(FRONTEND_PACKAGE.read_text(encoding="utf-8"))["packageManager"]
    name, _, version = manager.partition("@")
    assert name == "pnpm" and version, f"{FRONTEND_PACKAGE}: packageManager {manager!r}"
    return version


def pnpm_help_at_the_pin(tmp_path: Path) -> str:
    """`pnpm help exec` from the pinned pnpm, whatever pnpm the host defaults to."""
    pin = pinned_pnpm()
    if not shutil.which("corepack"):
        pytest.skip(f"UNAVAILABLE: no corepack here to run the pinned pnpm@{pin}")
    env = {**os.environ, **OFFLINE_COREPACK}
    probe = subprocess.run(
        ["corepack", f"pnpm@{pin}", "--version"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    if probe.returncode:
        pytest.skip(
            f"UNKNOWN: this host cannot run pnpm@{pin} offline ({probe.stderr.strip()[-200:]});"
            " provisioning it is the `pnpm` entry of ci/runner-toolset.yml"
        )
    assert probe.stdout.strip() == pin, f"corepack ran pnpm {probe.stdout.strip()!r}, not {pin}"
    return _run(["corepack", f"pnpm@{pin}", "help", "exec"], tmp_path, env)


def uv_help_in_the_verified_range(tmp_path: Path) -> str:
    """`uv run --help` from a host uv the table was verified against, else UNKNOWN."""
    if not shutil.which("uv"):
        pytest.skip("UNAVAILABLE: uv is not installed here")
    host = _run(["uv", "--version"], tmp_path).strip()
    low, high = (_version(v) for v in lex.UV_RUN_VALUE_OPTIONS_VERIFIED)
    if not low <= _version(host) <= high:
        pytest.skip(
            f"UNKNOWN: {host} is outside the uv versions the table was verified against"
            f" {lex.UV_RUN_VALUE_OPTIONS_VERIFIED}; re-derive it from `uv run --help`"
        )
    return _run(["uv", "run", "--help"], tmp_path)


@pytest.mark.parametrize(
    ("read_help", "declared"),
    [
        (uv_help_in_the_verified_range, lex.UV_RUN_VALUE_OPTIONS),
        (pnpm_help_at_the_pin, lex.PNPM_EXEC_VALUE_OPTIONS),
    ],
    ids=["uv-run", "pnpm-exec"],
)
def test_launcher_value_options_match_the_pinned_tools_own_help(
    tmp_path: Path, read_help: Callable[[Path], str], declared: frozenset[str]
) -> None:
    """A missing value-taking option makes its value the launched command; a boolean
    one listed as value-taking swallows the command."""
    value, boolean = _help_options(read_help(tmp_path))
    assert value and boolean, "parsed nothing from the help: the parser, not the tool, broke"
    assert not value - declared, f"missing value-taking options: {sorted(value - declared)}"
    assert not declared & boolean, (
        f"boolean options listed as value-taking: {sorted(declared & boolean)}"
    )
