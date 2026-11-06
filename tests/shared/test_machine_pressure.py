"""The machine-pressure reading: cached, aged honestly, never zero-filled.

The whole reason this endpoint exists is that a timing number without its
conditions is an anecdote, so the cases that matter most here are the ones
where nothing could be measured. A reading that renders as `0.0` when the
sampler is missing would be worse than no endpoint at all: it would let a
deck-load row claim an idle machine on a box nobody sampled, which is the
exact defect (private path) forbids.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from apps.engine_core.parent_watch import PARENT_ENV, PARENT_FILE_NAME
from apps.shared import machine_pressure
from apps.shared.machine_pressure import (
    CACHE_TTL_SECONDS,
    ELECTRON_GPU_ROLE,
    ELECTRON_RENDERER_ROLE,
    ELECTRON_UTILITY_ROLE,
    FFMPEG_ROLE,
    LAUNCHING_SHELL_PID_ENV,
    PRESSURE_CHURN_WEIGHT_SWAP,
    PRESSURE_SAMPLE_P95_WALL_MS,
    SHELL_REGISTRATION_FILE_NAME,
    MachinePressureCache,
    ProcessInfo,
    _family_root_pid,
    launching_shell_pid,
    live_process_family,
    live_process_family_members,
    live_process_family_state,
    opendj_process_name,
    read_machine_pressure,
    read_shell_registration,
    sample_wall_p95_ms,
)
from scripts.diagnostics.probe_process_family import (
    opendj_process_name as probe_opendj_process_name,
)
from tests.real_process_tree import (
    PARENT_PID,
    argv_node,
    kill,
    needs_fork,
    needs_visible_titles,
    node,
    spawn,
)
from tests.sleep_spy import spy_on_own_thread_sleep

FULL_SAMPLE: dict[str, Any] = {
    "physical_memory_mb": 16384.0,
    "kernel_memory_pressure_level": 1,
    "swap_raw": "total = 8192.00M  used = 6535.38M  free = 1656.62M  (encrypted)",
    "swap_total_mb": 8192.0,
    "swap_used_mb": 6535.4,
    "swap_free_mb": 1656.6,
    "load_average_1m": 5.76,
    "load_average_5m": 7.94,
    "load_average_15m": 7.6,
    "free_memory_mb": 67.7,
}


def _counting_sampler(payload: dict[str, Any]) -> tuple[Any, list[int]]:
    calls: list[int] = []

    def sampler() -> dict[str, Any]:
        calls.append(1)
        return dict(payload)

    return sampler, calls


def test_full_sample_exposes_the_three_load_row_fields() -> None:
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler(FULL_SAMPLE)

    body = cache.read(now=100.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert body["available"] is True
    assert body["load_avg_1m"] == pytest.approx(5.76)
    assert body["mem_free_mb"] == pytest.approx(67.7)
    assert body["swap_used_mb"] == pytest.approx(6535.4)
    assert body["cache_age_ms"] == pytest.approx(0.0)


def test_the_response_is_an_allowlist_not_a_passthrough() -> None:
    """A sampler that grows a key must not widen what the browser is handed."""

    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({**FULL_SAMPLE, "command_line": "/secret/path --token=abc"})

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert "command_line" not in body
    assert "swap_raw" not in body
    assert "physical_memory_mb" not in body
    allowed = {
        "available",
        "cache_age_ms",
        "load_avg_1m",
        "mem_free_mb",
        "swap_used_mb",
        "kernel_memory_pressure_level",
        "band",
        "sample_interval_ms",
        "sample_wall_ms",
        "sample_wall_p95_ms",
    }
    assert set(body) <= allowed


def test_a_second_read_inside_the_ttl_reuses_the_sample_and_ages_it() -> None:
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler(FULL_SAMPLE)

    first = cache.read(now=1000.0, sampler=sampler, vm_stat_reader=lambda: None)
    second = cache.read(now=1000.5, sampler=sampler, vm_stat_reader=lambda: None)

    assert len(calls) == 1, "the second read must not shell out again"
    assert first["cache_age_ms"] == pytest.approx(0.0)
    # The age is the POINT of the cache being visible: a caller that gets a
    # 0.5 s old reading has to be told so, or it will stamp it as fresh.
    assert second["cache_age_ms"] == pytest.approx(500.0)


def test_a_read_past_the_ttl_takes_a_new_sample() -> None:
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler(FULL_SAMPLE)

    cache.read(now=1000.0, sampler=sampler, vm_stat_reader=lambda: None)
    fresh = cache.read(now=1001.1, sampler=sampler, vm_stat_reader=lambda: None)

    assert len(calls) == 2
    assert fresh["cache_age_ms"] == pytest.approx(0.0)


def test_a_missing_sampler_reports_unavailable_and_NEVER_a_zero() -> None:
    cache = MachinePressureCache()

    def sampler() -> dict[str, Any]:
        raise ImportError("No module named 'scripts'")

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert body["available"] is False
    assert "scripts" in body["reason"]
    for key in ("load_avg_1m", "mem_free_mb", "swap_used_mb"):
        assert key not in body, f"{key} must be absent, not zero, when nothing was measured"


def test_a_sampler_that_raises_os_error_reports_unavailable() -> None:
    cache = MachinePressureCache()

    def sampler() -> dict[str, Any]:
        raise OSError("vm_stat: no such file")

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert body["available"] is False
    assert "vm_stat" in body["reason"]


def test_a_sampler_that_reads_nothing_is_unavailable_rather_than_empty_true() -> None:
    """An empty reading is a failed measurement, not a successful one."""

    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"physical_memory_mb": 16384.0})

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert body["available"] is False
    assert "no readable field" in body["reason"]


def test_a_partial_sample_exposes_only_what_was_read() -> None:
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"load_average_1m": 12.5})

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert body["available"] is True
    assert body["load_avg_1m"] == pytest.approx(12.5)
    assert "mem_free_mb" not in body
    assert "swap_used_mb" not in body


def test_the_shared_ttl_is_under_the_clients_ten_second_poll() -> None:
    """A TTL at or above the poll would hand every poll a stale sample."""

    assert 0 < CACHE_TTL_SECONDS < 10.0


def test_the_process_wide_reader_answers_on_this_machine() -> None:
    """Not a mock: the real sampler, on the real box this suite runs on.

    Asserted as a DISJUNCTION on purpose. On a dev checkout the diagnostics
    package imports and this returns real numbers; inside a packaged app
    `scripts` is absent and it must say so. Both are correct answers; a body
    that is neither -- available true with no numbers, or available false with
    no reason -- is the failure this catches.
    """

    body = read_machine_pressure()

    assert body["cache_age_ms"] >= 0
    if body["available"]:
        readings = [k for k in ("load_avg_1m", "mem_free_mb", "swap_used_mb") if k in body]
        assert readings, "available=true must carry at least one real reading"
        assert all(isinstance(body[key], float) for key in readings)
    else:
        assert body["reason"]


# --------------------------------------------------------------------------
# REAL PATH. The suite above drives the cache with injected samplers, which is
# how the failure branches (a sampler that raises, one that reads nothing, one
# whose keys the allowlist must drop) are reachable at all -- the real machine
# cannot be asked to run out of memory on demand. What that leaves untested is
# the production command, and this closes it: no injection, no patching, the
# real sampler and the real route.
#
# These assert the CONTRACT rather than a value, because the value is whatever
# the machine happens to be doing and CI is not this Mac. The contract is the
# part that matters and the part a regression would break: a field is either a
# finite number or ABSENT, and never present-and-zero standing in for
# "unmeasured".
# --------------------------------------------------------------------------


def test_the_real_sampler_reports_finite_numbers_or_nothing_at_all() -> None:
    """The production path, unpatched, on whatever machine is running this."""
    body = read_machine_pressure()

    assert isinstance(body["available"], bool)
    if not body["available"]:
        # The packaged-app branch: `scripts` is not staged, so the sampler is
        # not importable. It must say so and must not invent numbers.
        assert isinstance(body["reason"], str) and body["reason"] != ""
        for name, _ in machine_pressure._EXPOSED_FIELDS:
            assert name not in body, f"{name} present on an unavailable reading"
        return

    assert isinstance(body["cache_age_ms"], float)
    assert body["cache_age_ms"] >= 0.0
    present = [name for name, _ in machine_pressure._EXPOSED_FIELDS if name in body]
    assert present, "an available reading that carries no field is not a reading"
    for name in present:
        value = body[name]
        assert isinstance(value, float), f"{name} is {type(value)!r}, not a float"
        assert math.isfinite(value), f"{name} is {value}, which is not a measurement"
        assert value >= 0.0, f"{name} is negative"


def test_the_real_load_average_is_a_plausible_reading_not_a_placeholder() -> None:
    """A negative control on the value itself.

    Zero is both a value and an error signature here: a sampler that silently
    returned nothing would produce 0.0, which reads as an idle machine. A real
    load average on a machine running this test is strictly positive.
    """
    body = read_machine_pressure()
    if not body["available"] or "load_avg_1m" not in body:
        pytest.skip("this machine's sampler reports no load average")

    assert body["load_avg_1m"] > 0.0, (
        "a load average of exactly zero on a machine busy enough to run pytest "
        "is the sampler failing, not the machine idling"
    )


@pytest.mark.requirement("PERFMODE-05")
def test_unreadable_kernel_pressure_is_absent_never_zero() -> None:
    """[if] the sampler omits [then] the response omits, [else stop]."""
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"load_average_1m": 1.0})

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert "kernel_memory_pressure_level" not in body
    assert body.get("kernel_memory_pressure_level") != 0


@pytest.mark.requirement("PERFMODE-05")
def test_kernel_pressure_zero_is_treated_as_unreadable() -> None:
    """[if] kernel_memory_pressure_level is zero [then] the field is omitted as, [else stop]."""
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"load_average_1m": 1.0, "kernel_memory_pressure_level": 0})

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert "kernel_memory_pressure_level" not in body


@pytest.mark.requirement("PERFMODE-05")
def test_churn_score_uses_ram_cleanup_weight_ten() -> None:
    """[if] vm_stat deltas include swap and [then] churn_score weights swap, [else stop]."""
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"load_average_1m": 1.0})
    prior = {
        "swapins": 0,
        "swapouts": 0,
        "decompressions": 0,
        "page_size": 16384,
    }
    current = {
        "swapins": 2,
        "swapouts": 1,
        "decompressions": 20,
        "page_size": 16384,
    }
    reads = [prior, current]
    cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))
    body = cache.read(now=1.0, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))

    assert body["churn_score"] == pytest.approx(3 * PRESSURE_CHURN_WEIGHT_SWAP + 20)


@pytest.mark.requirement("PERFMODE-05")
def test_first_sample_omits_churn_rather_than_zero() -> None:
    """[if] no prior vm_stat snapshot exists [then] churn_score is omitted, [else stop]."""
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"load_average_1m": 1.0})
    snapshot = {
        "swapins": 1,
        "swapouts": 1,
        "decompressions": 1,
        "page_size": 16384,
    }

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: dict(snapshot))

    assert "churn_score" not in body


def test_read_vm_stat_churn_snapshot_parses_through_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Completed:
        stdout = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Swapins:                                    42.\n"
            "Swapouts:                                   7.\n"
            "Decompressions:                          1234.\n"
            "Pages stored in compressor:               890.\n"
        )

    monkeypatch.setattr(machine_pressure.subprocess, "run", lambda *_a, **_k: _Completed())

    snapshot = machine_pressure._read_vm_stat_churn_snapshot()

    assert snapshot == {
        "swapins": 42,
        "swapouts": 7,
        "decompressions": 1234,
        "page_size": 16384,
        "compressor_pages": 890,
    }


@pytest.mark.requirement("PERFMODE-05")
def test_elevated_kernel_lengthens_the_ttl_to_five_seconds() -> None:
    """[if] kernel_memory_pressure_level is elevated [then] TTL is 5s, [else stop]."""
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler({"load_average_1m": 1.0, "kernel_memory_pressure_level": 2})

    cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)
    cache.read(now=1.1, sampler=sampler, vm_stat_reader=lambda: None)
    cache.read(now=5.1, sampler=sampler, vm_stat_reader=lambda: None)

    assert len(calls) == 2


@pytest.mark.requirement("PERFMODE-05")
def test_elevated_churn_lengthens_even_when_kernel_is_fine() -> None:
    """[if] churn_score is elevated while kernel pressure is [then] TTL is still, [else stop]."""
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler({"load_average_1m": 1.0, "kernel_memory_pressure_level": 1})
    prior = {
        "swapins": 0,
        "swapouts": 0,
        "decompressions": 0,
        "page_size": 16384,
    }
    current = {
        "swapins": 50,
        "swapouts": 0,
        "decompressions": 0,
        "page_size": 16384,
    }
    reads = [prior, current, current]

    cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))
    cache.read(now=1.0, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))
    cache.read(now=5.1, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))

    assert len(calls) == 2


@pytest.mark.requirement("PERFMODE-05")
def test_idle_ttl_is_one_second() -> None:
    """[if] kernel pressure and churn are both idle [then] TTL is 1s, [else stop]."""
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler({"load_average_1m": 1.0, "kernel_memory_pressure_level": 1})
    prior = {
        "swapins": 0,
        "swapouts": 0,
        "decompressions": 0,
        "page_size": 16384,
    }
    current = {"swapins": 1, "swapouts": 0, "decompressions": 1, "page_size": 16384}
    reads = [prior, current, current]

    cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))
    cache.read(now=1.1, sampler=sampler, vm_stat_reader=lambda: reads.pop(0))

    assert len(calls) == 2


@pytest.mark.requirement("PERFMODE-05")
def test_sampler_does_not_sleep_to_build_churn(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] machine pressure is read on the request path [then] time.sleep is never, [else stop]."""

    def fail_sleep(_seconds: float) -> None:
        raise AssertionError("time.sleep must not run on the request path")

    # Thread-scoped: a leaked background thread's sleep is not the request path.
    spy_on_own_thread_sleep(monkeypatch, fail_sleep)
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler({"load_average_1m": 1.0})
    snapshot = {
        "swapins": 1,
        "swapouts": 1,
        "decompressions": 1,
        "page_size": 16384,
    }

    cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: dict(snapshot))


