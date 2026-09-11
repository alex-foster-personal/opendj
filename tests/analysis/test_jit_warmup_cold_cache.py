"""Cold-cache acceptance for the #1316 warm-up: the real CLI, real librosa.

Split out of ``test_jit_warmup.py``, which holds the fast guards (the compile
lock and its negative control, the parent-before-pool ordering, the cache
fingerprint). Everything here really compiles librosa from scratch into a real
shared numba cache directory, so it is minutes rather than seconds and carries
``@pytest.mark.slow``.

Nothing about the subject is mocked, and it cannot be: #1316 is an interaction
between two operating-system processes and one set of files on disk, and the
crash it produces is a NULL instruction pointer with no Python exception to
patch, assert on, or simulate. A test that stubbed any of that would be testing
the stub.

Regression lines:
  - if a cold `apps.analysis.run --workers 2` stops exiting 0 on a purged
    shared cache then #1316 has regressed
  - if a run against the cache two concurrent cold runs left stops exiting 0
    then the writers corrupted it after all
  - if a worker writes to the cache after the warm-up finished then the
    warm-up is not compiling everything analyze() needs
  - if the CI warm-up command can warm zero artifacts and still exit 0 then
    the CI step is a green signal that guarantees nothing
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apps.analysis.jit_warmup import cache_fingerprint
from tests.platform_capabilities import posix_permission_denial_supported

REPO_ROOT = Path(__file__).resolve().parents[2]

_CAN_TEST_PERMISSION_DENIAL = posix_permission_denial_supported(
    os.name, getattr(os, "geteuid", None)
)


def _stamp_vouches_via_subprocess(roots: tuple[Path, ...], env: dict[str, str]) -> bool:
    """Run ``stamp_vouches_for`` in a fresh process against an explicit env.

    Keeps ``MDT_NUMBA_WARMUP_DIR`` out of THIS process's environment (issue
    #1572 review: monkeypatching the process env is prohibited by AGENTS.md),
    so every bit of evidence for the vouch check comes from the real
    production path, exactly like the warm-up subprocess beside it.
    """
    driver = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from pathlib import Path
        from apps.analysis.jit_warmup import stamp_vouches_for
        roots = tuple(Path(p) for p in {[str(r) for r in roots]!r})
        print(stamp_vouches_for(roots))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    return proc.stdout.strip().splitlines()[-1] == "True"

# ---------------------------------------------------------------------------
# The real subject: librosa, a real purge, real concurrent CLI invocations
# ---------------------------------------------------------------------------


@pytest.fixture()
def purged_librosa_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A real, empty, shared numba cache dir that this test's processes share.

    A dedicated ``NUMBA_CACHE_DIR`` rather than the installed librosa package,
    because purging site-packages would sabotage every other test in the run.
    It is SHARED by the processes under test - one directory, several writers -
    which is the property that produced #1316, so the race is still reachable.
    """
    cache = tmp_path / "numba-cache"
    cache.mkdir()
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(cache))
    assert cache_fingerprint((cache,)) == "", "fixture must start cold"
    return cache


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("JIT-04")
def test_two_concurrent_cold_cli_invocations_both_succeed(
    purged_librosa_cache: Path, tmp_path: Path
) -> None:
    """[if] a cold concurrent analysis run exits non-zero, or leaves a cache a later run cannot load [then] fail, [else stop].

    if two cold `apps.analysis.run --workers 2` processes can still poison a
    shared cache then #1316 is back: they exit 139 with no traceback, and so
    does every later process that loads what they left

    The real CLI, the real librosa backend, real audio, no mock and no patch:
    the subject IS the interaction between two operating-system processes and a
    file on disk, and nothing smaller reproduces it.
    """
    fixtures = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup"
    audio = [fixtures / "src-320.mp3", fixtures / "src-v2.mp3"]
    for path in audio:
        assert path.is_file(), f"fixture precondition: {path} must exist"

    env = {**os.environ, "NUMBA_CACHE_DIR": str(purged_librosa_cache)}
    cmd = [
        sys.executable, "-m", "apps.analysis.run",
        "--files", *[str(p) for p in audio],
        "--workers", "2", "--all", "--dry-run",
    ]
    procs = [
        subprocess.Popen(
            cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for _ in range(2)
    ]
    outputs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=900)
        outputs.append((proc.returncode, out))

    for code, out in outputs:
        assert code == 0, (
            f"a cold concurrent invocation exited {code} "
            f"({'SEGFAULT' if code in (-11, 139) else 'error'}):\n{out}"
        )
    assert any("jit-warmup" in out for _, out in outputs), (
        "neither invocation reported a warm-up, so this run did not exercise it"
    )
    after_pool = cache_fingerprint((purged_librosa_cache,))
    assert after_pool, (
        "the run wrote no JIT artifacts, so the shared cache under test was "
        "never populated and this proves nothing about the race"
    )

    # And the cache they left is loadable: the crash was in the LOADERS, not
    # only in the writers, so a third run against it is the check that matters.
    after = subprocess.run(
        cmd, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert after.returncode == 0, (
        f"a later run against the cache those two left exited "
        f"{after.returncode}; the cache is poisoned:\n{after.stdout}"
    )

    # The load-bearing invariant, asserted rather than assumed: a warm-up
    # leaves the WORKERS nothing to compile. If it did not, the workers would
    # still be compiling concurrently into the shared cache and the lock would
    # be protecting the wrong window. Measured on agentbox: the full analysis
    # path compiles 78 artifacts and every one of them lands under librosa, so
    # LibrosaBackend.jit_cache_roots sees all of them.
    assert cache_fingerprint((purged_librosa_cache,)) == after_pool, (
        "a worker wrote to the shared JIT cache after the warm-up had already "
        "finished, so the warm-up is not compiling everything analyze() needs "
        "and the workers are still racing"
    )


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("JIT-05")
def test_the_warmup_cli_purges_warms_and_proves_it_wrote_something(
    purged_librosa_cache: Path,
) -> None:
    """[if] the CI warm-up command writes no artifacts [then] fail, [else stop].

    if the CI warm-up step could warm zero artifacts and still exit 0 then
    the step is a green signal that guarantees nothing"""
    env = {**os.environ, "NUMBA_CACHE_DIR": str(purged_librosa_cache)}
    proc = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    assert "purged=0" in proc.stdout, proc.stdout
    written = [
        int(line.split("=", 1)[1])
        for line in proc.stdout.splitlines()
        if line.startswith("jit-warmup artifacts=")
    ]
    assert written and written[0] > 0, (
        f"the warm-up reported {written} artifacts on disk:\n{proc.stdout}"
    )

    # Run it again: it must now purge what it just wrote, and warm again.
    again = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert again.returncode == 0, again.stdout
    assert f"purged={written[0]}" in again.stdout, again.stdout


@pytest.mark.requirement("JIT-05")
def test_the_warmup_cli_fails_loudly_when_it_warms_nothing(tmp_path: Path) -> None:
    """[if] a warm-up that compiled nothing still exits 0 [then] fail, [else stop].

    if a backend that declares cache roots warms zero artifacts and the CLI
    still exits 0 then CI's warm-up step cannot fail for the one reason it
    exists to catch"""
    driver = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from pathlib import Path
        from apps.analysis import backends
        from apps.analysis.jit_warmup import main

        class Hollow:
            name = "hollow"
            @classmethod
            def jit_cache_roots(cls):
                return (Path({str(tmp_path)!r}),)
            @classmethod
            def warm_jit_cache(cls):
                return "warmed absolutely nothing"

        backends.BACKENDS["hollow"] = Hollow
        raise SystemExit(main(["--backend", "hollow"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
        check=False,
    )
    assert proc.returncode == 1, (
        f"a warm-up that wrote nothing exited {proc.returncode}:\n{proc.stdout}"
    )
    assert "warmed zero artifacts" in proc.stdout, proc.stdout


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("JIT-02")
def test_a_real_artifact_torn_in_place_no_longer_vouches(
    purged_librosa_cache: Path,
) -> None:
    """[if] a torn real artifact still vouches [then] fail, [else stop].

    issue #1572: reproduces the corruption a racing or killed writer leaves -
    a real numba-compiled ``.nbc``, one byte flipped IN PLACE, its original
    size and mtime restored - against the real librosa backend's own cache,
    not a fabricated stand-in. A stale-but-untouched fingerprint would keep
    vouching for this cache, the warm-up would skip it, and the next loader
    would dereference the corrupt object code: the #1316 crash this module
    exists to prevent.

    if the fingerprint still vouched for a torn real artifact then a
    corrupted persistent cache would be trusted and loaded

    Warmed via the CLI in a subprocess, like the two tests above: numba reads
    ``NUMBA_CACHE_DIR`` once at import, so a second in-process real compile
    against a different cache dir would silently keep writing whatever the
    first one in this process used. The corruption itself needs no numba
    import and runs here, in the parent, against the artifacts the subprocess
    left on disk - but the vouch check ALSO runs in a subprocess (issue #1572
    review: no monkeypatching the process env), against the SAME explicit
    ``MDT_NUMBA_WARMUP_DIR`` the warm-up used, so its evidence comes entirely
    through the real production path too.
    """
    lock_dir = purged_librosa_cache.parent / "lock"
    lock_dir.mkdir(mode=0o700)
    roots = (purged_librosa_cache,)
    env = {**os.environ, "NUMBA_CACHE_DIR": str(purged_librosa_cache),
           "MDT_NUMBA_WARMUP_DIR": str(lock_dir)}

    warmup = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge-if-stale"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert warmup.returncode == 0, warmup.stdout
    assert "skipped" not in warmup.stdout, (
        f"a cold cache must compile, not skip: {warmup.stdout}"
    )
    assert _stamp_vouches_via_subprocess(roots, env), (
        "the serial warm-up's own cache must be vouched for"
    )

    artifacts = [p for root in roots for p in root.rglob("*.nbc") if p.is_file()]
    assert artifacts, "the real backend must have compiled at least one .nbc"
    target = artifacts[0]
    before_stat = target.stat()

    torn = bytearray(target.read_bytes())
    torn[len(torn) // 2] ^= 0xFF
    target.write_bytes(bytes(torn))
    os.utime(target, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))
    after_stat = target.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns

    assert not _stamp_vouches_via_subprocess(roots, env), (
        f"{target} was torn at its original size and mtime, but the real "
        "cache still vouches for itself"
    )


@pytest.mark.skipif(
    not _CAN_TEST_PERMISSION_DENIAL,
    reason="root bypasses file permissions; chmod(0o000) would still read, "
    "so this fixture cannot produce a real unreadable artifact here",
)
@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("JIT-02")
def test_a_real_artifact_that_cannot_be_read_self_heals_through_purge_if_stale(
    purged_librosa_cache: Path,
) -> None:
    """[if] an unreadable real artifact crashes or is trusted [then] fail, [else stop].

    issue #1572 review: an EIO-damaged file, or a cross-user cache artifact
    with restrictive permissions, is listable (stat succeeds) but not
    readable. Replaces an earlier version of this test that fabricated the
    artifact's bytes: this one chmods a REAL numba-compiled ``.nbc`` the
    production CLI just wrote, then re-runs the production
    ``--purge-if-stale`` path end to end and requires it to notice, purge,
    and recompile rather than crash or silently keep trusting the cache.
    Skipped, not xfailed, under a privileged test runner: root ignores the
    permission bits ``chmod(0o000)`` relies on (same reasoning as the
    sibling lock-file test in test_jit_warmup_hygiene.py), so nothing this
    test could assert there would say anything about the real defect.

    if the OSError from an unreadable artifact escaped cache_fingerprint()
    then the production purge-if-stale path would die before it could purge
    and self-heal the damaged cache - the opposite of what its docstring
    promises for anything it cannot measure
    """
    roots = (purged_librosa_cache,)
    env = {**os.environ, "NUMBA_CACHE_DIR": str(purged_librosa_cache)}

    warmup = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge-if-stale"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert warmup.returncode == 0, warmup.stdout
    assert "skipped" not in warmup.stdout, (
        f"a cold cache must compile, not skip: {warmup.stdout}"
    )

    artifacts = [p for root in roots for p in root.rglob("*.nbc") if p.is_file()]
    assert artifacts, "the real backend must have compiled at least one .nbc"
    target = artifacts[0]
    target.chmod(0o000)
    try:
        assert cache_fingerprint(roots) == "", (
            "a real but unreadable artifact must read as unmeasurable, not raise"
        )
        repaired = subprocess.run(
            [sys.executable, "-m", "apps.analysis.jit_warmup",
             "--backend", "librosa", "--purge-if-stale"],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
            check=False,
        )
    finally:
        target.chmod(0o644)

    assert repaired.returncode == 0, (
        f"{target} was made unreadable but the warm-up crashed instead of "
        f"purging and recompiling:\n{repaired.stdout}"
    )
    assert "skipped" not in repaired.stdout, (
        f"an unreadable artifact must force a purge and recompile, not a "
        f"skip: {repaired.stdout}"
    )
    assert _stamp_vouches_via_subprocess(roots, env), (
        "the purge-and-recompile must leave a vouched-for cache behind"
    )


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("JIT-02")
def test_a_real_analysis_run_purges_a_torn_cache_instead_of_loading_it(
    purged_librosa_cache: Path,
) -> None:
    """[if] apps.analysis.run loads a torn artifact instead of purging it [then] fail, [else stop].

    issue #1572 review: apps/analysis/run.py's own call to warm_backend_jit -
    the ACTUAL production entrypoint this whole module exists to protect,
    called in the same process about to analyze real audio - did not set
    purge_stale, so a detected content mismatch only skipped the "skip"
    branch and fell through to backend.warm_jit_cache(), which can still
    load the very artifact whose content just failed to vouch. Exercises
    apps.analysis.run itself, not the jit_warmup CLI, end to end against a
    real torn artifact.

    if the production analysis entrypoint detected a torn cache but did not
    purge it then the segfault this module exists to prevent could still
    happen through the exact call site the original #1572 bug hit
    """
    roots = (purged_librosa_cache,)
    env = {**os.environ, "NUMBA_CACHE_DIR": str(purged_librosa_cache)}

    warmup = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge-if-stale"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert warmup.returncode == 0, warmup.stdout

    artifacts = [p for root in roots for p in root.rglob("*.nbc") if p.is_file()]
    assert artifacts, "the real backend must have compiled at least one .nbc"
    target = artifacts[0]
    before_stat = target.stat()
    torn = bytearray(target.read_bytes())
    torn[len(torn) // 2] ^= 0xFF
    target.write_bytes(bytes(torn))
    os.utime(target, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))

    fixtures = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup"
    audio = fixtures / "src-320.mp3"
    assert audio.is_file(), f"fixture precondition: {audio} must exist"

    run = subprocess.run(
        [sys.executable, "-m", "apps.analysis.run",
         "--files", str(audio), "--workers", "1", "--all", "--dry-run"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert run.returncode == 0, (
        f"apps.analysis.run exited {run.returncode} "
        f"({'SEGFAULT' if run.returncode in (-11, 139) else 'error'}) against a "
        f"torn cache instead of purging it:\n{run.stdout}"
    )
    assert _stamp_vouches_via_subprocess(roots, env), (
        "apps.analysis.run must leave a vouched-for cache behind, proving it "
        "purged and recompiled the torn artifact rather than silently loading it"
    )


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("META-01")
def test_a_real_analysis_run_self_provisions_a_cache_dir_and_purges_it_when_torn(
    tmp_path: Path,
) -> None:
    """[if] apps.analysis.run with NUMBA_CACHE_DIR unset never gets purge protection [then] fail, [else stop].

    issue #1572 review round 7: run() delegates the purge_stale decision to
    ensure_owned_numba_cache_dir(), which self-provisions a private directory
    when NUMBA_CACHE_DIR is unset - the DEFAULT/product deployment path,
    since only CI sets that variable. A test that mocks the backend and spies
    on the keyword passed to warm_backend_jit proves only that a keyword
    moved, not that a real numba cache gets written, fingerprinted, torn and
    purged through that self-provisioned directory. Exercises
    apps.analysis.run end to end, with the real librosa backend and
    NUMBA_CACHE_DIR unset, exactly like
    test_a_real_analysis_run_purges_a_torn_cache_instead_of_loading_it above
    but for the path that never sets the variable at all.
    """
    env = {k: v for k, v in os.environ.items() if k != "NUMBA_CACHE_DIR"}
    env["TMPDIR"] = str(tmp_path)

    fixtures = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup"
    audio = fixtures / "src-320.mp3"
    assert audio.is_file(), f"fixture precondition: {audio} must exist"

    first_run = subprocess.run(
        [sys.executable, "-m", "apps.analysis.run",
         "--files", str(audio), "--workers", "1", "--all", "--dry-run"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert first_run.returncode == 0, first_run.stdout

    provisioned = [
        p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("mdt-numba-cache-")
    ]
    assert len(provisioned) == 1, (
        "expected exactly one self-provisioned cache directory under TMPDIR, "
        f"found {provisioned}"
    )
    cache_dir = provisioned[0]
    roots = (cache_dir,)

    artifacts = [p for p in cache_dir.rglob("*.nbc") if p.is_file()]
    assert artifacts, (
        "the real backend must have compiled at least one .nbc into the "
        "self-provisioned directory"
    )
    target = artifacts[0]
    before_stat = target.stat()
    torn = bytearray(target.read_bytes())
    torn[len(torn) // 2] ^= 0xFF
    target.write_bytes(bytes(torn))
    os.utime(target, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))

    second_run = subprocess.run(
        [sys.executable, "-m", "apps.analysis.run",
         "--files", str(audio), "--workers", "1", "--all", "--dry-run"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert second_run.returncode == 0, (
        f"apps.analysis.run exited {second_run.returncode} "
        f"({'SEGFAULT' if second_run.returncode in (-11, 139) else 'error'}) against a "
        f"torn self-provisioned cache instead of purging it:\n{second_run.stdout}"
    )
    stamp_env = {**env, "NUMBA_CACHE_DIR": str(cache_dir)}
    assert _stamp_vouches_via_subprocess(roots, stamp_env), (
        "apps.analysis.run must leave a vouched-for cache behind, proving it "
        "purged and recompiled the torn artifact rather than silently loading it"
    )


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requirement("JIT-05")
def test_a_no_cache_backends_purge_does_not_touch_a_real_shared_stamp(
    purged_librosa_cache: Path,
) -> None:
    """[if] a no-cache backend's purge deletes a real cache's shared stamp [then] fail, [else stop].

    issue #1572 review: the lock, fingerprint and stamp are keyed on the
    ENVIRONMENT (interpreter prefix + NUMBA_CACHE_DIR), not on a specific
    backend's roots, so a backend with no JIT cache of its own (mik) shares
    that key with librosa's real cache under the same NUMBA_CACHE_DIR. Two
    real subprocess warm-ups sharing one env, not an injected stamp path,
    because a unit test that hand-picks a stamp path cannot prove two
    backends actually SHARE one - only the real environment-keyed lookup can.

    if a no-cache backend's purge deleted the real cache's stamp then every
    mik (or other no-cache backend) call would force a spurious full
    recompile of librosa's cache for a reason unrelated to it
    """
    roots = (purged_librosa_cache,)
    env = {**os.environ, "NUMBA_CACHE_DIR": str(purged_librosa_cache)}

    warmup = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge-if-stale"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )
    assert warmup.returncode == 0, warmup.stdout
    assert _stamp_vouches_via_subprocess(roots, env), (
        "fixture precondition: the real librosa warm-up must leave a vouched stamp"
    )

    no_cache = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "mik", "--purge-if-stale"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        check=False,
    )
    assert no_cache.returncode == 0, no_cache.stdout

    assert _stamp_vouches_via_subprocess(roots, env), (
        "a no-cache backend's purge must not delete librosa's real, shared stamp"
    )

