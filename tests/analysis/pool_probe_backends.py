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
from datetime import datetime, timezone
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
            analyzed_at=datetime.now(timezone.utc),
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
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        if "sleeper" in Path(path).name:
            time.sleep(600)
            raise AssertionError("the sleeper must be terminated, not finish")
        time.sleep(float(os.environ.get("MDT_SELFKILL_DELAY", "0")))
        os.kill(os.getpid(), int(os.environ["MDT_SELFKILL_SIGNAL"]))
        raise AssertionError("a signalled worker must not return")
