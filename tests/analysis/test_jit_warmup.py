"""The serialized numba JIT-cache warm-up (issue #1316, requirements JIT-01..05).

What went wrong, and therefore what these tests have to prove
------------------------------------------------------------

librosa marks several hot paths ``@numba.guvectorize(..., cache=True)``. Those
compile on first call and write ``.nbi``/``.nbc`` artifacts into ONE shared
on-disk cache. Two processes meeting a COLD cache interleave those writes, and
from then on every process that LOADS the result dies at a NULL instruction
pointer - no traceback, no Python exception, exit 139. It is permanent until
the artifacts are deleted.

So there are four separate claims, and a test that proves one proves nothing
about the others:

1. the warm-up runs in the PARENT, before any worker process exists;
2. two processes cannot be inside the compile at the same time;
3. a real cold-cache CLI run really does exit 0 and write its artifacts;
4. the skip fast path fires only when the cache is what a serial warm-up left.

Every mutual-exclusion test here carries a NEGATIVE CONTROL that runs the same
probe WITHOUT the lock and asserts it DOES overlap. Without that control an
exclusion assertion passes on a machine slow enough that the processes never
happened to overlap, which is the green-but-useless signal this whole issue
hid behind.

Regression lines:
  - if the warm-up stops running before the pool then a worker meets a cold
    shared cache and the corruption is back
  - if the compile lock stops excluding then two cold invocations compile into
    one cache concurrently and poison it
  - if the lock test loses its negative control then it passes on a host too
    slow to overlap, proving nothing
  - if a cold `apps.analysis.run --workers 2` stops exiting 0 on a purged
    cache then #1316 has regressed
  - if the skip fast path fires against a cache nothing vouched for then a
    racing writer's cache is trusted
  - if purge stops removing the stamp then the next warm-up skips against a
    fingerprint of a cache that no longer exists
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis import jit_warmup
from apps.analysis.backends import get_backend
from apps.analysis.jit_warmup import (
    cache_fingerprint,
    purge_cache,
    stamp_vouches_for,
    toolchain_identity,
    warm_backend_jit,
    warmup_lock_path,
    warmup_stamp_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fingerprint and stamp
# ---------------------------------------------------------------------------


def _artifact(root: Path, name: str, body: bytes = b"x") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.mark.requirement("JIT-02")
def test_fingerprint_is_empty_when_there_is_nothing_to_vouch_for(
    tmp_path: Path,
) -> None:
    """[if] an unmeasurable cache produced a non-empty fingerprint [then] fail, [else stop].

    if an empty fingerprint ever compared equal to a stamp then a cache
    nobody measured would be trusted and the warm-up skipped"""
    assert cache_fingerprint(()) == ""
    assert cache_fingerprint((tmp_path,)) == ""
    _artifact(tmp_path, "notes.txt")
    assert cache_fingerprint((tmp_path,)) == "", (
        "a non-artifact file must not count as a warmed cache"
    )


@pytest.mark.requirement("JIT-02")
def test_fingerprint_moves_when_any_artifact_changes(tmp_path: Path) -> None:
    """[if] a write to the cache leaves the fingerprint unmoved [then] fail, [else stop].

    if the fingerprint ignored a write then a racing writer's cache would
    keep matching the stamp and the warm-up would be skipped against it"""
    a = _artifact(tmp_path, "sub/__pycache__/mod.nbi", b"index")
    _artifact(tmp_path, "sub/__pycache__/mod.nbc", b"objectcode")
    first = cache_fingerprint((tmp_path,))
    assert first, "two artifacts must produce a fingerprint"
    assert first.startswith("2:"), first
    assert cache_fingerprint((tmp_path,)) == first, "must be stable when nothing moves"

    a.write_bytes(b"index-rewritten-longer")
    assert cache_fingerprint((tmp_path,)) != first

    added = _artifact(tmp_path, "sub/__pycache__/other.nbi", b"i2")
    with_added = cache_fingerprint((tmp_path,))
    assert with_added.startswith("3:"), with_added
    added.unlink()
    assert cache_fingerprint((tmp_path,)).startswith("2:")


@pytest.mark.requirement("JIT-05")
def test_purge_removes_artifacts_and_the_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a purge leaves artifacts or the stamp behind [then] fail, [else stop].

    if purge left the stamp behind then the next warm-up would skip against
    a fingerprint describing a cache that no longer exists"""
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: stamp)
    stamp.write_text("stale")
    _artifact(tmp_path, "cache/__pycache__/a.nbi")
    _artifact(tmp_path, "cache/__pycache__/a.nbc")
    keep = _artifact(tmp_path, "cache/__pycache__/a.py")

    removed = purge_cache((tmp_path,))

    assert removed == 2, removed
    assert cache_fingerprint((tmp_path,)) == ""
    assert not stamp.exists(), "a purge must take the stamp with it"
    assert keep.exists(), "purge must not touch anything but .nbi/.nbc"


