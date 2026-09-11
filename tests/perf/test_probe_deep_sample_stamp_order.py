"""When a deep probe sample is stamped, relative to the expensive work it does.

This is the load-bearing premise behind `_agreeing_cadence`'s smallest-repeating
rule in scripts/perf/trace_overlap.py, so it is pinned here rather than left as
a reading of the source.

A periodic deep sample overruns `--interval`, and `_run_sampling_loop` then
sleeps zero and starts the next sample immediately. Whether that shows up in the
log as a LONG gap or a SHORT one depends entirely on where the record's
`timestamp` sits: `OpenDJProbe.sample` stamps it while building the record dict
and only THEN runs `_deep_vmmap`, so the expensive work lands in the gap ENDING
at the next sample. Overruns therefore lengthen a gap; they never shorten one,
and no recurring short gap can be manufactured by deep sampling.

That direction is what lets `_agreeing_cadence` take the smallest repeating
cluster: outages and overruns both push gaps LONGER, so the smallest recurring
width is the scheduled cadence. If the stamp ever moves after `_deep_vmmap`,
that reasoning inverts and the cadence rule silently starts electing catch-up
gaps as the cadence, under-widening every counter. This test fails loudly
instead (Codex P1, #1611, discussion_r3970682730, rebutted on this premise).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from scripts.diagnostics.opendj_performance_probe import OpenDJProbe
from scripts.diagnostics.probe_types import ProcessRow

DEEP_WORK_SECONDS = 0.4
SHELL_PID = 4242


@dataclass
class _FakeUsage:
    """The `proc_pid_rusage` struct's fields, at the adapter boundary.

    Faked for the same reason `_FakeNative` in test_probe_process_family.py
    is: `DarwinProcessMetrics` is a Darwin-only ctypes adapter, and this test
    is about WHEN `sample()` stamps its record, which is decoupled from how
    the numbers reached it. Faking the boundary keeps the test running on
    every runner instead of only the macOS shards; the values are never
    asserted on.
    """

    proc_start_abstime: int = 0
    user_time: int = 0
    system_time: int = 0
    diskio_bytesread: int = 0
    diskio_byteswritten: int = 0
    phys_footprint: int = 0
    lifetime_max_phys_footprint: int = 0
    resident_size: int = 0
    wired_size: int = 0
    pageins: int = 0


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "UNAVAILABLE: `sample()` reads machine metrics through sysctl keys that "
        "exist only on Darwin, and the probe this invariant belongs to is macOS "
        "only. Faking every remaining platform call to force a green here would "
        "leave the assertion measuring the fakes rather than the probe."
    ),
)
def test_a_deep_sample_is_stamped_before_its_expensive_vmmap_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If broken: an overrunning deep sample SHORTENS the following gap.

    Asserted as a duration, not by reading the source: the record's own
    timestamp must predate the moment `sample()` returned by at least the
    whole deep-vmmap cost.
    """
    rows = [
        ProcessRow(
            pid=SHELL_PID, ppid=1, pgid=SHELL_PID,
            command="/Applications/x/MacOS/opendj-desktop",
        )
    ]
    monkeypatch.setattr(
        "scripts.diagnostics.opendj_performance_probe.process_table", lambda: rows
    )

    def slow_deep_vmmap(self, processes):
        time.sleep(DEEP_WORK_SECONDS)
        return {"available": False, "reason": "stand-in for the real vmmap cost"}

    monkeypatch.setattr(OpenDJProbe, "_deep_vmmap", slow_deep_vmmap)

    probe = OpenDJProbe(SHELL_PID, ())
    monkeypatch.setattr(probe, "native", _StubNative())
    record = probe.sample(deep=True)
    returned_at = datetime.now(tz=UTC)

    # Premise control: the deep branch must actually have run, or this test
    # would pass on a sample that never paid the cost it is measuring.
    assert "deep_vmmap" in record, "the deep branch did not run; this proves nothing"

    stamped_at = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    stamp_to_return = (returned_at - stamped_at).total_seconds()
    assert stamp_to_return >= DEEP_WORK_SECONDS, (
        "the record was stamped AFTER the deep vmmap, so an overrunning deep "
        "sample now shortens the next gap instead of lengthening it. "
        "_agreeing_cadence's smallest-repeating rule assumes the opposite and "
        f"will elect catch-up gaps as the cadence. stamp to return: {stamp_to_return}s"
    )


class _StubNative:
    """The Darwin metrics adapter, standing still so the timing is the probe's.

    Returns the same zeroed struct for any pid. Nothing here is asserted on:
    the only quantity this module measures is the delay between the record's
    timestamp and the return of `sample()`, so the adapter must contribute no
    time of its own and no platform dependency.
    """

    def read(self, pid: int) -> _FakeUsage:
        return _FakeUsage()

    def ticks_to_seconds(self, ticks: int) -> float:
        return float(ticks)