@pytest.mark.requirement("PERFMODE-05")
def test_live_members_list_unnamed_rather_than_omitting() -> None:
    """[if] a process family includes [then] live_process_family_members lists them, [else stop]."""
    rows = [
        ProcessInfo(pid=100, ppid=1, command="opendj-engine --name opendj-engine", rss_bytes=1000),
        ProcessInfo(pid=101, ppid=100, command="python3 -m apps.engine_core serve", rss_bytes=500),
    ]

    members = live_process_family_members(engine_pid=100, process_iter=lambda: rows)

    assert len(members) == 2
    unnamed = [member for member in members if member["name"] == "unnamed"]
    assert len(unnamed) == 1


@pytest.mark.requirement("PERFMODE-05")
def test_live_walk_does_not_emit_command_or_pid() -> None:
    """[if] live_process_family_members returns member [then] each record omits, [else stop]."""
    rows = [
        ProcessInfo(pid=100, ppid=1, command="opendj-engine --name opendj-engine", rss_bytes=1000),
    ]

    members = live_process_family_members(engine_pid=100, process_iter=lambda: rows)

    assert set(members[0]) <= {"name", "rss_mb", "physical_footprint_mb", "role"}


def test_a_process_that_exits_mid_walk_does_not_empty_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """If one process exiting mid-walk empties the walk, then under runner load the
    family reads as unavailable (main CI, Thu 5 Nov 2026, run 41972082377)."""

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    gone = psutil.Process(child.pid)
    assert child.wait(timeout=20) == 0  # exited and reaped: every live call on it now raises
    # What psutil prefetches for a process that is gone by the time it is read.
    gone.info = {"pid": child.pid, "ppid": os.getpid(), "cmdline": None, "name": None, "memory_info": None}
    real_iter = psutil.process_iter

    def iter_with_the_exited_process(*args: Any, **kwargs: Any) -> Iterator[psutil.Process]:
        yield gone
        yield from real_iter(*args, **kwargs)

    monkeypatch.setattr(psutil, "process_iter", iter_with_the_exited_process)

    rows = machine_pressure._read_process_rows(None)

    assert os.getpid() in {row.pid for row in rows}, "the walk was emptied by one exited process"


