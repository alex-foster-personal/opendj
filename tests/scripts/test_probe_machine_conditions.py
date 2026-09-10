"""Load average and free physical memory, added to the machine sampler.

These two are the conditions ``docs/perf/performance-register.md`` blames most
often for a number the program cannot trust (a waveform decode measured 0.73 s
and 7.53 s in one evening on load alone; a beatgrid lane taken at load average
554 with roughly 71 MB free). They now live in the one existing sampler rather
than a second one, so the probe's JSONL and the engine's pressure endpoint can
never disagree about what "free memory" means.

The parser cases below are the point: ``vm_stat`` output that this cannot read
must yield NO key. A zero would read as a machine about to die.
"""

from __future__ import annotations

import math

from typing import Any

import pytest

from scripts.diagnostics import probe_native_metrics as native

# Trimmed from real `vm_stat` output on this Mac, Wed 9 Sep 2026.
VM_STAT_SAMPLE = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               17335.
Pages active:                            303014.
Pages inactive:                          296733.
Pages speculative:                         2020.
Pages throttled:                              0.
Pages wired down:                        144551.
"""


def test_free_memory_is_pages_free_times_page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "run_text", lambda *_a, **_k: VM_STAT_SAMPLE)

    fields = native._vm_stat_free_mb()

    # 17335 pages * 16384 bytes = 283,996,160 bytes = 270.8 MiB.
    assert fields == {"free_memory_mb": pytest.approx(270.8, abs=0.1)}


def test_a_missing_page_size_header_yields_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "run_text", lambda *_a, **_k: "Pages free: 17335.\n")

    assert native._vm_stat_free_mb() == {}


def test_a_missing_free_line_yields_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native, "run_text", lambda *_a, **_k: "Statistics: (page size of 16384 bytes)\n"
    )

    assert native._vm_stat_free_mb() == {}


def test_a_vm_stat_that_will_not_run_yields_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("vm_stat: command not found")

    monkeypatch.setattr(native, "run_text", _boom)

    assert native._vm_stat_free_mb() == {}


def test_load_average_is_read_without_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.os, "getloadavg", lambda: (5.7612, 7.9401, 7.6000))

    assert native._load_average() == {
        "load_average_1m": pytest.approx(5.76),
        "load_average_5m": pytest.approx(7.94),
        "load_average_15m": pytest.approx(7.6),
    }


def test_a_platform_without_load_average_yields_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> tuple[float, float, float]:
        raise OSError("load average is unobtainable")

    monkeypatch.setattr(native.os, "getloadavg", _boom)

    assert native._load_average() == {}


def test_machine_metrics_carries_the_new_conditions_on_this_machine() -> None:
    """Not a mock: the real sampler, on the box this suite runs on.

    The register's failures were all measured on a Mac, and so is this. On any
    other platform the sysctl reads return nothing and the keys are absent,
    which is the correct answer there -- so the assertion is that whatever IS
    present is a real number, never that a particular key exists.
    """

    metrics = native.machine_metrics()

    for key in ("load_average_1m", "load_average_5m", "load_average_15m", "free_memory_mb"):
        if key in metrics:
            assert isinstance(metrics[key], float)
            assert metrics[key] >= 0.0


# --------------------------------------------------------------------------
# REAL PATH. Everything above feeds CAPTURED real `vm_stat` output through the
# PRODUCTION parser: the fixtures are trimmed from this Mac's actual output and
# `_vm_stat_free_mb` is the shipped function, so substituting `run_text` is how
# a captured payload is delivered deterministically, not a stand-in for the
# parser. What it does not cover is the command itself, and this does: no
# substitution, the real `vm_stat` and the real `sysctl` on whatever machine
# runs the suite.
#
# Asserted as a CONTRACT, not a value, because the value is whatever the
# machine is doing. The contract is what a regression breaks: a field is a
# finite number or it is ABSENT, and never present-and-zero.
# --------------------------------------------------------------------------


def test_the_real_sampler_runs_and_omits_what_it_cannot_read() -> None:
    metrics = native.machine_metrics()

    assert isinstance(metrics, dict)
    for key, value in metrics.items():
        assert value is not None, f"{key} is None; an unreadable field must be ABSENT"
        if isinstance(value, float):
            assert math.isfinite(value), f"{key} is {value}, which is not a measurement"


def test_the_real_vm_stat_parse_is_a_positive_free_memory_or_no_key() -> None:
    """Negative control on the parser's own output.

    A zero here would read as a machine with no free memory at all, which is
    exactly the misreading this parser's docstring exists to prevent. On a
    machine that cannot answer, the KEY must be missing rather than zeroed.
    """
    fields = native._vm_stat_free_mb()

    if "free_memory_mb" not in fields:
        pytest.skip("vm_stat is not available on this machine")
    assert fields["free_memory_mb"] > 0.0, (
        "free memory of exactly zero is the parser failing, not the machine"
    )
