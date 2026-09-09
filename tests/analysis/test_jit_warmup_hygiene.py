"""Lock hygiene for the serialized numba JIT-cache warm-up (issue #1401).

The compile lock used to live at a fully predictable path in the world-writable
temp root, opened with no ``O_NOFOLLOW`` and then unconditionally ``fchmod``ed
to 0o666. That let a local user pre-plant a symlink at the lock path (an
arbitrary-file chmod-to-world-writable when the victim ran as root) or a file
the victim could not open (which made every warm-up run UNLOCKED - the exact
condition ``jit_warmup`` documents as corrupting the numba cache permanently).

The fix and these tests: the lock and stamp live in a private 0700 directory
(``mdt-numba-warmup-<key>`` under the temp root), or in an operator-controlled
directory named by ``MDT_NUMBA_WARMUP_DIR``; the lock file is opened with
``O_NOFOLLOW`` and used only when ``fstat`` says it is a regular file (owned by
this user unless the operator directory is in play); there is no unconditional
``fchmod``. A warm-up that cannot establish a trustworthy lock is REFUSED and
compiles NOTHING - that is the whole point of refusing, and every test asserts
it.

Why a separate file: these are the #1401 guards, distinct from the #1316 guards
in ``test_jit_warmup.py``, and keeping them apart keeps both files under the
repo's 600-line ceiling. CI's "JIT cache guards" step runs both.

Regression lines:
  - if a symlink at the lock path is followed, or a non-regular file is
    flocked, then the #1401 arbitrary-file chmod is back
  - if a warm-up that cannot open a safe lock compiles anyway then the planted
    unopenable-lock attack has turned the lock into an unlocked warm-up
  - if a hostile or missing lock directory is adopted or created then the
    module has guessed at where cross-user sharing should live again
  - if a healthy single-user warm-up is refused, or still fchmods the lock file
    to 0o666, then the fix over-corrected or missed its own primitive
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.analysis import _warmup_lock, jit_warmup
from apps.analysis.jit_warmup import warm_backend_jit, warmup_lock_path


class _CompileCounter:
    """Counts real warm-ups against no cache roots, so refusal is observable.

    ``jit_cache_roots`` is empty, so the skip fast path can never fire and
    every call really reaches the lock. The count is the one honest signal:
    a refused warm-up must compile nothing, and an unlocked one must not be
    able to pretend it locked.
    """

    compiles = 0

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        _CompileCounter.compiles += 1
        return f"compiled {_CompileCounter.compiles}"


def _hermetic(monkeypatch: pytest.MonkeyPatch, lock: Path) -> None:
    """Point the lock and stamp at ``lock``/``lock.parent/stamp`` under tmp_path."""
    monkeypatch.setattr(jit_warmup, "warmup_lock_path", lambda: lock)
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: lock.parent / "stamp")


@pytest.mark.requirement("JIT-06")
def test_a_symlink_at_the_lock_path_is_refused_and_its_target_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a symlink at the lock path is followed or chmodded [then] fail, [else stop].

    the issue's named control: point the lock at a symlink and assert the walk
    refuses rather than following it. Following it is the #1401 primitive - an
    arbitrary-file chmod when the victim is root - and compiling unlocked is
    the cache corruption this module exists to prevent"""
    target = tmp_path / "victim"
    target.write_text("do not touch", encoding="utf-8")
    target.chmod(0o600)
    lock = tmp_path / "mdt-numba-warmup.lock"
    os.symlink(target, lock)
    _hermetic(monkeypatch, lock)
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert _CompileCounter.compiles == 0, (
        "a warm-up ran after the lock path turned out to be a symlink; it must "
        "be refused, not run unlocked"
    )
    assert result.refused, "a symlinked lock must refuse the warm-up, not run it"
    assert (target.stat().st_mode & 0o777) == 0o600, (
        "the symlink was followed and the target fchmod'ed to 0o666 - the "
        "#1401 arbitrary-file chmod primitive"
    )
    assert target.read_text(encoding="utf-8") == "do not touch"


@pytest.mark.requirement("JIT-06")
def test_a_non_regular_file_at_the_lock_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a FIFO or device at the lock path is flocked and used [then] fail, [else stop].

    if a non-regular file passed the open then the fstat check the issue calls
    for - demand a regular file with an expected owner - is not doing its job"""
    lock = tmp_path / "mdt-numba-warmup.lock"
    os.mkfifo(lock)
    _hermetic(monkeypatch, lock)
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert _CompileCounter.compiles == 0
    assert result.refused, "a lock file that is not a regular file must refuse"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions; the FIFO test covers this branch",
)
@pytest.mark.requirement("JIT-06")
def test_a_planted_unopenable_lock_file_refuses_instead_of_warming_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a planted unopenable lock file yields an unlocked warm-up [then] fail, [else stop].

    the issue's cheaper attack: a planted 0600 file makes os.open fail EACCES,
    which used to log and warm UNLOCKED - the state that corrupts the cache.
    Failing closed here is part of the fix, not an extra"""
    lock = tmp_path / "mdt-numba-warmup.lock"
    lock.touch()
    lock.chmod(0o000)
    _hermetic(monkeypatch, lock)
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert _CompileCounter.compiles == 0, (
        "an unopenable lock file made the warm-up run UNLOCKED; it must be refused"
    )
    assert result.refused, "an unopenable lock file must refuse the warm-up"