# A packaged app beside a dev checkout on one machine, as REAL processes read
# back through the production psutil walk (tests/real_process_tree.py).
# THIS app: the desktop shell runs the engine; the engine runs the odj-audio
# sidecar and a job worker; the shell has another child of its own.
# ELSEWHERE, re-parented out of the test's tree: a second engine, and a vite
# dev server titled opendj-frontend with its esbuild child. A dev engine under
# a ``uv`` launcher whose command only passes an ``opendj-*`` data dir, beside
# a vite under the same shell. A ``--reload`` backend under its supervisor.
_SHELL = "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"
_WEBKIT_WEBCONTENT = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"
)
_WEBKIT_GPU = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.GPU.xpc/Contents/MacOS/com.apple.WebKit.GPU"
)
_WEBKIT_NETWORKING = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.Networking.xpc/Contents/MacOS/com.apple.WebKit.Networking"
)


@pytest.mark.requirement("PERF-UI-6785")
def test_live_walk_attributes_the_launchd_parented_webkit_helpers_to_the_shell() -> None:
    """[if] shell, engine, engine child and WebKit XPC helpers within 45 s [then] members carry probe roles, [else stop]."""
    rows = [
        ProcessInfo(10, 1, _SHELL, rss_bytes=1_000_000, start_time=1000.0),
        ProcessInfo(20, 10, "python3 -m apps.engine_core serve", rss_bytes=2_000_000, start_time=1002.0),
        ProcessInfo(
            21,
            20,
            "/bin/ffmpeg -nostdin -i /tmp/a.m4a -",
            rss_bytes=500_000,
            start_time=1010.0,
        ),
        ProcessInfo(30, 1, _WEBKIT_WEBCONTENT, rss_bytes=409 * 1024 * 1024, start_time=1005.0),
        ProcessInfo(31, 1, _WEBKIT_GPU, rss_bytes=14 * 1024 * 1024, start_time=1005.5),
        ProcessInfo(32, 1, _WEBKIT_NETWORKING, rss_bytes=18 * 1024 * 1024, start_time=1006.0),
        ProcessInfo(40, 1, _WEBKIT_WEBCONTENT, rss_bytes=50 * 1024 * 1024, start_time=900.0),
    ]
    family = live_process_family(engine_pid=20, process_iter=lambda: rows)
    assert family.webkit_association["status"] == "attributed"
    assert family.pids == {10, 20, 21, 30, 31, 32}
    assert 40 not in family.pids
    by_pid = {m["pid"]: m for m in family.members}
    assert by_pid[10]["role"] == "desktop-shell"
    assert by_pid[20]["role"] == "python-engine"
    assert by_pid[21]["role"] == FFMPEG_ROLE
    assert by_pid[30]["role"] == "webkit-webcontent"
    assert by_pid[31]["role"] == "webkit-gpu"
    assert by_pid[32]["role"] == "webkit-networking"


