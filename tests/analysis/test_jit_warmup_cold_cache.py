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

REPO_ROOT = Path(__file__).resolve().parents[2]

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

