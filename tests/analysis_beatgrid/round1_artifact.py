"""The committed round-1 runner artifact, restamped to the current producer version.

`ops/beatbench/round-1/raw-beatgrid_lane_t050.json` is real Beat This! output
written by the 1.2.0 runner. `record_from_payload` refuses a payload whose
stated `producer_version` differs from the package's, which is what re-queues
a library on a version bump in production. The model half of the producer (the
runner, its weights, its peak picker) is unchanged since 1.2.0: 1.3.0 changed
only the octave policy DOWNSTREAM of these beats (`bpm.choose_octave`,
`tempo_family.py`). So the beats are still valid input, and the record-writing
tests read them through this loader, which says so, rather than each test
file silently rewriting the stamp. When a version bump DOES change the runner,
this loader is wrong and the artifact must be regenerated instead.

-Claude
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.analysis_beatgrid.version import PRODUCER_VERSION

ROUND1_RAW = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "beatbench"
    / "round-1"
    / "raw-beatgrid_lane_t050.json"
)

#: The runner version that wrote the artifact. Pinned so a regenerated
#: artifact (or a runner-side bump) fails here loudly instead of being
#: restamped by accident.
ARTIFACT_RUNNER_VERSION = "1.2.0"

#: Producer versions whose runner half is identical to ARTIFACT_RUNNER_VERSION.
RUNNER_UNCHANGED_SINCE_ARTIFACT = ("1.2.0", "1.3.0")


def load_round1_raw() -> dict[str, Any]:
    whole = json.loads(ROUND1_RAW.read_text(encoding="utf-8"))
    assert whole["producer_version"] == ARTIFACT_RUNNER_VERSION, whole["producer_version"]
    assert PRODUCER_VERSION in RUNNER_UNCHANGED_SINCE_ARTIFACT, (
        f"producer {PRODUCER_VERSION} is not declared runner-identical to the "
        f"{ARTIFACT_RUNNER_VERSION} artifact; regenerate it or extend the tuple"
    )
    return {**whole, "producer_version": PRODUCER_VERSION}