@pytest.mark.requirement("PERF-UI-6785")
def test_live_walk_refuses_webkit_helpers_when_two_open_dj_shells_run() -> None:
    """[if] two Open DJ shells are in the fake table [then] WebKit is not attributed and body says why, [else stop]."""
    rows = [
        ProcessInfo(10, 1, _SHELL, rss_bytes=1_000_000, start_time=1000.0),
        ProcessInfo(50, 1, _SHELL, rss_bytes=1_000_000, start_time=1001.0),
        ProcessInfo(20, 10, "python3 -m apps.engine_core serve", rss_bytes=2_000_000, start_time=1002.0),
        ProcessInfo(
            21,
            20,
            "/bin/ffmpeg -nostdin -i /tmp/a.m4a -",
            rss_bytes=500_000,
            start_time=1010.0,
        ),
        ProcessInfo(30, 1, _WEBKIT_WEBCONTENT, rss_bytes=409 * 1024 * 1024, start_time=1005.0),
        ProcessInfo(31, 1, _WEBKIT_GPU, rss_bytes=14 * 1024 * 1024, start_time=1005.5),
        ProcessInfo(32, 1, _WEBKIT_NETWORKING, rss_bytes=18 * 1024 * 1024, start_time=1006.0),
        ProcessInfo(40, 1, _WEBKIT_WEBCONTENT, rss_bytes=50 * 1024 * 1024, start_time=900.0),
    ]
    family = live_process_family(engine_pid=20, process_iter=lambda: rows)
    assert family.webkit_association["status"] == "refused"
    assert "shell" in family.webkit_association["reason"].lower()
    assert not {30, 31, 32, 40, 50} & family.pids
    assert family.pids == {10, 20, 21}
    roles = {m["pid"]: m.get("role") for m in family.members}
    assert not any(str(r or "").startswith("webkit-") for r in roles.values())


@pytest.mark.requirement("PERF-UI-6785")
def test_live_walk_reports_none_found_when_webcontent_is_outside_the_association_window() -> None:
    """[if] WebContent starts more than 45 s after the shell [then] it is not in the family, [else stop]."""
    rows = [
        ProcessInfo(10, 1, _SHELL, rss_bytes=1_000_000, start_time=1000.0),
        ProcessInfo(20, 10, "python3 -m apps.engine_core serve", rss_bytes=2_000_000, start_time=1002.0),
        ProcessInfo(
            21,
            20,
            "/bin/ffmpeg -nostdin -i /tmp/a.m4a -",
            rss_bytes=500_000,
            start_time=1010.0,
        ),
        ProcessInfo(30, 1, _WEBKIT_WEBCONTENT, rss_bytes=10 * 1024 * 1024, start_time=1050.0),
    ]
    family = live_process_family(engine_pid=20, process_iter=lambda: rows)
    assert family.webkit_association["status"] == "none_found"
    assert 30 not in family.pids
    assert family.pids == {10, 20, 21}


@pytest.fixture(scope="module")
def machine() -> Iterator[dict[str, int]]:
    specs = [
        node(
            "shell",
            _SHELL,
            node("shell_sibling", "/usr/bin/log show --last 5m"),
            node(
                "engine",
                "opendj-engine --name opendj-engine --port 8585 [python3 apps.engine_core serve]",
                node("sidecar", "/payload/bin/odj-audio serve --clock wall"),
                node("worker", "opendj-worker --name opendj-worker [python3]"),
            ),
        ),
        node(
            "foreign_engine",
            "opendj-engine --name opendj-engine --port 8719 [python3 apps.engine_core serve]",
            detach=True,
        ),
        node(
            "vite",
            "opendj-frontend --name opendj-frontend --port 9439",
            node("esbuild", "/x/node_modules/@esbuild/linux-x64/bin/esbuild --service"),
            detach=True,
        ),
        node(
            "dev_shell",
            "-zsh",
            node(
                "uv",
                "uv run python -m apps.engine_core serve --data-dir /tmp/opendj-openapi-x1",
                node("dev_engine", "opendj-engine --name opendj-engine --port 8720 [python3]"),
            ),
            node(
                "dev_vite",
                "opendj-frontend --name opendj-frontend --port 9440",
                node("dev_esbuild", "esbuild --service"),
            ),
        ),
        node(
            "supervisor",
            "opendj-backend --name opendj-backend --port 8787 [python3 -m apps.webui.server]",
            node(
                "reload_worker",
                "opendj-backend --name opendj-backend --port 8787 [python3 -m apps.webui.server]",
                node("reload_sidecar", "/repo/target/release/odj-audio serve"),
            ),
        ),
    ]
    pids: dict[str, int] = {}
    try:
        for spec in specs:
            pids.update(spawn(spec))
        yield pids
    finally:
        kill(pids)


def _family_commands(pids: set[int]) -> str:
    """The command line of every pid in a family, so a failing family names its members."""
    lines = []
    for pid in sorted(pids):
        try:
            lines.append(f"{pid}: {' '.join(psutil.Process(pid).cmdline())[:200]}")
        except psutil.Error:
            lines.append(f"{pid}: gone")
    return "\n".join(lines)


def _family(engine_pid: int) -> tuple[list[dict[str, Any]], set[int]]:
    # The production entry point with its production process iterator.
    return live_process_family_state(engine_pid=engine_pid)


@needs_visible_titles
@needs_fork
@pytest.mark.requirement("PERF-UI-6785")
def test_live_family_is_this_engines_tree_not_every_opendj_name(machine: dict[str, int]) -> None:
    """[if] another engine and a vite dev server run elsewhere [then] only this tree is counted, [else stop].

    Both directions in one fixture: the foreign opendj-engine and the
    opendj-frontend vite with its esbuild child must be left out (the old
    name seeding counted them), and the desktop shell above the engine plus
    the shell's OTHER child must be counted (an engine-only walk misses them).
    """
    # Controls: the foreign processes are alive, carry opendj-* titles the old
    # seeding matched, and sit outside the test's tree.
    own_tree = {child.pid for child in psutil.Process().children(recursive=True)}
    for key, name in (("foreign_engine", "opendj-engine"), ("vite", "opendj-frontend")):
        command = " ".join(psutil.Process(machine[key]).cmdline())
        assert opendj_process_name(command) == name
        assert machine[key] not in own_tree
    assert machine["shell"] in own_tree

    members, pids = _family(machine["engine"])

    assert pids == {machine[k] for k in ("shell", "shell_sibling", "engine", "sidecar", "worker")}, _family_commands(
        pids
    )
    assert sorted(m["name"] for m in members) == [
        "opendj-desktop",
        "opendj-engine",
        "opendj-worker",
        "unnamed",
        "unnamed",
    ]
    assert all(m["rss_mb"] > 0 for m in members), "every member carries its real resident MB"