@pytest.mark.requirement("JIT-05")
def test_purge_of_empty_roots_is_a_pure_no_op() -> None:
    """[if] purge_cache(()) touches the filesystem at all [then] fail, [else stop].

    Unit-level half of the empty-roots guard: no artifacts, no stamp lookup,
    nothing removed. The claim that an empty-roots purge must not delete a
    REAL cache's SHARED stamp (issue #1572 review: the lock, fingerprint and
    stamp are keyed on the environment, not a backend's roots) needs two real
    backends sharing one environment to actually prove the sharing happens;
    see test_a_no_cache_backends_purge_does_not_touch_a_real_shared_stamp in
    test_jit_warmup_cold_cache.py for that end-to-end proof.

    if purge_cache(()) removed anything then it read a stamp path this test
    never gave it a reason to touch"""
    assert purge_cache(()) == 0


# ---------------------------------------------------------------------------
# Mutual exclusion, with its negative control
# ---------------------------------------------------------------------------


_LOCKED_DRIVER = """
import os, sys
sys.path.insert(0, {repo!r})
from apps.analysis.jit_warmup import warm_backend_jit
from tests.analysis.pool_probe_backends import LockProbeBackend
r = warm_backend_jit(LockProbeBackend, backend_name="lockprobe")
assert not r.skipped, "the lock probe declares no cache roots, so it must never skip"
print(r.render())
"""

_UNLOCKED_DRIVER = """
import json, os, sys, time
sys.path.insert(0, {repo!r})
record = os.environ["MDT_LOCK_RECORD"]
hold_s = float(os.environ.get("MDT_LOCK_HOLD_S", "0.6"))
ready_n = int(os.environ.get("MDT_LOCK_READY_N", "4"))
barrier = record + ".ready"
go = record + ".go"
with open(barrier, "a", encoding="utf-8") as fh:
    fh.write(str(os.getpid()) + "\\n")
    fh.flush()
deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    try:
        if len(open(barrier, encoding="utf-8").read().splitlines()) >= ready_n:
            break
    except OSError:
        pass
    time.sleep(0.01)
try:
    with open(go, "x", encoding="utf-8") as gf:
        gf.write(str(time.monotonic() + 0.05))
except FileExistsError:
    pass
# The writer creates the go file and then writes the stamp; a sibling that
# reads between those two steps sees an empty file, so wait for a stamp that
# parses rather than for the file to exist.
start_at = None
while start_at is None and time.monotonic() < deadline:
    try:
        text = open(go, encoding="utf-8").read().strip()
    except OSError:
        text = ""
    if text:
        start_at = float(text)
    else:
        time.sleep(0.001)
assert start_at is not None, "no start stamp appeared within the barrier deadline"
while time.monotonic() < start_at:
    time.sleep(0.001)
entered = time.monotonic()
time.sleep(hold_s)
left = time.monotonic()
with open(record, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"pid": os.getpid(), "entered": entered, "left": left}}) + "\\n")
"""


def _run_concurrently(driver: str, env: dict[str, str], n: int) -> list[int]:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", driver],
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(n)
    ]
    codes = []
    for proc in procs:
        out, _ = proc.communicate(timeout=180)
        codes.append(proc.returncode)
        if proc.returncode != 0:
            print(out, file=sys.stderr)
    return codes


def _intervals(record: Path) -> list[tuple[float, float]]:
    lines = [ln for ln in record.read_text(encoding="utf-8").splitlines() if ln.strip()]
    entries = [json.loads(ln) for ln in lines]
    return sorted((e["entered"], e["left"]) for e in entries)


def _max_overlap_s(intervals: list[tuple[float, float]]) -> float:
    worst = 0.0
    for i, (a_in, a_out) in enumerate(intervals):
        for b_in, b_out in intervals[i + 1 :]:
            worst = max(worst, min(a_out, b_out) - max(a_in, b_in))
    return worst


