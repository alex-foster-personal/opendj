"""Tests for :mod:`scripts.local_run_headroom`.

Acceptance lines:
  - [if] load and free memory are inside the limits [then] verdict is OK and ok is True, [else stop].
  - [if] load per core is above the limit [then] REFUSED, and the reason names the reading and the limit, [else stop].
  - [if] free memory is below the minimum [then] REFUSED, and the reason names the reading and the minimum, [else stop].
  - [if] either reading is None [then] UNKNOWN and ok is False, never OK, [else stop].
  - [if] a reading sits exactly AT a limit [then] OK, not refused (control for the comparison direction), [else stop].
  - [if] the meminfo text has MemAvailable and MemTotal [then] the free percent is their ratio, [else stop].
  - [if] meminfo lacks MemAvailable [then] HeadroomUnknown, never a number, [else stop].
  - [if] run mode is REFUSED [then] the command is never launched and the exit code is 1, [else stop].
  - [if] run mode is OK [then] the command is launched without a shell and its exit code is returned, [else stop].
  - [if] a measurement raises HeadroomUnknown [then] check exits 2, [else stop].
  - [if] run has no command after -- [then] argparse errors out, [else stop].
  - [if] run names a command that is not on PATH [then] it exits 127 before measuring anything, [else stop].
  - [if] a threshold flag is nan, inf or out of range [then] argparse rejects it before measuring, [else stop].
  - [if] a threshold or reading reaching verdict() is nan [then] REFUSED, never OK, [else stop].
  - [if] this host's real load or memory reader fails or returns nonsense [then] the platform test fails, [else stop].
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

import pytest

from scripts import local_run_headroom as lrh

pytestmark = pytest.mark.requirement("DEVOPS-25")

MAX_LOAD = 0.70
MIN_FREE = 20.0

MEMINFO_SAMPLE = """\
MemTotal:       16000000 kB
MemFree:          500000 kB
MemAvailable:    4000000 kB
Buffers:          100000 kB
"""


def _verdict(load: float | None, free: float | None) -> lrh.Verdict:
    return lrh.verdict(load, free, MAX_LOAD, MIN_FREE)


def _patch_readings(monkeypatch: pytest.MonkeyPatch, load: float, free: float) -> None:
    monkeypatch.setattr(lrh, "_load_per_core", lambda: load)
    monkeypatch.setattr(lrh, "_free_memory_pct", lambda: free)


def _raise_unknown() -> float:
    raise lrh.HeadroomUnknown("simulated unmeasurable")


def test_verdict_ok_inside_limits() -> None:
    result = _verdict(0.30, 50.0)
    assert result.ok is True, "if both readings are inside the limits then verdict must be OK"
    assert result.status == "OK"
    assert result.reasons == []


def test_verdict_refused_on_high_load_names_both_numbers() -> None:
    result = _verdict(1.25, 50.0)
    assert result.ok is False, "if load is above the limit then it must be refused"
    assert result.status == "REFUSED"
    joined = " ".join(result.reasons)
    assert "1.25" in joined and "0.70" in joined, "if the reason omits the reading or the limit then broken"


def test_verdict_refused_on_low_memory_names_both_numbers() -> None:
    result = _verdict(0.10, 8.5)
    assert result.ok is False, "if free memory is below the minimum then it must be refused"
    assert result.status == "REFUSED"
    joined = " ".join(result.reasons)
    assert "8.5" in joined and "20.0" in joined, "if the reason omits the reading or the minimum then broken"


@pytest.mark.parametrize("load, free", [(None, 50.0), (0.1, None), (None, None)])
def test_verdict_unknown_when_a_reading_is_missing(load: float | None, free: float | None) -> None:
    result = _verdict(load, free)
    assert result.status == "UNKNOWN", "if a reading is None then verdict must be UNKNOWN"
    assert result.ok is False


def test_verdict_unknown_beats_refused_but_keeps_refusal_reason() -> None:
    result = _verdict(None, 5.0)
    assert result.status == "UNKNOWN"
    assert any("5.0" in reason for reason in result.reasons), "if a known refusal is dropped then broken"


def test_values_exactly_at_the_limits_are_ok() -> None:
    result = _verdict(MAX_LOAD, MIN_FREE)
    assert result.status == "OK", "if exactly-at-limit readings are refused then the comparison is too strict"


def test_parse_meminfo_free_pct_on_literal_sample() -> None:
    assert lrh.parse_meminfo_free_pct(MEMINFO_SAMPLE) == pytest.approx(25.0)


def test_parse_meminfo_missing_field_raises_unknown() -> None:
    with pytest.raises(lrh.HeadroomUnknown, match="MemAvailable"):
        lrh.parse_meminfo_free_pct("MemTotal:       16000000 kB\n")


def test_run_refused_does_not_launch_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_readings(monkeypatch, load=2.0, free=50.0)
    calls: list[object] = []
    monkeypatch.setattr(lrh.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    code = lrh.main(["run", "--", sys.executable, "-c", "pass"])
    assert code == 1, "if headroom is missing then exit code must be 1"
    assert calls == [], "if the command launched while refused then the gate is broken"
    assert "[headroom]" in capsys.readouterr().out


def test_run_ok_launches_command_and_returns_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_readings(monkeypatch, load=0.1, free=60.0)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args[0], returncode=7)  # type: ignore[arg-type]

    monkeypatch.setattr(lrh.subprocess, "run", fake_run)
    code = lrh.main(["run", "--", sys.executable, "-c", "pass"])
    assert code == 7, "if the command's exit code is not returned then broken"
    assert len(calls) == 1
    # sys.executable, not `echo`: a shell builtin on Windows is no executable (Codex, PR #7598).
    assert calls[0][0][0] == [shutil.which(sys.executable), "-c", "pass"], (
        "if the command is not resolved, or is altered or shell-joined, then broken"
    )
    assert not calls[0][1].get("shell"), "if a shell is used then broken"


def test_check_exits_2_when_a_measurement_is_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(lrh, "_load_per_core", _raise_unknown)
    monkeypatch.setattr(lrh, "_free_memory_pct", lambda: 60.0)
    code = lrh.main(["check"])
    assert code == 2, "if an unmeasurable reading does not exit 2 then it is a silent fallback"
    assert "UNKNOWN" in capsys.readouterr().out


def test_check_ok_exits_0_and_prints_both_readings_and_limits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_readings(monkeypatch, load=0.25, free=55.0)
    assert lrh.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "0.25" in out and "0.70" in out and "55.0" in out and "20.0" in out


def test_cli_flags_override_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_readings(monkeypatch, load=0.9, free=50.0)
    assert lrh.main(["check"]) == 1
    assert lrh.main(["check", "--max-load-per-core", "1.0"]) == 0


def test_run_without_a_command_is_an_argparse_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        lrh.main(["run"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        lrh.main(["run", "--"])


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--max-load-per-core", "nan"),
        ("--max-load-per-core", "inf"),
        ("--max-load-per-core", "0"),
        ("--max-load-per-core", "-1"),
        ("--min-free-memory-pct", "nan"),
        ("--min-free-memory-pct", "-inf"),
        ("--min-free-memory-pct", "101"),
        ("--min-free-memory-pct", "-0.5"),
    ],
)
def test_a_non_finite_or_out_of_range_threshold_is_rejected_before_measuring(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str
) -> None:
    def must_not_measure() -> float:
        raise AssertionError("measured with an invalid threshold")

    monkeypatch.setattr(lrh, "_load_per_core", must_not_measure)
    monkeypatch.setattr(lrh, "_free_memory_pct", must_not_measure)
    with pytest.raises(SystemExit) as excinfo:
        lrh.main(["check", flag, value])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "flag, value", [("--max-load-per-core", "0.01"), ("--min-free-memory-pct", "0"), ("--min-free-memory-pct", "100")]
)
def test_finite_thresholds_at_the_range_edges_still_parse(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str
) -> None:
    _patch_readings(monkeypatch, load=0.0, free=100.0)
    assert lrh.main(["check", flag, value]) == 0


@pytest.mark.parametrize(
    "load, free, max_load, min_free",
    [
        (float("nan"), 50.0, 0.70, 20.0),
        (0.10, float("nan"), 0.70, 20.0),
        (0.10, 50.0, float("nan"), 20.0),
        (0.10, 50.0, 0.70, float("nan")),
    ],
)
def test_a_nan_reaching_the_verdict_refuses(load: float, free: float, max_load: float, min_free: float) -> None:
    result = lrh.verdict(load, free, max_load, min_free)
    assert result.status == "REFUSED" and not result.ok


def test_run_with_a_command_not_on_path_exits_127_before_measuring(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_measure() -> float:
        raise AssertionError("measured before checking the command exists")

    monkeypatch.setattr(lrh, "_load_per_core", must_not_measure)
    monkeypatch.setattr(lrh, "_free_memory_pct", must_not_measure)
    assert lrh.main(["run", "--", "no-such-command-headroom-test"]) == lrh.EXIT_COMMAND_NOT_FOUND


def test_a_load_average_os_error_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> tuple[float, float, float]:
        raise OSError("load average unobtainable")

    monkeypatch.setattr(lrh.os, "getloadavg", broken, raising=False)
    with pytest.raises(lrh.HeadroomUnknown):
        lrh._load_per_core()


# ----- the real OS boundary (Codex P1 on PR #7598): no reader is replaced here -----


@pytest.mark.skipif(not hasattr(os, "getloadavg"), reason="load average is POSIX-only; Windows reports UNKNOWN")
def test_the_real_load_reader_returns_a_plausible_load() -> None:
    load = lrh._load_per_core()
    assert 0.0 <= load < 100.0


@pytest.mark.skipif(platform.system() != "Darwin", reason="kern.memorystatus_level is macOS-only")
def test_the_real_macos_memory_reader_returns_a_percentage() -> None:
    assert 0.0 <= lrh._free_memory_pct() <= 100.0


@pytest.mark.skipif(platform.system() != "Linux", reason="/proc/meminfo is Linux-only")
def test_the_real_linux_meminfo_reader_returns_a_percentage() -> None:
    assert 0.0 <= lrh._free_memory_pct() <= 100.0


@pytest.mark.skipif(platform.system() in ("Darwin", "Linux"), reason="only other platforms lack a memory reader")
def test_an_unsupported_platform_reports_unknown_memory() -> None:
    with pytest.raises(lrh.HeadroomUnknown):
        lrh._free_memory_pct()