@pytest.mark.requirement("PERF-UI-6785")
def test_a_foreign_performance_probe_above_the_engine_is_not_its_launcher() -> None:
    """[if] a foreign opendj-performance-probe is the engine's ancestor [then] the root stays the engine, [else stop].

    The probe measures the installed app; it does not launch it. On a shared
    host it can sit above unrelated processes, so climbing through it would
    count every process it spawned. The app's own launchers (the desktop
    shell and the backend supervisor) still root the family above the engine.
    """

    def row(pid: int, ppid: int, command: str) -> ProcessInfo:
        return ProcessInfo(pid=pid, ppid=ppid, command=command, rss_bytes=None)

    probe = row(10, 1, "opendj-performance-probe --name opendj-performance-probe")
    engine = row(20, 10, "opendj-engine --name opendj-engine")
    foreign = {1: row(1, 0, "launchd"), 10: probe, 20: engine}
    assert _family_root_pid(foreign, 20) == 20

    backend = row(30, 1, "opendj-backend --name opendj-backend --port 8787 [python3 -m apps.webui.server]")
    supervised = {1: row(1, 0, "launchd"), 10: probe, 30: backend, 20: row(20, 30, "opendj-engine")}
    assert _family_root_pid(supervised, 20) == 30

    shell = row(40, 1, "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop")
    shelled = {1: row(1, 0, "launchd"), 40: shell, 20: row(20, 40, "opendj-engine")}
    assert _family_root_pid(shelled, 20) == 40


@needs_visible_titles
@pytest.mark.requirement("PERF-UI-6785")
def test_live_family_of_a_dev_engine_does_not_climb_into_its_launcher(machine: dict[str, int]) -> None:
    """[if] a dev engine runs under uv [then] the family is the engine's own subtree, [else stop].

    ``uv``'s command names an ``opendj-*`` data dir only as an argument, so
    it is not an Open DJ process and the root stays the engine; climbing
    through it would reach the shell that also runs vite.
    """
    assert psutil.Process(machine["dev_engine"]).ppid() == machine["uv"]

    _members, pids = _family(machine["dev_engine"])

    assert pids == {machine["dev_engine"]}


@needs_visible_titles
@pytest.mark.requirement("PERF-UI-6785")
def test_live_family_climbs_through_a_reload_supervisor(machine: dict[str, int]) -> None:
    """[if] the engine is a --reload worker [then] its opendj-backend supervisor is the root, [else stop]."""
    _members, pids = _family(machine["reload_worker"])

    assert pids == {machine[k] for k in ("supervisor", "reload_worker", "reload_sidecar")}, _family_commands(pids)


# Spawn line of a uvicorn --reload worker on Windows (multiprocessing spawn).
_WINDOWS_RELOAD_WORKER = [
    "-c",
    "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=1, pipe_handle=2)",
    "--multiprocessing-fork",
]


@pytest.mark.requirement("PERF-UI-6785")
def test_live_family_climbs_through_a_reload_supervisor_shown_by_its_launch_argv() -> None:
    """[if] the supervisor shows only ``python -m apps.webui.server --reload`` [then] it is still the root, [else stop].

    That is what a Windows process list shows, because setproctitle titles are
    invisible there. Real processes keep exactly that command line, so this
    runs on every platform: a supervisor whose worker owns a sidecar, and a
    second supervisor-owned child that must count too.
    """
    pids = spawn(
        argv_node(
            "supervisor",
            ["-m", "apps.webui.server", "--reload"],
            argv_node(
                "reload_worker",
                _WINDOWS_RELOAD_WORKER,
                argv_node("reload_sidecar", ["-c", "pass", "odj-audio stand-in"]),
            ),
            argv_node("supervisor_sibling", ["-c", "pass", "supervisor-owned helper"]),
        )
    )
    try:
        # Control: the supervisor carries no opendj-* title, only its launch argv.
        command = " ".join(psutil.Process(pids["supervisor"]).cmdline())
        assert opendj_process_name(command) == "unnamed"
        assert command.endswith("-m apps.webui.server --reload")

        _members, family = _family(pids["reload_worker"])
    finally:
        kill(pids)

    assert family == set(pids.values()), _family_commands(family)


# The Electron shell, as the engine sees it. Its executable is ``Open DJ``
# (packaged) or ``electron`` (unpackaged), so no name says it is Open DJ's;
# what does is the environment it launches the engine with:
# ``apps/desktop/electron/src/engine.ts`` ``spawnEngine`` sets
# OPENDJ_PARENT_PID to its own pid. These are real processes launched that
# way, with command lines no Open DJ pattern matches (``python -c pass ...``),
# so only the stamped pid can root the family at the shell. argv nodes, so
# this runs on every platform.
def _electron_like_tree(engine_env: dict[str, str]) -> dict[str, object]:
    return argv_node(
        "shell",
        ["-c", "pass", "Open DJ (Electron main) stand-in"],
        argv_node("renderer", ["-c", "pass", "Open DJ Helper (Renderer) --type=renderer"]),
        argv_node("gpu", ["-c", "pass", "Open DJ Helper (GPU) --type=gpu-process"]),
        argv_node(
            "engine",
            ["-c", "pass", "engine payload launcher stand-in"],
            argv_node("sidecar", ["-c", "pass", "odj-audio stand-in"]),
            env=engine_env,
        ),
    )


def _engine_shell_pid(engine: int) -> int | None:
    """What the engine reads from its OWN environment, read back from the real process."""
    return launching_shell_pid(psutil.Process(engine).environ())


@pytest.mark.requirement("PERF-UI-6785")
def test_live_family_is_rooted_at_the_shell_that_stamped_its_pid_into_the_engine() -> None:
    """[if] the Electron shell launched the engine with OPENDJ_PARENT_PID [then] the shell and its Chromium children count, [else stop]."""
    pids = spawn(_electron_like_tree({LAUNCHING_SHELL_PID_ENV: PARENT_PID}))
    try:
        # Controls: no command in the tree is an app launcher, so the name climb
        # alone stops at the engine; the stamp is the shell's pid.
        for key in ("shell", "engine"):
            command = " ".join(psutil.Process(pids[key]).cmdline())
            assert not machine_pressure._runs_as_app_launcher(command)
        _members, unstamped = live_process_family_state(engine_pid=pids["engine"])
        assert unstamped == {pids["engine"], pids["sidecar"]}
        shell_pid = _engine_shell_pid(pids["engine"])
        assert shell_pid == pids["shell"]

        members, family = live_process_family_state(engine_pid=pids["engine"], shell_pid=shell_pid)
    finally:
        kill(pids)

    assert family == set(pids.values()), _family_commands(family)
    assert len(members) == 5 and all(m["rss_mb"] > 0 for m in members)