@pytest.mark.requirement("JIT-03")
def test_the_compile_lock_excludes_concurrent_processes(tmp_path: Path) -> None:
    """[if] two processes are inside the compile at once [then] fail, [else stop].

    if the lock stops excluding then two cold invocations compile into one
    shared cache at once, which is exactly what corrupts it (#1316)"""
    record = tmp_path / "locked.jsonl"
    env = {
        "MDT_LOCK_RECORD": str(record),
        "MDT_LOCK_HOLD_S": "0.6",
        # A cache dir unique to this test, so the lock name is unique to it
        # and a real warm-up elsewhere on this host cannot serialize with it
        # (or, worse, make this test pass by holding the lock itself).
        "NUMBA_CACHE_DIR": str(tmp_path / "numba"),
    }
    codes = _run_concurrently(_LOCKED_DRIVER.format(repo=str(REPO_ROOT)), env, 4)

    assert codes == [0, 0, 0, 0], codes
    intervals = _intervals(record)
    assert len(intervals) == 4, intervals
    overlap = _max_overlap_s(intervals)
    assert overlap <= 0.0, (
        f"two processes were inside the compile together for {overlap:.3f}s; "
        "the cross-process lock is not excluding"
    )
    # Four 0.6s holds serialized end to end, so the run spans >= 2.4s. Without
    # this the assertion above would also pass if every process had crashed
    # before entering, or if the holds had been zero-length.
    span = intervals[-1][1] - intervals[0][0]
    assert span >= 4 * 0.6, (
        f"four serialized 0.6s holds spanned only {span:.3f}s, so they cannot "
        "all have been held"
    )


@pytest.mark.requirement("JIT-03")
def test_negative_control_the_same_probe_overlaps_without_the_lock(
    tmp_path: Path,
) -> None:
    """[if] the unlocked probe does not overlap [then] fail, because the exclusion test above could not have failed either, [else stop].

    if this control ever passes-as-excluded then the exclusion test above is
    proving nothing: it would mean this host never overlaps anyway"""
    record = tmp_path / "unlocked.jsonl"
    env = {
        "MDT_LOCK_RECORD": str(record),
        "MDT_LOCK_HOLD_S": "0.6",
        "MDT_LOCK_READY_N": "4",
    }
    codes = _run_concurrently(_UNLOCKED_DRIVER.format(repo=str(REPO_ROOT)), env, 4)

    assert codes == [0, 0, 0, 0], codes
    intervals = _intervals(record)
    assert len(intervals) == 4, intervals
    overlap = _max_overlap_s(intervals)
    assert overlap > 0.05, (
        f"the UNLOCKED probe overlapped only {overlap:.3f}s, so this host does "
        "not overlap on its own and the locked test above cannot distinguish a "
        "working lock from a missing one"
    )


@pytest.mark.requirement("JIT-03")
def test_lock_path_is_shared_by_processes_that_share_a_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the lock path stops tracking the cache location [then] fail, [else stop].

    if the lock key stopped tracking the cache location then processes
    writing one cache would take different locks and race anyway"""
    monkeypatch.delenv("NUMBA_CACHE_DIR", raising=False)
    default = warmup_lock_path()
    assert default == warmup_lock_path(), "same environment must mean same lock"

    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "a"))
    a = warmup_lock_path()
    assert a != default
    assert warmup_stamp_path().parent == a.parent
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "b"))
    assert warmup_lock_path() != a, (
        "two different cache dirs must not share one lock"
    )
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "a"))
    assert warmup_lock_path() == a, "the key must be a function of the cache dir"


# ---------------------------------------------------------------------------
# Ordering: the warm-up happens in the parent, before any worker
# ---------------------------------------------------------------------------


@pytest.mark.requirement("JIT-01")
def test_the_warmup_completes_in_the_parent_before_any_worker_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a worker runs before the parent's warm-up finished [then] fail, [else stop].

    if the warm-up moved after the pool, or into a child, then workers meet
    a cold shared cache and compile into it concurrently (#1316)

    Checked from inside the real workers, not from stdout: a log line proves
    only that something was printed first.
    """
    from apps.analysis import backends
    from apps.analysis import run as run_mod
    from tests.analysis.pool_probe_backends import WarmupOrderBackend

    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    marker = tmp_path / "warm.marker"
    monkeypatch.setenv("MDT_PROBE_DIR", str(probe_dir))
    monkeypatch.setenv("MDT_WARMUP_MARKER", str(marker))
    monkeypatch.setitem(backends.BACKENDS, "warmorder", WarmupOrderBackend)

    refs = []
    for i in range(4):
        audio = tmp_path / f"t{i}.wav"
        audio.write_bytes(b"0" * 64)
        refs.append(run_mod.TrackRef(stable_id=f"sid{i:05d}", path=audio))

    summary = run_mod.run(
        refs,
        backend_name="warmorder",
        workers=2,
        only_missing=False,
        db_path=tmp_path / "state.db",
    )

    assert summary.failed == 0, summary.errors
    reports = [json.loads(p.read_text()) for p in probe_dir.glob("*.json")]
    assert len(reports) == 4, reports
    assert all(r["warm_seen"] for r in reports), (
        f"a worker ran before the warm-up finished: {reports}"
    )
    driver_pid = os.getpid()
    assert {r["warm_pid"] for r in reports} == {driver_pid}, (
        f"the warm-up ran in a process that is not the driver ({driver_pid}); "
        f"workers saw {[r['warm_pid'] for r in reports]}"
    )
    assert {r["pid"] for r in reports} != {driver_pid}, (
        "fixture precondition: the workers must be separate processes, or this "
        "test proves nothing about ordering across processes"
    )