@pytest.mark.requirement("JIT-06")
def test_a_lock_directory_that_is_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a symlink planted as the private lock directory is adopted [then] fail, [else stop].

    the private 0700 directory is the new security boundary; a symlink there
    means the directory is not ours and no trustworthy lock exists, so the
    warm-up must be refused rather than run through it"""
    real = tmp_path / "real"
    real.mkdir()
    planted = tmp_path / "lockdir"
    os.symlink(real, planted)
    lock = planted / "mdt-numba-warmup.lock"
    _hermetic(monkeypatch, lock)
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert _CompileCounter.compiles == 0
    assert result.refused, "a lock directory we do not own must refuse the warm-up"
    assert not (real / "stamp").exists(), "nothing may be written through the symlink"


@pytest.mark.requirement("JIT-06")
def test_a_healthy_private_lock_directory_still_warms_and_never_chmods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the hardened lock refuses a healthy single-user warm-up [then] fail, [else stop].

    the control against over-correcting: the fix must reject hostile paths, not
    the normal case, or every warm-up on a clean host stops compiling. The lock
    file is pre-created 0600 so the no-fchmod claim is checked against a real
    byte, independent of umask"""
    lock_dir = tmp_path / "warmup"
    lock_dir.mkdir(mode=0o700)
    lock = lock_dir / "mdt-numba-warmup.lock"
    lock.touch()
    lock.chmod(0o600)
    _hermetic(monkeypatch, lock)
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert not result.refused, "a healthy lock must not refuse"
    assert result.lock_held, "a healthy warm-up must hold the lock"
    assert _CompileCounter.compiles == 1, "a healthy warm-up must still compile"
    assert (lock.stat().st_mode & 0o777) == 0o600, (
        "the lock file was fchmod'ed (to 0o666); the unconditional fchmod is "
        "the #1401 primitive and must be gone"
    )


@pytest.mark.requirement("JIT-06")
def test_the_operator_lock_directory_opt_in_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] MDT_NUMBA_WARMUP_DIR is ignored, or auto-created when absent [then] fail, [else stop].

    cross-user sharing is the module's original reason for a shared lock, and
    it survives only as an explicit opt-in: the operator names the directory,
    so it must be used as-is and pre-exist, never guessed at"""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    monkeypatch.setenv(jit_warmup.LOCK_DIR_ENV, str(shared))
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: shared / "stamp")
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert not result.refused
    assert _CompileCounter.compiles == 1
    assert warmup_lock_path().parent == shared, (
        "the lock was not placed in the operator-named directory"
    )
    assert warmup_lock_path().exists()


@pytest.mark.requirement("JIT-06")
def test_a_missing_operator_lock_directory_is_refused_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a missing MDT_NUMBA_WARMUP_DIR directory is silently created [then] fail, [else stop].

    the operator's directory is theirs to create and secure; a module that
    makes it for them has guessed at where cross-user sharing should live,
    which is the inferred world-writable placement this issue removes"""
    missing = tmp_path / "operator-dir"
    monkeypatch.setenv(jit_warmup.LOCK_DIR_ENV, str(missing))
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: missing / "stamp")
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert result.refused, "a missing operator directory must refuse, not be created"
    assert _CompileCounter.compiles == 0
    assert not missing.exists(), "the module must not create the operator's directory"


@pytest.mark.requirement("JIT-06")
def test_an_empty_operator_lock_directory_env_var_is_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] MDT_NUMBA_WARMUP_DIR="" is read as an opt-in to a shared directory [then] fail, [else stop].

    ``os.environ.get(LOCK_DIR_ENV) is not None`` is true for an empty string,
    which used to flip ``shared`` on while ``warmup_dir()`` still fell back to
    its own private default path (a falsy check) - the two disagreed, so the
    predictable default path ran in shared mode, which neither creates the
    directory 0700 nor checks ``st_uid`` (issue #1401 P1). An empty value must
    be treated exactly like unset: the private path, created 0700 and
    ownership-checked."""
    monkeypatch.setattr(_warmup_lock.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv(jit_warmup.LOCK_DIR_ENV, "")
    _CompileCounter.compiles = 0

    result = warm_backend_jit(_CompileCounter, backend_name="counter")

    assert not result.refused, (
        "an empty MDT_NUMBA_WARMUP_DIR must not be adopted as a shared "
        "directory that then fails to exist and refuses the warm-up"
    )
    assert _CompileCounter.compiles == 1, "the private-mode warm-up must still compile"
    lock_dir = warmup_lock_path().parent
    dir_mode = lock_dir.stat().st_mode & 0o777
    assert dir_mode == 0o700, (
        f"the private lock directory must be created 0700, got {oct(dir_mode)}"
    )
    assert lock_dir.stat().st_uid == os.getuid(), (
        "private mode must check st_uid; shared mode never would have"
    )