@pytest.mark.requirement("PERF-UI-6785")
def test_a_stamped_pid_that_is_not_the_engines_ancestor_roots_nothing() -> None:
    """[if] OPENDJ_PARENT_PID names a live process outside the engine's ancestry [then] it is ignored, [else stop].

    A recycled or stale pid can name any process. Here it names a real,
    running stand-in for another app's shell, with children of its own; none
    of that tree may enter the family.
    """
    other = spawn(
        argv_node(
            "other_shell",
            ["-c", "pass", "another app's shell"],
            argv_node("other_child", ["-c", "pass", "its child"]),
        )
    )
    pids: dict[str, int] = {}
    try:
        pids = spawn(_electron_like_tree({LAUNCHING_SHELL_PID_ENV: str(other["other_shell"])}))
        shell_pid = _engine_shell_pid(pids["engine"])
        assert shell_pid == other["other_shell"] and psutil.pid_exists(shell_pid)

        _members, family = live_process_family_state(engine_pid=pids["engine"], shell_pid=shell_pid)
    finally:
        kill(pids)
        kill(other)

    assert family == {pids["engine"], pids["sidecar"]}


_STAND_IN = Path(__file__).with_name("electron_launch_stand_in.py")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _launch_shell_and_engine(*, stamp: bool) -> dict[str, Any]:
    """Run the real shell stand-in; it launches the engine, which walks and serves the route."""
    env = dict(os.environ)
    env.pop(LAUNCHING_SHELL_PID_ENV, None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(_REPO_ROOT), env.get("PYTHONPATH", "")) if p)
    argv = [sys.executable, str(_STAND_IN), "shell"] + ([] if stamp else ["--no-stamp"])
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.requirement("PERF-UI-6785")
def test_an_engine_launched_by_a_stamping_shell_walks_from_that_shell_by_default() -> None:
    """[if] a shell launches the engine with OPENDJ_PARENT_PID [then] its children count, [else stop].

    The engine's own default walk and route count the shell, its renderer
    and its GPU child, tag the renderer and GPU helpers, and mark only the renderer as the page's process.

    Nothing is passed to the walk: inside the real engine child it reads the
    variable the real parent put in its environment, as the route does.
    """
    report = _launch_shell_and_engine(stamp=True)

    assert report["inherited"] == str(report["shell"]), "the engine inherited the shell's pid"
    assert set(report["family"]) == {report[k] for k in ("shell", "renderer", "gpu", "engine")}
    assert report["roles"][str(report["renderer"])] == ELECTRON_RENDERER_ROLE
    assert report["roles"][str(report["gpu"])] == ELECTRON_GPU_ROLE, "the GPU helper is named, not the page"
    route = report["route"]
    assert route["available"] is True and len(route["members"]) == 4
    assert [m.get("role") for m in route["members"]].count(ELECTRON_RENDERER_ROLE) == 1


@pytest.mark.requirement("PERF-UI-6785")
def test_an_engine_launched_without_the_stamp_walks_only_its_own_tree() -> None:
    """[if] the same shell launches the engine WITHOUT OPENDJ_PARENT_PID [then] neither the shell nor its renderer counts, [else stop].

    The control for the test above: same real processes, only the
    environment differs, so the stamp is what roots the family at the shell
    and what makes the renderer the page's process.
    """
    report = _launch_shell_and_engine(stamp=False)

    assert report["inherited"] is None
    assert report["family"] == [report["engine"]]
    assert all(role is None for role in report["roles"].values())
    assert all(m.get("role") is None for m in report["route"]["members"])


@pytest.mark.requirement("PERF-UI-6785")
def test_a_renderer_argv_alone_does_not_make_a_process_the_page() -> None:
    """[if] a --type=renderer process is in a family NOT rooted by the shell stamp [then] it gets no page role, [else stop].

    Every Chromium-based browser starts renderers with that argument, so it
    is trusted only under the stamped shell. Here the family is rooted by name
    (a ``--reload`` supervisor's launch argv) and holds a real process with
    exactly that argument; it counts as a member, but not as the page.
    """
    pids = spawn(
        argv_node(
            "supervisor",
            ["-m", "apps.webui.server", "--reload"],
            argv_node("reload_worker", _WINDOWS_RELOAD_WORKER),
            argv_node("renderer_like", ["-c", "pass", "--type=renderer"]),
        )
    )
    try:
        members, family = live_process_family_state(engine_pid=pids["reload_worker"])
    finally:
        kill(pids)

    assert family == set(pids.values()), "the renderer-like child is in the family\n" + _family_commands(family)
    assert all(m.get("role") is None for m in members)


@pytest.mark.requirement("PERF-UI-6785")
@pytest.mark.parametrize(
    ("raw", "pid"),
    [("4242", 4242), (" 4242\n", 4242), ("", None), ("0", None), ("-3", None), ("abc", None)],
)
def test_launching_shell_pid_reads_the_variable_the_shells_set(raw: str, pid: int | None) -> None:
    """[if] the env value is not a positive pid [then] no shell is named, [else stop]."""
    assert launching_shell_pid({LAUNCHING_SHELL_PID_ENV: raw}) == pid
    assert launching_shell_pid({}) is None


def test_the_launch_identity_is_the_variable_parent_watch_reads() -> None:
    """The family walk and the parent watch must name the same shell variable."""
    assert LAUNCHING_SHELL_PID_ENV == PARENT_ENV