@pytest.mark.requirement("JIT-01")
def test_a_single_worker_run_still_warms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] --workers 1 skips the warm-up [then] fail, [else stop].

    if --workers 1 skipped the warm-up then the drain's per-chunk
    invocations would each meet a cold shared cache; the corruption needs two
    PROCESSES, not two workers"""
    from apps.analysis import backends
    from apps.analysis import run as run_mod
    from tests.analysis.pool_probe_backends import WarmupOrderBackend

    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    marker = tmp_path / "warm.marker"
    monkeypatch.setenv("MDT_PROBE_DIR", str(probe_dir))
    monkeypatch.setenv("MDT_WARMUP_MARKER", str(marker))
    monkeypatch.setitem(backends.BACKENDS, "warmorder", WarmupOrderBackend)

    audio = tmp_path / "t.wav"
    audio.write_bytes(b"0" * 64)
    run_mod.run(
        [run_mod.TrackRef(stable_id="sid00001", path=audio)],
        backend_name="warmorder",
        workers=1,
        only_missing=False,
        db_path=tmp_path / "state.db",
    )

    assert marker.exists(), "--workers 1 did not warm the JIT cache"
    assert json.loads(marker.read_text())["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# The skip fast path
# ---------------------------------------------------------------------------


class _CountingBackend:
    """Counts compiles against a cache directory it really writes into."""

    name = "counting"
    version = "counting-1.0"
    root: Path
    compiles = 0

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return (cls.root,)

    @classmethod
    def warm_jit_cache(cls) -> str:
        cls.compiles += 1
        target = cls.root / "__pycache__" / f"compiled-{cls.compiles}.nbi"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"artifact" * cls.compiles)
        (cls.root / "__pycache__" / "obj.nbc").write_bytes(b"o" * cls.compiles)
        return f"compiled {cls.compiles}"


@pytest.mark.requirement("JIT-02")
def test_the_warmup_is_skipped_only_while_the_cache_is_what_it_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the skip fires against a cache no serial warm-up vouched for [then] fail, [else stop].

    if the skip fired against a cache a racing writer had touched then a
    half-written cache would be trusted; if it never fired then every chunked
    drain invocation pays the full warm-up again"""
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: tmp_path / "stamp")
    _CountingBackend.root = tmp_path / "cache"
    _CountingBackend.root.mkdir()
    _CountingBackend.compiles = 0

    first = warm_backend_jit(_CountingBackend, backend_name="counting")
    assert not first.skipped, "a cold cache must compile"
    assert _CountingBackend.compiles == 1

    second = warm_backend_jit(_CountingBackend, backend_name="counting")
    assert second.skipped, "an untouched cache must not be recompiled"
    assert _CountingBackend.compiles == 1
    assert "skipped=1" in second.render()

    # A racing writer moves the fingerprint, so the next run must NOT trust it.
    (_CountingBackend.root / "__pycache__" / "obj.nbc").write_bytes(b"raced")
    third = warm_backend_jit(_CountingBackend, backend_name="counting")
    assert not third.skipped, (
        "the cache was written to by something other than a serial warm-up and "
        "was trusted anyway"
    )
    assert _CountingBackend.compiles == 2

    # And a purge is not a skip either.
    purge_cache(_CountingBackend.jit_cache_roots())
    fourth = warm_backend_jit(_CountingBackend, backend_name="counting")
    assert not fourth.skipped
    assert _CountingBackend.compiles == 3


