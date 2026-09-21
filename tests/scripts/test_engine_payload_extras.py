"""OMITTED_OPTIONAL_EXTRAS: the build's audit of every extra it never requests.

Split out of test_engine_payload.py, which crossed the 600-line file-size
ratchet when this section landed and turned trunk red. Registered in
macos-packaging.yml's PACKAGING_TESTS alongside it; the ubuntu fast lane
collects all of tests/ and needs no registration.

- if a lock without any omitted extra reads as remarkable, the steady state
  itself is broken and every real build would fail on nothing -> broken.
- if a core dependency that is ALSO named by an extra reads as that extra
  leaking in, every real build trips on its own hard dependencies -> broken.
- if a package only a core dep pulls in (httpx2 via mcp) reads as its extra
  leaking in, every dmg build from main fails on nothing -> broken.
- if the project itself pulls in an extra's package and that is excused
  because a core dep also needs it, a requested extra ships unaudited -> broken.
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
from unittest import mock

import pytest

import scripts.build_engine_payload as payload_build
from scripts.build_engine_payload import (
    NEVER_SHIP,
    OMITTED_OPTIONAL_EXTRAS,
    REQUESTED_OPTIONAL_EXTRAS,
    PayloadBuildError,
    _assert_never_ship_absent,
    _requirement_name,
    _verify_omitted_extras,
    locked_requirements,
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
def test_a_transitive_core_dependency_also_named_by_an_extra_is_not_a_false_positive() -> None:
    """httpx2 is in "dev" AND required by mcp 2.x, a core dep. Reached only
    through mcp it is core closure, not the extra leaking; flagging it failed
    every dmg build from main at f04dc8a82 (Tue 15 Sep 2026)."""
    lock = LOCK_SAMPLE + "httpx2==2.13.0\n    # via mcp\n"
    _verify_omitted_extras(parse_locked_export(lock), PYPROJECT)


@pytest.mark.requirement("INSTALL-12")
def test_an_extra_package_the_project_pulls_in_still_fails_even_if_a_core_dep_needs_it() -> None:
    """Control for the test above: when the export also shows the project
    itself requiring httpx2, the dev extra WAS requested, so the build stops."""
    lock = LOCK_SAMPLE + "httpx2==2.13.0\n    # via\n    #   mcp\n    #   music-dj-tools\n"
    with pytest.raises(PayloadBuildError) as excinfo:
        _verify_omitted_extras(parse_locked_export(lock), PYPROJECT)
    assert "httpx2" in str(excinfo.value)
    assert "dev" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_the_real_locked_export_passes_the_extras_audit() -> None:
    """The steady state on the real lock: the audit the dmg build runs must
    accept the export this repo actually produces."""
    entries, _dropped = locked_requirements(REPO_ROOT)
    assert "httpx2" in {entry.name for entry in entries}


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
    assert declared == set(OMITTED_OPTIONAL_EXTRAS) | set(REQUESTED_OPTIONAL_EXTRAS)
    # An extra can only be in one register. Both at once would let a group
    # satisfy this equality while the two halves of the audit disagree about
    # whether it ships.
    assert not (set(OMITTED_OPTIONAL_EXTRAS) & set(REQUESTED_OPTIONAL_EXTRAS))


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


@pytest.mark.requirement("NATIVE-08")
def test_madmom_is_declared_nowhere_uv_export_could_read_it_from() -> None:
    """[if] `uv export` (this build's only source of the locked closure,
    never requirements.txt) is asked for pyproject.toml's core dependencies
    plus every optional-dependencies group [then] "madmom" is absent from
    the names it could resolve, [else stop] -- a shipped payload can only
    ever include what pyproject.toml declares, so this is a structural
    guarantee rather than a snapshot of one `uv export` run."""
    declared_extras: dict[str, list[str]] = PYPROJECT["project"]["optional-dependencies"]
    core_names = {_requirement_name(r) for r in PYPROJECT["project"]["dependencies"]}
    extra_names = {
        _requirement_name(r) for reqs in declared_extras.values() for r in reqs
    }
    assert "madmom" not in core_names
    assert "madmom" not in extra_names
    # control: librosa DOES live in the "analysis" extra, so this probe can
    # find a real package and is not just returning an empty set by accident.
    assert "librosa" in extra_names


@pytest.mark.requirement("NATIVE-08")
def test_madmom_is_absent_from_the_real_resolved_locked_closure() -> None:
    """[if] the checked-in test above only reads pyproject.toml's flat
    declared strings [then] it could stay green even if madmom arrived
    through a transitive dependency of some other, legitimately-requested
    package [else stop] -- so this probe instead runs the real `uv export`
    this build actually installs from (locked_requirements) and inspects
    the resolved closure, not a subset of it."""
    entries, _dropped = locked_requirements(REPO_ROOT)
    names = {entry.name for entry in entries}
    assert "madmom" not in names
    # control: numpy is a real core dependency, present in every resolved
    # closure regardless of extras, so this probe is not just reading an
    # empty export by accident.
    assert "numpy" in names


@pytest.mark.requirement("NATIVE-08")
def test_a_madmom_line_in_the_lock_fails_the_build() -> None:
    """Even if madmom slipped into a future export -- a stray dependency, a
    renamed extra, a transitive pull-in -- the build must reject it
    explicitly rather than rely solely on it never being declared."""
    lock = LOCK_SAMPLE + "madmom==0.17.dev0\n    # via music-dj-tools\n"
    with pytest.raises(PayloadBuildError) as excinfo:
        _assert_never_ship_absent(parse_locked_export(lock))
    assert "madmom" in str(excinfo.value)
    assert "CC BY-NC-SA" in str(excinfo.value)


@pytest.mark.requirement("NATIVE-08")
def test_never_ship_states_why_each_package_is_permanently_unshippable() -> None:
    for name, reason in NEVER_SHIP.items():
        assert len(reason) > 40, f"{name} has no real justification recorded"


# ----- REQUESTED_OPTIONAL_EXTRAS: the presence half ----------------------
# The omission half above asks "did something we never requested sneak in".
# That question has no answer for an extra we DO request, and the failure it
# cannot see is the one measured live on the shipped build Wed 16 Sep 2026:
# librosa absent, so every folder import landed with no BPM, no key and no
# beatgrid and nothing in the build said a word.


def _lock_without(lock: str, *packages: str) -> str:
    """Drop whole ``name==ver`` + ``# via`` stanzas from an export sample."""
    kept: list[str] = []
    dropping = False
    for line in lock.splitlines(keepends=True):
        if line.startswith((" ", "#")):
            if not dropping:
                kept.append(line)
            continue
        dropping = _requirement_name(line) in packages
        if not dropping:
            kept.append(line)
    return "".join(kept)


