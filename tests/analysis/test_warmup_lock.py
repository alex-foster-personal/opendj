"""Ownership of the numba cache directory ``purge_stale`` is allowed to delete from.

Split from ``test_jit_warmup_hygiene.py`` (which guards the *lock*) because this
guards a different primitive: ``ensure_owned_numba_cache_dir`` in
``_warmup_lock.py``, which decides whether ``run.py`` may pass
``purge_stale=True`` into ``warm_backend_jit`` (issue #1572).

Everything here runs the real function in a fresh subprocess against an
explicit environment, never ``monkeypatch.setenv``/``setattr`` on this
process's own state (AGENTS.md: no monkeypatching in tests) - the same
convention as ``_stamp_vouches_via_subprocess`` in
``test_jit_warmup_cold_cache.py``. ``TMPDIR`` in that environment, not a
patched ``tempfile.gettempdir``, is what keeps the default (unset) path under
a disposable ``tmp_path`` rather than the real system temp root.

Three rounds of review shaped what these tests pin:
  - round 6: a first fix gated purge_stale on NUMBA_CACHE_DIR merely being
    SET, which regressed the default/product path (only CI sets that
    variable) and trusted presence over ownership.
  - round 7 (this file): the ownership check alone still collided across
    users sharing one venv (agentbox has run it as both root and ghrunner) -
    the default path carried no UID, so the second user found the first
    user's directory and silently lost purge protection instead of getting
    their own. And ownership alone does not mean a directory is DEDICATED to
    this cache: purge_cache() deletes every *.nbi/*.nbc under its root, so an
    operator's general-purpose NUMBA_CACHE_DIR - shared with other
    numba-using tools - would lose their artifacts too.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.waits import EXTERNAL_STATE_GUARD_S

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_owned_numba_cache_dir_via_subprocess(env: dict[str, str]) -> str:
    """Run ``ensure_owned_numba_cache_dir()`` in a fresh process against ``env``.

    Prints ``None`` or the resolved path, one line, so the caller can then
    inspect the real directory the subprocess actually left on disk (mode,
    owner, marker) using plain filesystem calls in THIS process - which is
    evidence about the real production path, not about a fixture.
    """
    driver = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from apps.analysis._warmup_lock import ensure_owned_numba_cache_dir
        print(ensure_owned_numba_cache_dir())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    return proc.stdout.strip().splitlines()[-1]


def _env_without_numba_cache_dir(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "NUMBA_CACHE_DIR"}
    env["TMPDIR"] = str(tmp_path)
    return env


@pytest.mark.requirement("META-01")
def test_unset_numba_cache_dir_gets_a_private_owned_uid_scoped_directory(
    tmp_path: Path,
) -> None:
    """[if] the default path is missing, unowned, shared, or UID-collides [then] fail, [else stop].

    the round-6 regression: gating purge_stale on NUMBA_CACHE_DIR being set
    left the default/product deployment - the one that never sets it - with
    purge permanently off. The round-7 regression: a default path with no
    UID in it lets a second user on a shared venv (agentbox: root then
    ghrunner) find the first user's directory and silently lose purge
    protection instead of getting their own"""
    result = _ensure_owned_numba_cache_dir_via_subprocess(_env_without_numba_cache_dir(tmp_path))

    assert result != "None", "the default path must provision a directory, not refuse"
    path = Path(result)
    assert path.parent == tmp_path, "must be created under TMPDIR, not the real system temp root"
    assert path.is_dir()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o700, f"the private cache directory must be 0700, got {oct(mode)}"
    assert path.stat().st_uid == os.getuid()
    assert str(os.getuid()) in path.name, (
        "the default path must be scoped per-UID, or a second user sharing "
        "this venv silently loses purge protection instead of getting their "
        "own directory (issue #1572 review round 7)"
    )


@pytest.mark.requirement("META-01")
def test_two_independent_processes_converge_on_the_same_default_directory(
    tmp_path: Path,
) -> None:
    """[if] two separate process runs disagree on the default directory [then] fail, [else stop].

    warm_backend_jit and any later cache lookup are separate CLI invocations
    in production, not calls inside one process - they must land on the one
    directory a prior run already provisioned and verified"""
    env = _env_without_numba_cache_dir(tmp_path)

    first = _ensure_owned_numba_cache_dir_via_subprocess(env)
    second = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert first != "None"
    assert first == second


@pytest.mark.requirement("META-01")
def test_an_operator_set_directory_that_is_genuinely_owned_is_verified_and_kept(
    tmp_path: Path,
) -> None:
    """[if] a real, self-owned, empty NUMBA_CACHE_DIR is refused/replaced [then] fail, [else stop].

    the control against over-correcting: an operator who already points
    NUMBA_CACHE_DIR at a real directory of their own must still get purge
    protection, not lose it because verification is stricter than presence"""
    real_cache = tmp_path / "numba-cache"
    real_cache.mkdir()
    env = {**os.environ, "NUMBA_CACHE_DIR": str(real_cache)}

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == str(real_cache)


@pytest.mark.requirement("META-01")
def test_an_operator_set_directory_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    """[if] a symlinked NUMBA_CACHE_DIR is trusted for purge [then] fail, [else stop].

    the round-6 finding this closes: the prior gate trusted NUMBA_CACHE_DIR
    merely being set, so an operator (or an attacker on a shared host)
    pointing it at a symlink into a directory we do not own would still get
    purged into"""
    target = tmp_path / "victim"
    target.mkdir()
    planted = tmp_path / "cache-symlink"
    os.symlink(target, planted)
    env = {**os.environ, "NUMBA_CACHE_DIR": str(planted)}

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == "None", "a symlinked NUMBA_CACHE_DIR must refuse, not be purged into"
    assert stat.S_ISDIR(os.stat(target).st_mode), "the symlink target must be untouched"


@pytest.mark.requirement("META-01")
def test_an_operator_set_directory_that_does_not_exist_is_refused_not_created(
    tmp_path: Path,
) -> None:
    """[if] a missing operator NUMBA_CACHE_DIR is silently created [then] fail, [else stop].

    an operator-named directory is theirs to create; guessing at making it
    for them is the same overreach the warm-up lock's own operator opt-in
    (MDT_NUMBA_WARMUP_DIR) already refuses"""
    missing = tmp_path / "does-not-exist"
    env = {**os.environ, "NUMBA_CACHE_DIR": str(missing)}

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == "None"
    assert not missing.exists(), "the module must not create the operator's directory"


@pytest.mark.requirement("META-01")
def test_a_nonempty_operator_directory_without_our_marker_is_refused(tmp_path: Path) -> None:
    """[if] a general-purpose NUMBA_CACHE_DIR with foreign files is purged [then] fail, [else stop].

    the round-7 finding this closes: ownership alone does not mean a
    directory is DEDICATED to this cache. purge_cache() deletes every
    *.nbi/*.nbc anywhere under its root, so an operator who points
    NUMBA_CACHE_DIR at their normal, general-purpose numba cache - shared
    with other numba-using tools - would lose those tools' artifacts too. A
    directory with unrelated content and no dedication marker must refuse,
    not be trusted merely for being owned"""
    shared = tmp_path / "shared-numba-cache"
    shared.mkdir()
    (shared / "someone_elses_module.nbi").write_bytes(b"not ours")
    env = {**os.environ, "NUMBA_CACHE_DIR": str(shared)}

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == "None"


@pytest.mark.requirement("META-01")
def test_a_nonempty_operator_directory_with_our_marker_is_trusted(tmp_path: Path) -> None:
    """[if] a directory this app already claimed is refused on a later run [then] fail, [else stop].

    the control against over-correcting: once ensure_owned_numba_cache_dir
    has planted its dedication marker in an operator directory, a LATER run
    (a later process, a later analysis) must still get purge protection for
    content it compiled there - dedication does not expire the moment the
    directory stops being empty"""
    shared = tmp_path / "already-claimed"
    shared.mkdir()
    env = {**os.environ, "NUMBA_CACHE_DIR": str(shared)}

    first = _ensure_owned_numba_cache_dir_via_subprocess(env)
    assert first == str(shared), "fixture precondition: the first run must claim the directory"
    (shared / "librosa_module.nbi").write_bytes(b"a real compiled artifact")

    second = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert second == str(shared)


@pytest.mark.requirement("META-01")
def test_an_operator_directory_already_stamped_by_our_own_warmup_is_trusted(
    tmp_path: Path,
) -> None:
    """[if] a cache warmed through jit_warmup's own CLI is refused here [then] fail, [else stop].

    the regression this closes: CI pre-warms a persistent per-runner cache
    by calling ``python -m apps.analysis.jit_warmup`` directly (issue
    #1437), which writes real .nbi/.nbc artifacts and a warm-up stamp
    WITHOUT ever calling ensure_owned_numba_cache_dir() to plant the
    dedication marker. A directory with real content and a warm-up stamp
    for this exact NUMBA_CACHE_DIR, but no marker, must still be trusted -
    our own warm-up machinery already vouched for it, whichever entrypoint
    did the warming"""
    shared = tmp_path / "pre-warmed-by-jit-warmup-cli"
    shared.mkdir()
    (shared / "librosa_module.nbi").write_bytes(b"a real compiled artifact")
    env = {**os.environ, "NUMBA_CACHE_DIR": str(shared)}

    stamp_driver = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from apps.analysis._warmup_lock import warmup_stamp_path
        stamp = warmup_stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("fake-stamp")
        """
    )
    setup = subprocess.run(
        [sys.executable, "-c", stamp_driver],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    assert setup.returncode == 0, setup.stdout

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == str(shared)


@pytest.mark.requirement("META-01")
@pytest.mark.rb_parity
def test_this_platform_provisions_a_directory_or_refuses_without_raising(
    tmp_path: Path,
) -> None:
    """[if] this platform crashes, or trusts a directory it cannot prove it owns [then] fail, [else stop].

    windows-latest job 115807584571: ``os.getuid()`` raised AttributeError
    there, every ``apps.analysis.run`` with work to do exited 5 before its
    first track, and the drain read a vanished target (3) and an unavailable
    backend (4) as a systemic fault instead. Enrolled in the Windows parity
    gate so the real Windows ``os`` (no ``getuid``) runs this, not a stand-in.
    With a uid the default path is provisioned; without one, ownership cannot
    be proven, so the answer is a refusal (no purge), not a crash and not an
    assumed owner (Sol review at 7110a58d7)."""
    result = _ensure_owned_numba_cache_dir_via_subprocess(_env_without_numba_cache_dir(tmp_path))

    if not hasattr(os, "getuid"):
        assert result == "None", "with no uid, ownership is unproven and must refuse"
        assert not any(tmp_path.iterdir()), "a refusal must not create a directory"
        return
    assert result != "None", "the default path must provision a directory, not refuse"
    path = Path(result)
    assert path.parent == tmp_path
    assert path.is_dir()
    assert (path / ".mdt-numba-cache-owned").is_file(), (
        "the dedication marker must still be planted, or purge_stale has "
        "nothing proving the directory is this cache's alone"
    )


def _run_without_platform_primitive(tmp_path: Path, *, hide: str, body: str) -> str:
    """Run ``body`` in a fresh process where ``os.getuid`` or ``fcntl`` is absent.

    ``hide="nothing"`` runs it against this platform's real primitives.

    Reproduces the Windows shape on a POSIX host without touching this
    process: ``hide="getuid"`` deletes ``os.getuid`` and ``hide="fcntl"``
    makes ``import fcntl`` raise ImportError, before any repo module loads.
    ``body`` prints its result on the last line.
    """
    hiding = {
        "getuid": "import os\nif hasattr(os, 'getuid'):\n    del os.getuid",
        "fcntl": "sys.modules['fcntl'] = None",
        "nothing": "",
    }[hide]
    driver = f"import sys\nsys.path.insert(0, {str(REPO_ROOT)!r})\n{hiding}\n{textwrap.dedent(body)}"
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(REPO_ROOT),
        env=_env_without_numba_cache_dir(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    return proc.stdout.strip().splitlines()[-1]


@pytest.mark.requirement("JIT-06")
def test_without_a_uid_the_lock_dir_is_provisioned_but_cache_is_refused(
    tmp_path: Path,
) -> None:
    """[if] no uid lets cache dir pass as ours [then] fail, [else stop].

    Lock directory is provisioned under tmp; cache directory and ownership
    checks still refuse."""
    existing = tmp_path / "already-here"
    existing.mkdir()
    lock_path = tmp_path / "lock"
    out = _run_without_platform_primitive(
        tmp_path,
        hide="getuid",
        body=f"""
        from pathlib import Path
        from apps.analysis._warmup_lock import (
            _ensure_lock_dir, _is_owned_dir, ensure_owned_numba_cache_dir,
        )
        print(
            _is_owned_dir(Path({str(existing)!r})),
            _ensure_lock_dir(Path({str(lock_path)!r}), shared=False),
            ensure_owned_numba_cache_dir(),
        )
        """,
    )

    assert out == "False True None"
    assert lock_path.is_dir()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["already-here", "lock"]


@pytest.mark.requirement("JIT-06")
def test_with_a_uid_the_same_checks_still_pass(tmp_path: Path) -> None:
    """[if] the no-uid refusal overshoots into refusing a POSIX host [then] fail, [else stop].

    The overshoot control for the test above: the same helper and body with
    nothing hidden must still own, create and provision."""
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX control: this platform has no getuid")
    existing = tmp_path / "already-here"
    existing.mkdir()
    out = _run_without_platform_primitive(
        tmp_path,
        hide="nothing",
        body=f"""
        from pathlib import Path
        from apps.analysis._warmup_lock import (
            _ensure_lock_dir, _is_owned_dir, ensure_owned_numba_cache_dir,
        )
        print(
            _is_owned_dir(Path({str(existing)!r})),
            _ensure_lock_dir(Path({str(tmp_path / "lock")!r}), shared=False),
            ensure_owned_numba_cache_dir() is not None,
        )
        """,
    )

    assert out == "True True True"
    assert (tmp_path / "lock").is_dir()


@pytest.mark.requirement("JIT-8573")
@pytest.mark.rb_parity
def test_a_non_contention_lock_error_refuses_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] try_lock raises a non-contention error [then] refuse at once, [else stop]."""
    import logging

    from apps.analysis import jit_warmup
    from apps.shared import file_lock

    caplog.set_level(logging.ERROR, logger="apps.analysis.jit_warmup")

    def _bad_lock(_fd: int, *, nt_offset: int = file_lock.NT_LOCK_OFFSET) -> bool:
        del nt_offset
        raise OSError(errno.EBADF, "bad fd")

    monkeypatch.setattr(file_lock, "try_lock", _bad_lock)
    started = time.monotonic()
    with jit_warmup._compile_lock(tmp_path / "x.lock", 30.0, require_self_owned=False) as (held, proceed, _waited):
        assert (held, proceed) == (False, False)
    assert time.monotonic() - started < 5.0
    assert any("REFUSED rather than treat a lock error as contention" in r.message for r in caplog.records)
    assert not any("another process held" in r.message for r in caplog.records)


@pytest.mark.requirement("JIT-8573")
@pytest.mark.rb_parity
def test_contention_past_timeout_refuses_rather_than_warming_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] contention outlasts timeout [then] refuse not unlock, [else stop]."""
    import logging

    from apps.analysis import jit_warmup
    from apps.shared import file_lock

    caplog.set_level(logging.ERROR, logger="apps.analysis.jit_warmup")
    monkeypatch.setattr(file_lock, "try_lock", lambda _fd, **_: False)
    with jit_warmup._compile_lock(tmp_path / "x.lock", 0.4, require_self_owned=False) as (held, proceed, _waited):
        assert (held, proceed) == (False, False)
    assert any("REFUSED rather than compile a cold cache unlocked" in r.message for r in caplog.records)
    assert not any("warming WITHOUT" in r.message for r in caplog.records)


@pytest.mark.requirement("JIT-06")
@pytest.mark.rb_parity
def test_the_real_compile_lock_on_this_platform_locks_or_refuses(
    tmp_path: Path,
) -> None:
    """[if] this platform's compile lock proceeds without holding [then] fail, [else stop]."""
    out = _run_without_platform_primitive(
        tmp_path,
        hide="nothing",
        body=f"""
        from pathlib import Path
        from apps.analysis.jit_warmup import _compile_lock
        with _compile_lock(
            Path({str(tmp_path / "x.lock")!r}), 1.0, require_self_owned=False
        ) as (held, proceed, _waited):
            print(held, proceed)
        """,
    )

    held, proceed = out.split()
    assert (held, proceed) == ("True", "True"), out


_COMPILE_LOCK_HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from apps.analysis.jit_warmup import _compile_lock\n"
    "with _compile_lock(Path(sys.argv[1]), 300.0, require_self_owned=False) as (held, proceed, _):\n"
    "    print(held, proceed, flush=True)\n"
    "    sys.stdin.read()\n"
)

_COMPILE_LOCK_CONTENDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from apps.analysis.jit_warmup import _compile_lock\n"
    "with _compile_lock(Path(sys.argv[1]), 1.0, require_self_owned=False) as (held, proceed, _):\n"
    "    print(held, proceed)\n"
)


def _compile_lock_contender(path: Path) -> tuple[bool, bool]:
    done = subprocess.run(
        [sys.executable, "-c", _COMPILE_LOCK_CONTENDER, str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=EXTERNAL_STATE_GUARD_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr or done.stdout
    held_s, proceed_s = done.stdout.strip().split()
    return held_s == "True", proceed_s == "True"


@pytest.mark.requirement("JIT-8573")
@pytest.mark.rb_parity
def test_the_real_compile_lock_excludes_a_second_process(tmp_path: Path) -> None:
    """[if] a live holder holds the compile lock [then] a child is refused, [else stop]."""
    lock_path = tmp_path / "x.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _COMPILE_LOCK_HOLDER, str(lock_path)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        line = holder.stdout.readline().strip()
        assert line == "True True"
        assert _compile_lock_contender(lock_path) == (False, False)
    finally:
        holder.kill()
        holder.wait(timeout=EXTERNAL_STATE_GUARD_S)
        assert holder.stdin is not None and holder.stdout is not None
        holder.stdin.close()
        holder.stdout.close()
    assert _compile_lock_contender(lock_path) == (True, True)


@pytest.mark.requirement("META-01")
@pytest.mark.rb_parity
def test_a_symlinked_directory_is_refused_on_this_platform(
    tmp_path: Path,
) -> None:
    """[if] dropping the uid half of the check also drops the no-symlink half [then] fail, [else stop].

    the overshoot control for the test above: on Windows, where no uid exists,
    making ownership pass must not make a planted symlink pass with it"""
    target = tmp_path / "victim"
    target.mkdir()
    planted = tmp_path / "cache-symlink"
    try:
        os.symlink(target, planted, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"this account may not create directory symlinks: {exc}")
    env = {**os.environ, "NUMBA_CACHE_DIR": str(planted)}

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == "None", "a symlinked NUMBA_CACHE_DIR must refuse, not be purged into"
    assert not (target / ".mdt-numba-cache-owned").exists()
