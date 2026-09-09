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

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
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
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
        check=False,
    )
    assert setup.returncode == 0, setup.stdout

    result = _ensure_owned_numba_cache_dir_via_subprocess(env)

    assert result == str(shared)
