"""The machine-pressure reading: cached, aged honestly, never zero-filled.

The whole reason this endpoint exists is that a timing number without its
conditions is an anecdote, so the cases that matter most here are the ones
where nothing could be measured. A reading that renders as `0.0` when the
sampler is missing would be worse than no endpoint at all: it would let a
deck-load row claim an idle machine on a box nobody sampled, which is the
exact defect .claude/rules/verification.md forbids.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from apps.shared import machine_pressure
from apps.shared.machine_pressure import (
    CACHE_TTL_SECONDS,
    PRESSURE_CHURN_WEIGHT_SWAP,
    PRESSURE_SAMPLE_P95_WALL_MS,
    MachinePressureCache,
    ProcessInfo,
    live_process_family_members,
    opendj_process_name,
    read_machine_pressure,
    sample_wall_p95_ms,
)
from scripts.diagnostics.probe_process_family import (
    opendj_process_name as probe_opendj_process_name,
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
    sampler, _ = _counting_sampler(
        {"load_average_1m": 1.0, "kernel_memory_pressure_level": 0}
    )

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


@pytest.mark.requirement("PERFMODE-05")
def test_elevated_kernel_lengthens_the_ttl_to_five_seconds() -> None:
    """[if] kernel_memory_pressure_level is elevated [then] TTL is 5s, [else stop]."""
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler(
        {"load_average_1m": 1.0, "kernel_memory_pressure_level": 2}
    )

    cache.read(now=0.0, sampler=sampler, vm_stat_reader=lambda: None)
    cache.read(now=1.1, sampler=sampler, vm_stat_reader=lambda: None)
    cache.read(now=5.1, sampler=sampler, vm_stat_reader=lambda: None)

    assert len(calls) == 2


@pytest.mark.requirement("PERFMODE-05")
def test_elevated_churn_lengthens_even_when_kernel_is_fine() -> None:
    """[if] churn_score is elevated while kernel pressure is [then] TTL is still, [else stop]."""
    cache = MachinePressureCache()
    sampler, calls = _counting_sampler(
        {"load_average_1m": 1.0, "kernel_memory_pressure_level": 1}
    )
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
    sampler, calls = _counting_sampler(
        {"load_average_1m": 1.0, "kernel_memory_pressure_level": 1}
    )
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


@pytest.mark.requirement("PERFMODE-05")
def test_opendj_process_name_matches_probe_parser() -> None:
    """[if] representative process command lines are parsed [then] webui and probe, [else stop]."""
    fixtures = [
        ("opendj-engine --name opendj-engine --port 8585 [/usr/bin/python3 ...]", "opendj-engine"),
        ("opendj-worker --name opendj-worker [...]", "opendj-worker"),
        ("opendj-backend --name opendj-backend --port 8787 [...]", "opendj-backend"),
        ("/Applications/Open DJ.app/Contents/MacOS/opendj-desktop", "opendj-desktop"),
        (
            "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
            "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent",
            "unnamed",
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