@pytest.mark.requirement("JIT-02")
def test_a_backend_with_no_cache_roots_never_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a backend that cannot vouch for a cache takes the fast path [then] fail, [else stop].

    if an unvouchable backend took the fast path then "I cannot measure this
    cache" would render as "this cache is fine\""""
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: tmp_path / "stamp")

    class _NoRoots:
        name = "noroots"
        calls = 0

        @classmethod
        def jit_cache_roots(cls) -> tuple[Path, ...]:
            return ()

        @classmethod
        def warm_jit_cache(cls) -> str:
            _NoRoots.calls += 1
            return "warmed"

    for _ in range(3):
        assert not warm_backend_jit(_NoRoots, backend_name="noroots").skipped
    assert _NoRoots.calls == 3


@pytest.mark.requirement("JIT-01")
def test_a_backend_that_never_declared_its_jit_cache_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an undeclared backend is defaulted or dies unnamed [then] fail, [else stop].

    if an undeclared backend defaulted to "no cache" then it would silently
    take the nothing-to-protect path, which is the wrong guess; and a bare
    AttributeError would read as a bug in the warm-up rather than as a missing
    declaration (this is how the CLI test doubles broke, twice)"""
    monkeypatch.setattr(jit_warmup, "warmup_stamp_path", lambda: tmp_path / "stamp")

    class _Undeclared:
        name = "undeclared"

    with pytest.raises(TypeError) as caught:
        warm_backend_jit(_Undeclared, backend_name="undeclared")  # type: ignore[arg-type]

    message = str(caught.value)
    assert "undeclared" in message
    assert "jit_cache_roots" in message and "warm_jit_cache" in message

    class _HalfDeclared:
        name = "half"

        @classmethod
        def jit_cache_roots(cls) -> tuple[Path, ...]:
            return ()

    with pytest.raises(TypeError) as half:
        warm_backend_jit(_HalfDeclared, backend_name="half")  # type: ignore[arg-type]
    assert "warm_jit_cache" in str(half.value)
    assert "jit_cache_roots" not in str(half.value), (
        "the error must name only what is missing, or it cannot be acted on"
    )


@pytest.mark.slow
@pytest.mark.requirement("JIT-02")
def test_a_real_cache_is_vouched_for_by_its_toolchain_and_stamp_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a stale real cache is trusted, or a stamped one purged [then] fail, [else stop].

    Stale: stamp gone (writer killed before stamping) or a different toolchain.
    Two real librosa compiles (~25-45 s each), so slow: Full CI keeps it.

    if a stale cache were kept then a killed or racing writer's artifacts, or
    a previous librosa's, would be loaded and segfault; if a stamped cache
    were purged then every job on a persistent per-runner cache would pay the
    cold compile again"""
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "cache"))
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700)
    monkeypatch.setenv("MDT_NUMBA_WARMUP_DIR", str(lock_dir))
    backend = get_backend("librosa")
    roots = tuple(backend.jit_cache_roots())
    assert roots == (tmp_path / "cache",), roots
    assert not stamp_vouches_for(roots), "an empty cache has nothing to vouch for"

    cold = warm_backend_jit(backend, backend_name="librosa", purge_stale=True)
    assert not cold.skipped and cold.purged == 0 and cold.lock_held
    assert cache_fingerprint(roots), "the real backend compiled nothing"
    assert stamp_vouches_for(roots), "the serial warm-up's own cache must be vouched for"
    assert warm_backend_jit(backend, backend_name="librosa", purge_stale=True).skipped

    compiled_by = cache_fingerprint(roots)
    assert cache_fingerprint(roots, identity=toolchain_identity() + "-upgraded") != compiled_by, (
        "the fingerprint ignored the toolchain that compiled the artifacts"
    )
    assert toolchain_identity().startswith(f"py{sys.version_info[0]}.{sys.version_info[1]}-numba")

    # A writer killed before it could stamp: real artifacts, no witness. The
    # repair runs in a FRESH process, as the CI step does: this process has
    # the dispatchers compiled in memory and would write nothing after a purge.
    warmup_stamp_path().unlink()
    assert not stamp_vouches_for(roots)
    repaired = subprocess.run(
        [sys.executable, "-m", "apps.analysis.jit_warmup",
         "--backend", "librosa", "--purge-if-stale"],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert re.search(r"purged=[1-9]\d*", repaired.stdout), repaired.stdout
    assert stamp_vouches_for(roots), "the purge-and-warm must leave a vouched-for cache"