@pytest.mark.requirement("PERF-UI-6785")
@pytest.mark.parametrize(
    ("command", "runs_as"),
    [
        ("opendj-engine --name opendj-engine --port 8585 [python3 apps.engine_core serve]", False),
        (_SHELL, True),
        (f"{_SHELL} --some-flag", True),
        ('"C:\\Program Files\\Open DJ\\opendj-desktop.exe" --flag', True),
        ("/payload/bin/opendj-engine --data-dir /d", False),
        (f"/usr/bin/pgrep -f {_SHELL}", False),
        ("uv run python -m apps.engine_core serve --data-dir /tmp/opendj-openapi-x1", False),
        ("/bin/sh -c opendj-engine", False),
        ("python3 -m apps.engine_core serve", True),
        ("C:\\repo\\.venv\\Scripts\\python.exe -m apps.webui.server --reload", True),
        ('"C:\\Program Files\\Py\\python.exe" -X utf8 -m apps.webui.server', True),
        ("/repo/.venv/bin/python3.11 -u -m apps.engine_core serve --port 1", True),
        ("/usr/bin/python3 -c import apps.webui.server", False),
        ("python -u script.py -m apps.webui.server", False),
        ("python -m pytest -m apps.webui.server", False),
        ("python -m apps.webui.serverless", False),
        ("pytest tests -k apps.webui.server", False),
    ],
)
def test_family_root_climbs_only_through_app_launchers(command: str, runs_as: bool) -> None:
    """[if] a parent is a neighbor program or only MENTIONS opendj-* [then] the climb does not go through it, [else stop].

    A pure function over a command string: the climb asks it of each parent.
    """
    assert machine_pressure._runs_as_app_launcher(command) is runs_as


@pytest.mark.requirement("PERFMODE-05")
def test_live_walk_tags_chromium_helpers_under_stamped_shell() -> None:
    """[if] shell tree has Chromium helpers [then] gpu and utility roles tag, [else stop]."""

    def fake_iter() -> list[ProcessInfo]:
        return [
            ProcessInfo(pid=10, ppid=1, command=_SHELL, rss_bytes=None),
            ProcessInfo(
                pid=11,
                ppid=10,
                command='Open DJ Helper --type=renderer "standalone"',
                rss_bytes=1000,
            ),
            ProcessInfo(
                pid=12,
                ppid=10,
                command="Open DJ Helper --type=gpu-process",
                rss_bytes=1000,
            ),
            ProcessInfo(
                pid=13,
                ppid=10,
                command="Open DJ Helper --type=utility --utility-sub-type=network.mojom.NetworkService",
                rss_bytes=1000,
            ),
            ProcessInfo(
                pid=20,
                ppid=10,
                command="python3 -m apps.engine_core serve",
                rss_bytes=2000,
            ),
            ProcessInfo(
                pid=21,
                ppid=20,
                command="/bin/ffmpeg -nostdin -i /tmp/a.m4a -",
                rss_bytes=500,
            ),
        ]

    members, family = live_process_family_state(engine_pid=20, shell_pid=10, process_iter=fake_iter)
    assert family == {10, 11, 12, 13, 20, 21}
    by_pid = {m["pid"]: m for m in members}
    assert by_pid[11]["role"] == ELECTRON_RENDERER_ROLE
    assert by_pid[12]["role"] == ELECTRON_GPU_ROLE
    assert by_pid[13]["role"] == ELECTRON_UTILITY_ROLE
    assert by_pid[21]["name"] == "ffmpeg"
    assert "role" not in by_pid[21]


@pytest.mark.requirement("PERFMODE-05")
def test_opendj_process_name_matches_probe_parser() -> None:
    """[if] representative process command lines are parsed [then] webui and probe, [else stop]."""
    fixtures = [
        ("opendj-engine --name opendj-engine --port 8585 [/usr/bin/python3 ...]", "opendj-engine"),
        ("opendj-worker --name opendj-worker [...]", "opendj-worker"),
        ("opendj-backend --name opendj-backend --port 8787 [...]", "opendj-backend"),
        ("/Applications/Open DJ.app/Contents/MacOS/opendj-desktop", "opendj-desktop"),
        (
            "/Applications/Open DJ.app/Contents/Frameworks/Open DJ Helper (Renderer).app/"
            "Contents/MacOS/Open DJ Helper (Renderer) --type=renderer",
            ELECTRON_RENDERER_ROLE,
        ),
        (
            "/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/"
            "Contents/MacOS/Claude Helper (Renderer) --type=renderer",
            "unnamed",
        ),
        (
            "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
            "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent",
            "unnamed",
        ),
        (
            "/opt/homebrew/bin/ffmpeg -nostdin -i /Music/opendj-mix.mp3 -f f32le -",
            "ffmpeg",
        ),
        (
            "/usr/sbin/taskpolicy -b /repo/ffmpeg -nostdin -i x -",
            "ffmpeg",
        ),
        (
            "/Applications/Open DJ.app/Contents/Resources/payload/runtime/bin/python3 "
            "-m apps.analysis.queue_cli --db /d/state.db",
            "analysis-lane",
        ),
        ("python3 -m apps.engine_core serve", "unnamed"),
    ]
    for command, expected in fixtures:
        assert opendj_process_name(command) == expected
        assert probe_opendj_process_name(command) == expected


@pytest.mark.requirement("PERFMODE-05")
def test_sample_wall_p95_uses_nearest_rank() -> None:
    """[if] twenty sample walls are recorded [then] p95 is the 19th ordered value, [else stop]."""
    walls = [float(i) for i in range(1, 21)]

    p95 = sample_wall_p95_ms(walls)

    assert PRESSURE_SAMPLE_P95_WALL_MS == 5
    assert p95 == pytest.approx(19.0)


@pytest.mark.requirement("PERFMODE-05")
def test_empty_wall_ring_omits_p95_rather_than_zero() -> None:
    """[if] no sample walls have been recorded [then] p95 is absent, never 0, [else stop]."""
    assert sample_wall_p95_ms([]) is None


@pytest.mark.requirement("PERFMODE-05")
def test_pressure_response_exposes_p95_without_failing_over_budget() -> None:
    """[if] samples are taken [then] sample_wall_p95_ms is present, [else stop]."""
    cache = MachinePressureCache()
    sampler, _ = _counting_sampler(FULL_SAMPLE)

    body = cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)

    assert "sample_wall_ms" in body
    assert "sample_wall_p95_ms" in body
    assert body["sample_wall_p95_ms"] != 0 or body["sample_wall_ms"] == 0


# PERF-UI-6785 (Codex on #6785): a shell that ADOPTS a running engine is neither
# its ancestor nor stamped in its environment. It registers in the engine's
# data dir (``.engine.parent``, written by both shells on spawn, adopt and
# restart), and the walk adds that shell's tree once the registration is
# validated against the live process.


