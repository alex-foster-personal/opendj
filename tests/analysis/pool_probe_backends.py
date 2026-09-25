"""Analysis backends that report on, or deliberately kill, their own worker.

A separate module on purpose. The pool hands workers the backend CLASS, so a
spawned child imports THIS module when it unpickles the work item, which is
after :func:`apps.analysis.worker_diagnostics.init_worker` has run. The
import-time constants below therefore record the state the initializer left
behind, which is exactly the invariant the pool has to guarantee: the thread
pins are in place before the backend, and so before numpy and numba, load.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from apps.analysis.record import AnalysisRecord
from apps.analysis.worker_diagnostics import THREAD_PIN_VARS

#: The pins as they stood when this module was imported. In a pool worker the
#: import happens after the initializer, so every value must be "1".
PINS_AT_IMPORT: dict[str, str | None] = {
    name: os.environ.get(name) for name in THREAD_PIN_VARS
}

#: Whether numpy had already been imported when this module was. A pool worker
#: must reach here before numpy, or the pins above are decoration.
NUMPY_AT_IMPORT: bool = "numpy" in sys.modules

#: The pid that imported this module. A SPAWNED child imports it itself, so
#: this is its own pid; a FORKED child inherits the parent's value.
IMPORT_PID: int = os.getpid()


class ProbeBackend:
    """Writes one JSON report per analyzed track, then returns a valid record."""

    name = "probe"
    version = "probe-1.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return "probe backend has no JIT cache"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        out = Path(os.environ["MDT_PROBE_DIR"]) / f"{uuid.uuid4().hex}.json"
        out.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "import_pid": IMPORT_PID,
                    "pins_at_import": PINS_AT_IMPORT,
                    "numpy_at_import": NUMPY_AT_IMPORT,
                }
            )
        )
        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=cls.version,
            analyzed_at=datetime.now(UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.5,
            key_camelot="8A",
            key_openkey="8m",
            key_confidence=0.5,
            energy=5,
        )


class SelfKillingBackend:
    """Kills its own worker with the signal named by the environment.

    A path whose name contains ``sleeper`` never dies on its own: it stays busy
    so the executor has a surviving worker to terminate during teardown, which
    is what produces a real mixed crash/cleanup signal set.
    """

    name = "selfkill"
    version = "selfkill-1.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return "selfkill backend has no JIT cache"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        if "sleeper" in Path(path).name:
            time.sleep(600)
            raise AssertionError("the sleeper must be terminated, not finish")
        time.sleep(float(os.environ.get("MDT_SELFKILL_DELAY", "0")))
        os.kill(os.getpid(), int(os.environ["MDT_SELFKILL_SIGNAL"]))
        raise AssertionError("a signalled worker must not return")


class WarmupOrderBackend:
    """Records, from inside each real worker, whether the warm-up ran first.

    The ordering claim this backend exists to test - "the JIT cache is warmed
    before any second process exists" - cannot be checked by reading stdout,
    because a log line proves only that something was PRINTED first. So the
    warm-up drops a marker file and every worker reports whether it could see
    that marker at the moment it ran, and which pid wrote it. A worker that
    started before the warm-up finished reports ``warm_seen: false``, and a
    warm-up that ran in a CHILD rather than the parent reports a pid that is
    not the driver's.

    ``jit_cache_roots`` is empty on purpose: with no cache to fingerprint the
    warm-up can never take its skip path, so every run under this backend
    exercises the ordering rather than accidentally testing the fast path.
    """

    name = "warmorder"
    version = "warmorder-1.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        marker = Path(os.environ["MDT_WARMUP_MARKER"])
        marker.write_text(json.dumps({"pid": os.getpid()}))
        return "warm-up marker written"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        marker = Path(os.environ["MDT_WARMUP_MARKER"])
        seen = marker.exists()
        out = Path(os.environ["MDT_PROBE_DIR"]) / f"{uuid.uuid4().hex}.json"
        out.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "warm_seen": seen,
                    "warm_pid": json.loads(marker.read_text())["pid"] if seen else None,
                }
            )
        )
        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=cls.version,
            analyzed_at=datetime.now(UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.5,
            key_camelot="8A",
            key_openkey="8m",
            key_confidence=0.5,
            energy=5,
        )


class LockProbeBackend:
    """Records the wall-clock interval its warm-up occupied, from inside it.

    The claim under test is mutual exclusion: two processes must not be inside
    ``warm_jit_cache`` at the same moment. That is a statement about intervals,
    so the probe records intervals rather than a completion order - two
    processes that overlapped completely still finish in SOME order, and an
    order alone would report that as a pass.

    Each process appends ONE short json line under ``O_APPEND``, which the
    kernel keeps whole for a write this size, so a reader sees every
    participant or none rather than a spliced record.

    ``jit_cache_roots`` is empty, so the warm-up can never take its skip path
    and every run of this probe really enters the locked region.
    """

    name = "lockprobe"
    version = "lockprobe-1.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        record = Path(os.environ["MDT_LOCK_RECORD"])
        hold_s = float(os.environ.get("MDT_LOCK_HOLD_S", "0.6"))
        entered = time.monotonic()
        time.sleep(hold_s)
        left = time.monotonic()
        with record.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"pid": os.getpid(), "entered": entered, "left": left})
                + "\n"
            )
        return f"lock probe held {hold_s:g}s"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:  # pragma: no cover
        raise AssertionError("LockProbeBackend exists to warm, never to analyze")
