"""Real-process coverage for the analysis pool (META-07, META-08).

Every process-level test here drives REAL spawned workers. The pool, the
executor, the child processes, the signals and the exit codes are all genuine;
the only thing the tests supply is a backend that decides how the worker dies.

Those backends live in :mod:`tests.analysis.pool_probe_backends` rather than in
this file, because the pool hands workers the backend CLASS and a spawned child
imports that class's module when it unpickles the work item. Keeping them in
their own module is what lets the probe record what the worker interpreter
looked like at exactly that moment.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from apps.analysis import pool as pool_mod
from apps.analysis import run as run_mod
from apps.analysis.worker_diagnostics import (
    CLEANUP_SIGNAL,
    THREAD_PIN_VARS,
    _signal_label,
    pool_death_message,
    worker_exit_signals,
)

from .pool_probe_backends import SelfKillingBackend

REPO_ROOT = Path(__file__).resolve().parents[2]

_ALL_SIGNALS_NAMED = "this platform names every signal number 1-127"

#: Forces the multiprocessing DEFAULT start method to ``fork`` before calling
#: the real ``run()``. Without an explicit spawn context the pool would then
#: fork, and the probe's ``import_pid`` would betray it. This is what makes the
#: spawn assertion bite on macOS too, where the platform default is already
#: spawn and dropping ``mp_context`` would otherwise change nothing.
FORK_DEFAULT_DRIVER = '''\
import multiprocessing
import os
import sys
from pathlib import Path

from apps.analysis.backends import register
from apps.analysis.run import build_queue, run

if __name__ == "__main__":
    # Imported HERE, not at module level: a spawned child re-imports this file
    # as __mp_main__ before the pool initializer runs, and a top-level import
    # would capture the probe's import-time state too early to mean anything.
    from tests.analysis.pool_probe_backends import ProbeBackend

    multiprocessing.set_start_method("fork", force=True)
    assert multiprocessing.get_start_method() == "fork"
    register("probe", ProbeBackend)
    summary = run(
        build_queue([Path(p) for p in sys.argv[1:]]),
        backend_name="probe",
        dry_run=True,
        workers=2,
    )
    assert summary.failed == 0, summary.errors
    print("driver-pid", os.getpid(), "analysed", summary.analysed)
'''

#: Registers the crashing backend, then hands straight over to the real CLI.
CRASH_CLI_DRIVER = '''\
import sys

from apps.analysis.backends import register
from apps.analysis.run import main

if __name__ == "__main__":
    # Under the guard for the same reason as the driver above: the worker gets
    # the class by reference and imports it when it unpickles the work item.
    from tests.analysis.pool_probe_backends import SelfKillingBackend

    register("selfkill", SelfKillingBackend)
    sys.exit(main())
'''


@pytest.fixture()
def selfkill(monkeypatch: pytest.MonkeyPatch) -> type[SelfKillingBackend]:
    from apps.analysis import backends

    monkeypatch.setitem(backends.BACKENDS, "selfkill", SelfKillingBackend)
    return SelfKillingBackend


def _audio(tmp_path: Path, *names: str) -> list[Path]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"not really audio; no backend here decodes it")
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# META-07: spawned workers, pinned before the backend import chain
# ---------------------------------------------------------------------------


@pytest.mark.requirement("META-07")
def test_pool_workers_are_spawned_and_thread_pinned(tmp_path: Path) -> None:
    """[if] the pool forks a worker or leaves its BLAS pins unset [then] fail, [else stop]."""
    probe_dir = tmp_path / "probes"
    probe_dir.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(FORK_DEFAULT_DRIVER)
    files = _audio(tmp_path, "a.wav", "b.wav", "c.wav", "d.wav")

    env = {**os.environ, "MDT_PROBE_DIR": str(probe_dir), "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, str(driver), *[str(p) for p in files]],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=300, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    reports = [json.loads(p.read_text()) for p in probe_dir.glob("*.json")]
    assert len(reports) == len(files), proc.stderr
    driver_pid = int(proc.stdout.split("driver-pid")[1].split()[0])

    for report in reports:
        # A fresh interpreter imported the backend itself; a fork would have
        # carried the driver's pid in here instead.
        assert report["import_pid"] == report["pid"], report
        assert report["pid"] != driver_pid, report
        # The pins were already in place when the backend module loaded, which
        # is what puts them ahead of the numpy/numba import chain.
        assert report["pins_at_import"] == {n: "1" for n in THREAD_PIN_VARS}, report
        assert report["numpy_at_import"] is False, report


@pytest.mark.requirement("META-07")
def test_the_probe_reports_no_pins_when_no_initializer_ran() -> None:
    """[if] the probe reports pins with no initializer [then] the pins prove nothing, [else stop]."""
    probe = textwrap.dedent(
        """
        import json
        from tests.analysis import pool_probe_backends as p
        print(json.dumps({"pins": p.PINS_AT_IMPORT, "numpy": p.NUMPY_AT_IMPORT}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120, check=False,
        env={k: v for k, v in os.environ.items() if k not in THREAD_PIN_VARS},
    )

    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert seen["pins"] == {n: None for n in THREAD_PIN_VARS}, seen
    assert seen["numpy"] is False, seen


@pytest.mark.requirement("META-07")
def test_the_worker_initializer_is_importable_without_numpy() -> None:
    """[if] the initializer module imports numpy [then] its pins land too late, [else stop]."""
    probe = textwrap.dedent(
        """
        import sys
        import apps.analysis.worker_diagnostics as wd
        assert "numpy" not in sys.modules, sorted(sys.modules)[:5]
        wd.init_worker()
        import os
        assert all(os.environ[name] == "1" for name in wd.THREAD_PIN_VARS)
        import faulthandler
        assert faulthandler.is_enabled()
        print("ok")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# ---------------------------------------------------------------------------
# META-08: the parent-facing diagnostic, through real signalled worker deaths
# ---------------------------------------------------------------------------


def _run_cli(files: list[Path], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Drive the real CLI with the crashing backend registered in every process."""
    driver = files[0].parent / "cli_driver.py"
    driver.write_text(CRASH_CLI_DRIVER)
    return subprocess.run(
        [
            sys.executable, str(driver),
            "--backend", "selfkill", "--workers", "2", "--dry-run",
            "--files", *[str(p) for p in files],
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
        env={**env, "PYTHONPATH": str(REPO_ROOT)}, timeout=300, check=False,
    )


@pytest.mark.requirement("META-08")
def test_a_segfaulting_worker_is_named_with_its_signal(tmp_path: Path) -> None:
    """[if] a real pool child dies by SIGSEGV [then] the CLI must name it and exit 5, [else stop]."""
    files = _audio(tmp_path, "one.wav", "two.wav")
    env = {**os.environ, "MDT_SELFKILL_SIGNAL": str(int(signal.SIGSEGV))}

    proc = _run_cli(files, env)

    assert proc.returncode == run_mod.EXIT_INTERNAL_ERROR, proc.stderr
    assert "analysis worker died from SIGSEGV (signal 11)" in proc.stderr, proc.stderr
    assert "faulthandler trace from the crashed worker is printed above" in proc.stderr
    assert "BrokenProcessPool" in proc.stderr, "the cause must be preserved"


@pytest.mark.requirement("META-08")
def test_a_sigterm_worker_is_not_credited_with_a_faulthandler_trace(
    tmp_path: Path,
) -> None:
    """[if] a SIGTERM worker is credited with a faulthandler trace [then] fail, [else stop]."""
    files = _audio(tmp_path, "one.wav", "two.wav")
    env = {**os.environ, "MDT_SELFKILL_SIGNAL": str(int(signal.SIGTERM))}

    proc = _run_cli(files, env)

    assert proc.returncode == run_mod.EXIT_INTERNAL_ERROR, proc.stderr
    assert "analysis worker died from SIGTERM (signal 15)" in proc.stderr, proc.stderr
    assert "does not trace SIGTERM (signal 15)" in proc.stderr, proc.stderr
    assert "trace from the crashed worker is printed above" not in proc.stderr


@pytest.mark.requirement("META-08")
def test_executor_cleanup_sigterms_are_labelled_not_reported_as_causes(
    tmp_path: Path,
) -> None:
    """[if] a survivor's teardown SIGTERM is reported as a cause [then] fail, [else stop]."""
    # The sleeper is submitted first so it is certainly busy when the other
    # worker crashes, which makes the executor terminate it during teardown.
    files = _audio(tmp_path, "sleeper.wav", "crasher.wav")
    env = {
        **os.environ,
        "MDT_SELFKILL_SIGNAL": str(int(signal.SIGSEGV)),
        "MDT_SELFKILL_DELAY": "4",
    }

    proc = _run_cli(files, env)

    assert proc.returncode == run_mod.EXIT_INTERNAL_ERROR, proc.stderr
    assert "analysis worker died from SIGSEGV (signal 11)" in proc.stderr, proc.stderr
    assert "terminated by SIGTERM (signal 15) during executor cleanup" in proc.stderr, (
        proc.stderr
    )
    # The control: SIGTERM must not be presented as a second cause.
    assert "died from SIGSEGV (signal 11), SIGTERM" not in proc.stderr


@pytest.mark.requirement("META-08")
def test_a_worker_dying_during_submission_is_reported_not_leaked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selfkill: type[SelfKillingBackend]
) -> None:
    """[if] a worker dies mid-submission and a bare BrokenProcessPool escapes [then] fail, [else stop]."""
    monkeypatch.setenv("MDT_SELFKILL_SIGNAL", str(int(signal.SIGSEGV)))
    files = _audio(tmp_path, *[f"t{i}.wav" for i in range(40)])
    queue = run_mod.build_queue(files)
    submitted: list[str] = []

    def _slowly():
        # Real targets, fed slowly, so submission is guaranteed to still be in
        # flight when the first worker dies. Nothing about the pool is faked.
        for ref in queue:
            submitted.append(ref.stable_id)
            time.sleep(0.25)
            yield ref.stable_id, str(ref.path)

    with pytest.raises(RuntimeError) as excinfo:
        pool_mod.run_pool(_slowly(), backend=selfkill, workers=2)

    assert "analysis worker died from SIGSEGV (signal 11)" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, pool_mod.BrokenProcessPool)
    # The discriminator: submission was cut short, so this is the submit path
    # and not the result-collection path.
    assert len(submitted) < len(files), (len(submitted), len(files))


# ---------------------------------------------------------------------------
# Signal rendering
# ---------------------------------------------------------------------------


def _signal_number_with_no_name() -> int:
    """A valid-looking signal number that this platform gives no enum member."""
    named = {member.value for member in signal.Signals}
    for candidate in range(1, 128):
        if candidate not in named:
            return candidate
    raise AssertionError(_ALL_SIGNALS_NAMED)


@pytest.mark.requirement("META-08")
def test_an_unnamed_signal_renders_its_number_instead_of_raising() -> None:
    """[if] an unnamed signal number raises instead of rendering [then] fail, [else stop]."""
    number = _signal_number_with_no_name()

    assert _signal_label(number) == f"unnamed signal {number}"
    message = pool_death_message([number])

    assert f"unnamed signal {number}" in message
    # The control: a named signal still renders symbolically.
    assert "SIGSEGV (signal 11)" in pool_death_message([int(signal.SIGSEGV)])


@pytest.mark.requirement("META-08")
def test_a_lone_sigterm_is_still_reported_as_the_cause() -> None:
    """[if] a lone SIGTERM is demoted to cleanup [then] fail, [else stop]."""
    message = pool_death_message([CLEANUP_SIGNAL])

    assert "analysis worker died from SIGTERM (signal 15)" in message
    assert "cleanup" not in message


@pytest.mark.requirement("META-08")
def test_unreaped_workers_report_that_the_signal_was_unreadable() -> None:
    """[if] no exit code is readable and a signal is invented anyway [then] fail, [else stop]."""

    class _Unreaped:
        exitcode = None

    class _Clean:
        exitcode = 0

    assert worker_exit_signals([_Unreaped(), _Clean()]) == []
    assert pool_death_message([]) == (
        "analysis worker died before its exit signal could be read"
    )


@pytest.mark.requirement("META-08")
def test_traced_signals_match_the_platform_signal_set() -> None:
    """[if] a traced signal is missing on this platform (SIGBUS on Windows) [then] import and trace the rest, [else stop].

    The module import above is the regression check on Windows, where the old
    `signal.SIGBUS` read raised at collection. This asserts the set is exactly the
    fatal signals this platform defines, nothing invented and nothing dropped.
    """
    from apps.analysis.worker_diagnostics import TRACED_SIGNALS

    expected = {
        int(getattr(signal, name))
        for name in ("SIGSEGV", "SIGFPE", "SIGABRT", "SIGBUS", "SIGILL")
        if hasattr(signal, name)
    }
    assert int(signal.SIGSEGV) in TRACED_SIGNALS
    assert set(TRACED_SIGNALS) == expected
