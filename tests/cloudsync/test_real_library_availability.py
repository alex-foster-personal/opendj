"""Availability probing for the REAL-LIBRARY fixture tier.

These cases exercise :func:`library_is_available`, :func:`first_available` and
:func:`skip_unless_real_library` with real chmod-based permission denials.
They live here rather than in ``test_p0_naive_stamp.py`` so that file stays
under the 600-line ratchet.

[if] a candidate probe swallows operational errors or lies on 3.14 [then]
fail, [else stop].
"""
from __future__ import annotations

import errno
import inspect
import os
from pathlib import Path

import pytest

from tests.platform_capabilities import posix_permission_denial_supported

from . import real_library

_CAN_TEST_PERMISSION_DENIAL = posix_permission_denial_supported(
    os.name, getattr(os, "geteuid", None)
)


def _walled_off(directory: Path) -> Path:
    """A real file this process cannot reach, because its parent denies entry."""
    directory.mkdir()
    hidden = directory / "state.db"
    hidden.write_bytes(b"")
    directory.chmod(0o000)
    return hidden


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_an_unreadable_candidate_resolves_as_absent_instead_of_raising(
    tmp_path: Path,
) -> None:
    """An unreachable candidate must SKIP the tier, never error every test in it.

    ``Path.stat()`` / ``Path.open()`` raise EACCES for a candidate under a
    directory this process cannot traverse, where an absent path returns
    ENOENT. On 3.14 ``Path.is_file()`` returns False here, so the control uses
    ``stat()``.

    Drives the production selector with real paths rather than replacing
    module state, per AGENTS.md "No mocks and locked real fixtures".

    [if] an unreachable candidate makes the selector raise [then] fail,
    [else stop].
    """
    walled = tmp_path / "walled"
    unreachable = _walled_off(walled)
    try:
        with pytest.raises(PermissionError):
            unreachable.stat()

        readable = tmp_path / "readable.db"
        readable.write_bytes(b"")

        assert (
            real_library.first_available((unreachable, readable), unreachable)
            == readable
        )
        assert real_library.library_is_available(readable)
        assert not real_library.library_is_available(unreachable)
        assert (
            real_library.first_available((unreachable,), unreachable) == unreachable
        )
    finally:
        walled.chmod(0o755)


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_an_unreadable_file_in_a_readable_directory_is_not_available(
    tmp_path: Path,
) -> None:
    """Statting is the wrong question; the tier has to READ the file.

    ``Path.is_file()`` only stats, and a stat succeeds on a mode-0000 file
    whose directory IS traversable. Answering True there would select the
    candidate and defer the failure to ``shutil.copyfile`` inside the fixture,
    further from the cause and inside a session-scoped setup.

    [if] an unreadable file reads as available [then] fail, [else stop].
    """
    sealed = tmp_path / "sealed.db"
    sealed.write_bytes(b"")
    sealed.chmod(0o000)
    readable = tmp_path / "open.db"
    readable.write_bytes(b"")
    try:
        assert sealed.is_file(), "the control is inert unless stat still passes"
        assert not real_library.library_is_available(sealed)
        assert real_library.first_available((sealed, readable), sealed) == readable
    finally:
        sealed.chmod(0o600)


def test_an_operational_stat_failure_propagates_instead_of_skipping(
    tmp_path: Path,
) -> None:
    """A broken environment must not skip its way to green.

    Catching every ``OSError`` would answer "absent" for EIO on a failing
    disk, ESTALE on a dropped mount or EMFILE on an exhausted process, and the
    tier would report a clean skip while the runner was broken.

    Asserted as a PROPERTY of the errno set rather than by staging a failing
    disk, which no test can do honestly.

    [if] an operational errno is treated as merely absent [then] fail,
    [else stop].
    """
    src = inspect.getsource(real_library.library_is_available)
    assert "is_file" not in src
    assert ".exists(" not in src
    unavailable = real_library.UNAVAILABLE_ERRNOS
    for benign in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.EISDIR):
        assert benign in unavailable
    for operational in (errno.EIO, errno.ESTALE, errno.EMFILE, errno.ENFILE):
        assert operational not in unavailable


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_skip_unless_real_library_names_an_unreadable_default(
    tmp_path: Path,
) -> None:
    """When every candidate is unstatable the tier SKIPS, naming the path.

    [if] an unreadable default is skipped without naming the path [then] fail,
    [else stop].
    """
    walled = tmp_path / "walled"
    unreachable = _walled_off(walled)
    try:
        with pytest.raises(pytest.skip.Exception, match="not readable") as caught:
            real_library.skip_unless_real_library(unreachable)
        message = str(caught.value)
        assert str(unreachable) in message
        assert "PermissionError" in message
    finally:
        walled.chmod(0o755)


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="platform cannot create a real chmod-based permission denial",
)
def test_an_unreadable_env_override_errors_loud_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator who pointed at an unreadable file must not get a skip.

    [if] an unreadable env override is skipped instead of raising [then] fail,
    [else stop].
    """
    walled = tmp_path / "walled"
    target = _walled_off(walled)
    try:
        monkeypatch.setenv(real_library.REAL_LIBRARY_ENV_VAR, str(target))
        assert real_library.source_state_db() == target
        with pytest.raises(PermissionError):
            real_library.skip_unless_real_library(target)
    finally:
        walled.chmod(0o755)