def _adopting_shell_tree() -> dict[str, object]:
    """A shell with its Chromium children, launched NOT as the engine's parent."""
    return argv_node(
        "shell",
        ["-c", "pass", "Open DJ (Electron main) stand-in"],
        argv_node("renderer", ["-c", "pass", "Open DJ Helper (Renderer) --type=renderer"]),
        argv_node("gpu", ["-c", "pass", "Open DJ Helper (GPU) --type=gpu-process"]),
    )


def _engine_tree() -> dict[str, object]:
    return argv_node(
        "engine",
        ["-c", "pass", "engine payload launcher stand-in"],
        argv_node("sidecar", ["-c", "pass", "odj-audio stand-in"]),
    )


def _register(data_dir: Path, pid: int) -> Path:
    """Write the registration the way both shells do: the pid, a newline, 0600."""
    path = data_dir / SHELL_REGISTRATION_FILE_NAME
    path.write_text(f"{pid}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_the_registration_file_is_the_one_parent_watch_trusts() -> None:
    assert SHELL_REGISTRATION_FILE_NAME == PARENT_FILE_NAME


@pytest.mark.requirement("PERF-UI-6785")
def test_a_registered_adopting_shell_and_its_children_join_the_family(tmp_path: Path) -> None:
    """[if] the shell adopted the engine and registered [then] its tree counts and its renderer is the page's, [else stop].

    Controls: without the registration the adopting shell is NOT in the
    family (the under-count Codex found), and an unrelated, unregistered
    process with a renderer child of its own never enters it.
    """
    shell = spawn(_adopting_shell_tree())
    engine = spawn(_engine_tree())
    unrelated = spawn(
        argv_node(
            "browser",
            ["-c", "pass", "some other Chromium browser"],
            argv_node("browser_renderer", ["-c", "pass", "Browser Helper --type=renderer"]),
        )
    )
    try:
        # The shell really is outside the engine's ancestry.
        assert shell["shell"] not in {p.pid for p in psutil.Process(engine["engine"]).parents()}
        assert read_shell_registration(tmp_path).status == "absent"
        _members, unregistered = live_process_family_state(engine_pid=engine["engine"])
        assert unregistered == {engine["engine"], engine["sidecar"]}

        _register(tmp_path, shell["shell"])
        registration = read_shell_registration(tmp_path)
        assert registration.status == "accepted", registration
        assert registration.pid == shell["shell"]
        members, family = live_process_family_state(engine_pid=engine["engine"], registered_shell_pid=registration.pid)
    finally:
        kill(unrelated)
        kill(engine)
        kill(shell)

    assert family == set(shell.values()) | set(engine.values())
    assert not family & set(unrelated.values())
    roles = {m["pid"]: m.get("role") for m in members}
    assert roles[shell["renderer"]] == ELECTRON_RENDERER_ROLE
    assert [pid for pid, role in roles.items() if role == ELECTRON_RENDERER_ROLE] == [shell["renderer"]]


@pytest.mark.requirement("PERF-UI-6785")
def test_a_registration_naming_a_dead_pid_is_rejected_and_counts_nothing(tmp_path: Path) -> None:
    """[if] the registered shell has exited [then] the registration is rejected, not guessed, [else stop]."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=20) == 0
    _register(tmp_path, child.pid)
    engine = spawn(_engine_tree())
    try:
        registration = read_shell_registration(tmp_path)
        _members, family = live_process_family_state(engine_pid=engine["engine"], registered_shell_pid=registration.pid)
    finally:
        kill(engine)
    assert registration.status == "rejected"
    assert registration.pid is None
    assert registration.reason == "the registered shell is not running"
    assert family == set(engine.values())


@pytest.mark.requirement("PERF-UI-6785")
def test_a_registration_older_than_the_process_holding_its_pid_is_a_reused_pid(
    tmp_path: Path,
) -> None:
    """[if] the pid's process started after the registration was written [then] it is rejected, [else stop].

    A shell that dies leaves its file behind, and the pid can be given to a
    new process. That is this state on disk: the file's write time precedes
    the process now holding the pid. Control: the same process with a file
    written after it started is accepted.
    """
    shell = spawn(_adopting_shell_tree())
    try:
        path = _register(tmp_path, shell["shell"])
        accepted = read_shell_registration(tmp_path)
        created = psutil.Process(shell["shell"]).create_time()
        os.utime(path, (created - 5, created - 5))
        reused = read_shell_registration(tmp_path)
    finally:
        kill(shell)
    assert accepted.status == "accepted"
    assert reused.status == "rejected" and reused.pid is None
    assert "reused pid" in (reused.reason or "")


@pytest.mark.requirement("PERF-UI-6785")
def test_a_registration_dated_in_the_future_is_rejected_even_for_a_later_process(
    tmp_path: Path,
) -> None:
    """[if] the file's write time is ahead of now [then] it is rejected, [else stop].

    A clock that moved back leaves a registration dated in the future. A later
    process reusing that pid started before that date, so the create-time check
    alone would accept it. Control: the same live shell with a write time that
    is not ahead of now is accepted.
    """
    shell = spawn(_adopting_shell_tree())
    try:
        path = _register(tmp_path, shell["shell"])
        accepted = read_shell_registration(tmp_path)
        future = time.time() + 3600
        os.utime(path, (future, future))
        future_dated = read_shell_registration(tmp_path)
    finally:
        kill(shell)
    assert accepted.status == "accepted"
    assert future_dated.status == "rejected" and future_dated.pid is None
    assert "dated in the future" in (future_dated.reason or "")


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="file owner and mode bits are POSIX-only")
@pytest.mark.requirement("PERF-UI-6785")
def test_a_registration_file_others_can_write_is_rejected(tmp_path: Path) -> None:
    """[if] another user could have written the registration [then] it is rejected, [else stop]."""
    shell = spawn(_adopting_shell_tree())
    try:
        path = _register(tmp_path, shell["shell"])
        path.chmod(0o666)
        registration = read_shell_registration(tmp_path)
    finally:
        kill(shell)
    assert registration.status == "rejected"
    assert registration.reason == "the registration file is writable by other users"


@pytest.mark.requirement("PERF-UI-6785")
def test_a_registration_that_holds_no_pid_is_rejected(tmp_path: Path) -> None:
    """[if] the registration file holds no pid [then] it is rejected and names no pid, [else stop]."""
    (tmp_path / SHELL_REGISTRATION_FILE_NAME).write_text("not a pid\n", encoding="utf-8")
    (tmp_path / SHELL_REGISTRATION_FILE_NAME).chmod(0o600)
    registration = read_shell_registration(tmp_path)
    assert registration.status == "rejected"
    assert registration.reason == "the registration file does not hold a pid"
