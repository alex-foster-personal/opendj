"""Regression coverage for package discovery around the waveform Rust build tree.

Cargo writes incremental artifacts below ``apps/webui/server/native/waveform/target``.
Setuptools namespace discovery must never treat those transient directories as Python
packages, because setuptools-rust can rewrite them during an editable install.

Discovery runs in an isolated build-tool environment on purpose. setuptools is a
requirements.txt-only entry (see ``INTENTIONAL_TXT_ONLY`` in
tests/test_dependency_contract.py), so importing it here would either fail in a
uv-synced venv or force a pyproject declaration that breaks that contract.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
WAVEFORM_PACKAGE = "apps.webui.server.native.waveform"
WAVEFORM_TARGET_PACKAGE = f"{WAVEFORM_PACKAGE}.target"


def _package_discovery_config() -> dict[str, list[str]]:
    """Read the package discovery patterns from the project's actual config."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["packages"]["find"]


def _discover_packages(root: Path, config: dict[str, list[str]]) -> list[str]:
    """Run setuptools discovery in its isolated build-tool environment."""
    discovery_script = """
import json
import sys
from setuptools.discovery import PEP420PackageFinder

print(json.dumps(PEP420PackageFinder.find(
    where=sys.argv[1], include=json.loads(sys.argv[2]), exclude=json.loads(sys.argv[3]),
)))
"""
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--with",
            "setuptools",
            "python",
            "-c",
            discovery_script,
            str(root),
            json.dumps(config["include"]),
            json.dumps(config["exclude"]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _build_waveform_tree(root: Path) -> None:
    """A miniature of the real tree: the shipped package beside Cargo's target dir."""
    waveform = root / "apps/webui/server/native/waveform"
    incremental = (
        waveform / "target/debug/incremental" / "_rb_waveform_native-abc" / "s-build-artifact"
    )
    incremental.mkdir(parents=True)
    (waveform / "__init__.py").write_text("", encoding="utf-8")


def test_waveform_rust_target_is_excluded_from_setuptools_discovery(
    tmp_path: Path,
) -> None:
    """Cargo target directories never become namespace packages during discovery."""
    _build_waveform_tree(tmp_path)

    discovered = _discover_packages(tmp_path, _package_discovery_config())

    assert not any(package.startswith(WAVEFORM_TARGET_PACKAGE) for package in discovered), (
        f"setuptools discovered Cargo build artifacts as packages: {discovered}"
    )


def test_excluding_the_cargo_target_keeps_the_waveform_package_itself(
    tmp_path: Path,
) -> None:
    """The exclude must be surgical, and this is the probe that proves it ran.

    The sibling test asserts an absence, so on its own it stays green for the
    wrong reasons: discovery returning nothing at all passes it, and so does an
    exclude widened to ``apps.webui.server.native.waveform*``, which would
    quietly drop the real extension module from the built wheel. Asserting the
    package survives is what makes both of those go red.
    """
    _build_waveform_tree(tmp_path)

    discovered = _discover_packages(tmp_path, _package_discovery_config())

    assert WAVEFORM_PACKAGE in discovered, (
        f"the Cargo target exclude also removed the shipped package "
        f"{WAVEFORM_PACKAGE!r}, which would ship a wheel without the native "
        f"waveform module. Discovered: {discovered}"
    )
