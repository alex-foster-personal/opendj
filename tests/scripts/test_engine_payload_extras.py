"""OMITTED_OPTIONAL_EXTRAS: the build's audit of every extra it never requests.

Split out of test_engine_payload.py, which crossed the 600-line file-size
ratchet when this section landed and turned trunk red. Registered in
macos-packaging.yml's PACKAGING_TESTS alongside it; the ubuntu fast lane
collects all of tests/ and needs no registration.

- if a lock without any omitted extra reads as remarkable, the steady state
  itself is broken and every real build would fail on nothing -> broken.
- if a core dependency that is ALSO named by an extra reads as that extra
  leaking in, every real build trips on its own hard dependencies -> broken.
- if the registry narrows to only the extra a bug report happened to name,
  the other omitted extras stay unaudited -> broken.
- if pyproject.toml grows or renames an [extras] group nobody added to the
  registry, that omission ships unaudited and unnoticed -> broken.
- if a registry entry outlives the pyproject extra it describes, a stale
  claim nobody re-checked stands in for a real audit -> broken.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from scripts.build_engine_payload import (
    OMITTED_OPTIONAL_EXTRAS,
    PayloadBuildError,
    _verify_omitted_extras,
    parse_locked_export,
)
from tests.scripts.test_engine_payload import LOCK_SAMPLE

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
PYPROJECT: dict[str, Any] = tomllib.loads(
    (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)


@pytest.mark.requirement("INSTALL-12")
def test_a_lock_without_the_omitted_extras_is_unremarkable() -> None:
    _verify_omitted_extras(parse_locked_export(LOCK_SAMPLE), PYPROJECT)


@pytest.mark.requirement("INSTALL-12")
def test_a_core_dependency_also_named_by_an_extra_is_not_a_false_positive() -> None:
    """numpy is a core dep AND part of "analysis"; its presence alone must
    not read as the extra leaking in, or every real build trips on itself."""
    lock = LOCK_SAMPLE + "numpy==1.26.4\n    # via music-dj-tools\n"
    _verify_omitted_extras(parse_locked_export(lock), PYPROJECT)


@pytest.mark.requirement("INSTALL-12")
@pytest.mark.parametrize(
    "package,spec,extra",
    [("mutagen", "mutagen==1.47.0", "tags"), ("boto3", "boto3==1.34.0", "cloud")],
)
def test_an_omitted_extras_package_in_the_lock_fails_the_build(
    package: str, spec: str, extra: str
) -> None:
    """Any audited extra's package leaking into the export must be caught,
    not just mutagen/tags -- issue #795's registry must not narrow to one."""
    lock = LOCK_SAMPLE + f"{spec}\n    # via music-dj-tools\n"
    with pytest.raises(PayloadBuildError) as excinfo:
        _verify_omitted_extras(parse_locked_export(lock), PYPROJECT)
    assert package in str(excinfo.value)
    assert extra in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_every_pyproject_extra_except_all_is_audited() -> None:
    """A new or renamed [extras] group nobody added here must fail loudly.

    "dev" is audited too: --no-dev disables uv's dev-dependencies/
    dependency-groups mechanism, not a PEP 621 extra merely named "dev"."""
    declared = set(PYPROJECT["project"]["optional-dependencies"]) - {"all"}
    assert declared == set(OMITTED_OPTIONAL_EXTRAS)


@pytest.mark.requirement("INSTALL-12")
def test_a_dev_extra_package_in_the_lock_fails_the_build() -> None:
    """The exact gap Codex found live on PR #833: --no-dev does not exclude
    the PEP 621 "dev" extra, so a stray --extra dev must still be caught."""
    lock = LOCK_SAMPLE + "pytest==8.4.2\n    # via music-dj-tools\n"
    with pytest.raises(PayloadBuildError) as excinfo:
        _verify_omitted_extras(parse_locked_export(lock), PYPROJECT)
    assert "pytest" in str(excinfo.value)
    assert "dev" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_a_registry_entry_pyproject_no_longer_defines_fails_the_build() -> None:
    stale_pyproject = {
        "project": {"optional-dependencies": {"tags": ["mutagen>=1.47,<2"]}}
    }
    with pytest.raises(PayloadBuildError) as excinfo:
        _verify_omitted_extras(parse_locked_export(LOCK_SAMPLE), stale_pyproject)
    assert "analysis" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_every_omitted_extra_states_why_it_is_safe() -> None:
    for name, reason in OMITTED_OPTIONAL_EXTRAS.items():
        assert len(reason) > 80, f"{name} has no real justification recorded"
