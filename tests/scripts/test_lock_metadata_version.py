"""scripts/lock_metadata_check.py: the root package VERSION, compared the way uv records
it. A static `[project] version` must equal the lock root's; a dynamic one (listed in
`project.dynamic`) is recorded as no version at all; a project with neither, or both, is a
file uv or the build backend rejects, so it is UNKNOWN rather than clean (Codex P2 on
#3763, round 19). The fixture pair and `_run` live in test_lock_metadata_check.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_marker_semantics import canonical_version
from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import LOCK, PYPROJECT, _run

VERSIONLESS_LOCK = LOCK.replace('version = "0.1.0"\nsource', "source")
DYNAMIC = PYPROJECT.replace('version = "0.1.0"', 'dynamic = ["version"]')
assert VERSIONLESS_LOCK != LOCK and DYNAMIC != PYPROJECT


def test_a_version_only_bump_without_uv_lock_is_stale(tmp_path: Path) -> None:
    """if [project].version moves and the lock's root package version does not then 1"""
    code, message = _run(tmp_path, PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"'))
    assert code == EXIT_STALE
    assert "[project] version: pyproject.toml '0.2.0', uv.lock root '0.1.0'" in message
    code, message = _run(tmp_path)
    assert code == EXIT_OK, message


def test_a_dynamic_version_matches_the_versionless_root_uv_records(tmp_path: Path) -> None:
    """`dynamic = ["version"]`: uv records the root with no version (measured: `Updated
    probe v0.1.0 -> (dynamic)`), a later bump of the dynamic value passes `uv lock
    --check`, and the lock left over from a static version reads stale; the static
    project against the versionless lock is stale the other way (measured)."""
    code, message = _run(tmp_path, DYNAMIC, VERSIONLESS_LOCK)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, DYNAMIC, LOCK)
    assert code == EXIT_STALE, message
    assert "[project] version: pyproject.toml dynamic, uv.lock root '0.1.0'" in message
    code, message = _run(tmp_path, PYPROJECT, VERSIONLESS_LOCK)
    assert code == EXIT_STALE, message
    assert "[project] version: pyproject.toml '0.1.0', uv.lock root none" in message


def test_a_missing_or_doubly_declared_version_is_unknown_not_clean(tmp_path: Path) -> None:
    """No `version` and no `dynamic` entry is a file uv refuses to parse; both at once
    is one the build backend refuses (`cannot be both statically defined and listed in
    field project.dynamic`); a `dynamic` that is not a list is a parse error too. None
    of those can read clean against any lock (measured, Codex P2 on #3763, round 19)."""
    for label, pyproject in (
        ("neither", PYPROJECT.replace('version = "0.1.0"\n', "")),
        (
            "both",
            PYPROJECT.replace('version = "0.1.0"', 'version = "0.1.0"\ndynamic = ["version"]'),
        ),
        ("dynamic not a list", PYPROJECT.replace('version = "0.1.0"', 'dynamic = "version"')),
    ):
        assert pyproject != PYPROJECT, label
        for lock in (LOCK, VERSIONLESS_LOCK):
            code, message = _run(tmp_path, pyproject, lock)
            assert code == EXIT_UNKNOWN, (label, message)
            assert "version" in message, (label, message)


@pytest.mark.parametrize(
    ("spelled", "canonical"),
    [
        ("01.026", "1.26"),
        ("v1.0", "1.0"),
        ("1.0.0-rc1", "1.0.0rc1"),
        ("1.0alpha", "1.0a0"),
        ("1.0.preview.2", "1.0rc2"),
        ("1.0-1", "1.0.post1"),
        ("1.0.rev2", "1.0.post2"),
        ("1.0-dev", "1.0.dev0"),
        ("0!1.0", "1.0"),
        ("2!1.0", "2!1.0"),
        ("1.0+Ubuntu_1", "1.0+ubuntu.1"),
        ("1.0+01", "1.0+1"),
        ("1.0+abc.007.x01", "1.0+abc.7.x01"),
        ("1.0.1", "1.0.1"),
    ],
)
def test_canonical_version_matches_pep_440_normalization(spelled: str, canonical: str) -> None:
    assert canonical_version(spelled) == canonical


def test_a_non_version_is_unknown() -> None:
    with pytest.raises(Exception, match="not a PEP 440 version"):
        canonical_version("latest")


def test_noncanonical_version_spellings_match_the_canonical_lock(tmp_path: Path) -> None:
    """if pyproject.toml spells `>=01.026`, `>=03.011` and `1.0.0-rc1` and uv wrote the
    canonical forms then 0: a fresh lock must never read stale over spelling"""
    pyproject = (
        PYPROJECT.replace('"numpy>=1.26"', '"numpy>=01.026"')
        .replace('requires-python = ">=3.11"', 'requires-python = ">=03.011"')
        .replace('version = "0.1.0"', 'version = "0.1.0-rc1"')
    )
    lock = LOCK.replace('version = "0.1.0"\nsource', 'version = "0.1.0rc1"\nsource')
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message


def test_an_unparseable_project_version_is_unknown_not_clean(tmp_path: Path) -> None:
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('version = "0.1.0"', 'version = "latest"'),
    )
    assert code == EXIT_UNKNOWN
    assert "not a PEP 440 version" in message


def test_a_non_string_version_or_dynamic_entry_is_unknown_not_clean(tmp_path: Path) -> None:
    """[if] `[project] version` is a TOML number (`version = 1.0`) or a `dynamic` entry
    is not a string [then] UNKNOWN: uv refuses the file ("invalid type: floating point
    `1.0`, expected a string", measured uv 0.8.17, Codex P2 on #3763, round 20), so
    stringifying `1.0` against a lock that records "1.0" must never read clean"""
    lock_1_0 = LOCK.replace('version = "0.1.0"\nsource', 'version = "1.0"\nsource')
    assert lock_1_0 != LOCK
    for pyproject, lock in (
        (PYPROJECT.replace('version = "0.1.0"', "version = 1.0"), lock_1_0),
        (PYPROJECT.replace('version = "0.1.0"', "version = 1.0"), LOCK),
        (PYPROJECT.replace('version = "0.1.0"', "dynamic = [1]"), VERSIONLESS_LOCK),
        (PYPROJECT.replace('version = "0.1.0"', 'dynamic = ["version", 1]'), VERSIONLESS_LOCK),
    ):
        assert pyproject != PYPROJECT
        code, message = _run(tmp_path, pyproject, lock)
        assert code == EXIT_UNKNOWN, (pyproject[:80], message)