@pytest.mark.requirement("INSTALL-25")
def test_a_requested_extra_missing_from_the_lock_fails_the_build() -> None:
    """The mutation this whole register exists to catch: the export stops
    carrying librosa and the installed app silently goes back to analyzing
    nothing."""
    lock = _lock_without(LOCK_SAMPLE, "librosa")
    with pytest.raises(PayloadBuildError) as excinfo:
        _verify_omitted_extras(parse_locked_export(lock), PYPROJECT)
    assert "librosa" in str(excinfo.value)
    assert "analysis" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-25")
def test_the_helper_that_strips_a_package_really_strips_it() -> None:
    """Control for the test above. A stripper that silently changed nothing
    would make that test pass for the wrong reason, on a lock that still
    holds librosa."""
    stripped = _lock_without(LOCK_SAMPLE, "librosa")
    assert "librosa" in {e.name for e in parse_locked_export(LOCK_SAMPLE)}
    assert "librosa" not in {e.name for e in parse_locked_export(stripped)}
    # and it strips ONLY what it was asked for
    assert "scipy" in {e.name for e in parse_locked_export(stripped)}


@pytest.mark.requirement("INSTALL-25")
def test_a_requested_extra_present_in_the_lock_is_accepted() -> None:
    """The opposite mutation: a guard that fired on a lock that DOES carry
    the extra would fail every real build."""
    _verify_omitted_extras(parse_locked_export(LOCK_SAMPLE), PYPROJECT)


@pytest.mark.requirement("INSTALL-25")
def test_the_real_locked_export_carries_the_analysis_extra() -> None:
    """Against the export the dmg build actually installs from, not a sample."""
    entries, _dropped = locked_requirements(REPO_ROOT)
    project_pulled = {e.name for e in entries if "music-dj-tools" in e.via}
    assert {"librosa", "scipy", "soundfile"} <= project_pulled
    # Negative control: an extra this build still omits must read as absent,
    # or "present" is what this probe says about everything.
    assert "mutagen" not in {e.name for e in entries}


@pytest.mark.requirement("OBS-04")
def test_the_real_locked_export_carries_the_observability_extra() -> None:
    """The dmg ships sentry-sdk (OBS-04). Against the real export, so a lock
    that quietly stops carrying it fails here rather than on a tester's Mac."""
    entries, _dropped = locked_requirements(REPO_ROOT)
    project_pulled = {e.name for e in entries if "music-dj-tools" in e.via}
    assert "sentry-sdk" in project_pulled
    assert "observability" in REQUESTED_OPTIONAL_EXTRAS
    assert "observability" not in OMITTED_OPTIONAL_EXTRAS


@pytest.mark.requirement("INSTALL-25")
def test_an_extra_registered_as_both_omitted_and_requested_is_refused() -> None:
    both = dict(OMITTED_OPTIONAL_EXTRAS)
    both["analysis"] = "x" * 100
    with (
        mock.patch.object(payload_build, "OMITTED_OPTIONAL_EXTRAS", both),
        pytest.raises(PayloadBuildError) as excinfo,
    ):
        _verify_omitted_extras(parse_locked_export(LOCK_SAMPLE), PYPROJECT)
    assert "analysis" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-25")
def test_every_requested_extra_states_why_it_ships() -> None:
    for name, reason in REQUESTED_OPTIONAL_EXTRAS.items():
        assert len(reason) > 80, f"{name} has no real justification recorded"
