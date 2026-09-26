"""The key lane warms the numba cache it compiles into, before its workers race.

- [if] key workers meet a cold numba cache [then] the parent already warmed it, [else stop].

NATIVE-10 (issue #2315), found by the offline acceptance run. ``own_key``
declared no JIT cache and warmed nothing, on the stated grounds that
``chroma_cqt`` "compiles into the process-wide numba cache the runner owns".
Nothing warmed that cache for it: the queue runner warms only the backend it
is draining. So a first key drain on a fresh install met a cold cache with four
spawn workers, all four compiled librosa's ``pitch._pi_wrapper`` gufunc
(``chroma_cqt`` -> ``estimate_tuning`` -> ``piptrack``) at once, and the
interleaved writes left a cache every later process dies loading: SIGSEGV in
``numba/np/ufunc/gufunc.py``, BrokenProcessPool, the lane dead for good. That
is issue #1316's crash on the one lane the #1316 fix never covered. Measured
Sat 26 Sep 2026: a single process pointed at the poisoned cache segfaults on
the first track; the same track against a clean cache succeeds.

Real subject, nothing mocked: a purged dedicated ``NUMBA_CACHE_DIR``, the real
warm-up CLI, then the real ``analyze_audio`` in a separate process on real
decoded audio.

Regression lines:
  - if the own_key warm-up writes no numba artifacts then the key lane's
    workers still compile concurrently on a cold cache (#1316 on the key lane)
  - if analyze_audio writes to the cache after the own_key warm-up then the
    warm-up does not compile what the key lane runs
  - if own_key.jit_cache_roots ignores NUMBA_CACHE_DIR then the warm-up's
    stamp fingerprints the wrong directory and never vouches
"""
from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import textwrap
import wave
from pathlib import Path

import pytest

from apps.analysis.backends.own_key import BACKEND_NAME
from apps.analysis.jit_warmup import cache_fingerprint

pytestmark = pytest.mark.requirement("NATIVE-10")

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A rate the warm-up does not use, so a pass also shows numba's cache is keyed
#: on dtype and not on sample rate (``analyze_audio`` decodes at native rate).
REAL_RATE = 44_100


def _chord(path: Path, seconds: float = 8.0) -> Path:
    """A C major triad: tonal enough for the full chroma path to run."""
    freqs = (261.63, 329.63, 392.0)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(REAL_RATE)
        out.writeframes(b"".join(
            struct.pack("<h", int(7000 * sum(
                math.sin(2 * math.pi * f * i / REAL_RATE) for f in freqs) / 3))
            for i in range(int(seconds * REAL_RATE))))
    return path


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        check=False,
    )


def test_jit_cache_roots_follow_numba_cache_dir(tmp_path: Path) -> None:
    env = {**os.environ, "NUMBA_CACHE_DIR": str(tmp_path)}
    proc = _run([sys.executable, "-c", (
        "from apps.analysis.backends.own_key import OwnKeyBackfillBackend as B;"
        "print([str(p) for p in B.jit_cache_roots()])")], env)
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.strip().splitlines()[-1] == repr([str(tmp_path)])


@pytest.mark.slow
@pytest.mark.requires_audio_stack
@pytest.mark.requires_ffmpeg
def test_warmup_compiles_everything_the_key_lane_runs(tmp_path: Path) -> None:
    cache = tmp_path / "numba-cache"
    cache.mkdir()
    env = {**os.environ, "NUMBA_CACHE_DIR": str(cache)}

    warm = _run([sys.executable, "-m", "apps.analysis.jit_warmup",
                 "--backend", BACKEND_NAME, "--purge"], env)
    assert warm.returncode == 0, warm.stdout
    warmed = cache_fingerprint((cache,))
    assert warmed, f"the own_key warm-up wrote no numba artifacts:\n{warm.stdout}"
    pitch = sorted(p.name for p in cache.rglob("*pitch*.nbi"))
    assert pitch, (
        "the warm-up did not compile librosa's pitch gufunc, the one the "
        f"key lane's workers were seen racing on: {sorted(cache.rglob('*.nbi'))}"
    )

    audio = _chord(tmp_path / "chord.wav")
    analyze = _run([sys.executable, "-c", textwrap.dedent(f"""
        from pathlib import Path
        from apps.analysis.backends.own_key import analyze_audio
        record = analyze_audio(Path({str(audio)!r}), "sid_chord",
                               db_path=Path({str(tmp_path / "absent.db")!r}))
        lane = record.lanes["key"]
        print("status", lane.status)
    """)], env)
    assert analyze.returncode == 0, analyze.stdout
    assert "status " in analyze.stdout, analyze.stdout
    assert cache_fingerprint((cache,)) == warmed, (
        "analyze_audio wrote numba artifacts after the own_key warm-up, so the "
        "key lane's workers would still compile concurrently on a cold cache"
    )
