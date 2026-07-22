"""Tests for scripts/vocal_worker_runner.py (Windows farm-out loop).

The demucs worker itself is always stubbed here (heavy end-to-end coverage
lives in tests/vocals/test_worker_portability.py) -- these tests exercise
the runner's own control flow: STOP sentinel, the GPU gate, outbox
wrapping, and success/failure file movement.

Regression one-liners:
  - if a STOP sentinel present before the first file doesn't skip the pass then broken
  - if a busy GPU reading doesn't hold the pass then broken
  - if success doesn't write outbox/<stem>.json with worker metadata then broken
  - if success doesn't delete the input and log a done: line then broken
  - if a worker failure doesn't move the input to inbox/failed/ then broken
  - if gpu_is_busy doesn't trip on util OR mem alone then broken
  - if publication fails then partial final or temp output must not remain
  - if publication skips flush, fsync, or close before replace then broken
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import scripts.vocal_worker_runner as runner

pytestmark = pytest.mark.requirement("CAT-05")


def _make_inbox_file(inbox: Path, name: str = "track.wav") -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / name
    p.write_bytes(b"not real audio -- worker is stubbed in these tests")
    return p


def _worker_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": 1,
        "source": "demucs-htdemucs",
        "fps": 2.0,
        "duration_s": 10.0,
        "coverage_pct": 40.0,
        "regions": [{"start_s": 1.0, "end_s": 5.0, "confidence": 0.7}],
        "params": {"hop_s": 0.5},
        "device": "cpu",
    }
    base.update(overrides)
    return base


# ----- gpu_is_busy pure logic ------------------------------------------------------

def test_gpu_is_busy_trips_on_util_alone() -> None:
    reading = runner.GpuReading(utilization_pct=10, memory_used_mb=0)
    assert runner.gpu_is_busy(reading) is True


def test_gpu_is_busy_trips_on_mem_alone() -> None:
    reading = runner.GpuReading(utilization_pct=0, memory_used_mb=500)
    assert runner.gpu_is_busy(reading) is True


def test_gpu_is_busy_false_when_idle() -> None:
    reading = runner.GpuReading(utilization_pct=9, memory_used_mb=499)
    assert runner.gpu_is_busy(reading) is False


def test_gpu_is_busy_uses_supplied_thresholds() -> None:
    reading = runner.GpuReading(utilization_pct=50, memory_used_mb=2047)
    assert runner.gpu_is_busy(reading, utilization_threshold=50, memory_threshold_mb=2048)
    assert not runner.gpu_is_busy(reading, utilization_threshold=51, memory_threshold_mb=2048)


# ----- read_gpu_state: parses a stubbed nvidia-smi's CSV output --------------------

@pytest.mark.skipif(sys.platform == "win32", reason="posix shebang script stub")
def test_read_gpu_state_parses_stubbed_nvidia_smi(tmp_path: Path) -> None:
    stub = tmp_path / "fake_nvidia_smi.sh"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "23, 1234"
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    reading = runner.read_gpu_state(str(stub))
    assert reading.utilization_pct == 23
    assert reading.memory_used_mb == 1234


@pytest.mark.skipif(sys.platform == "win32", reason="posix shebang script stub")
def test_read_gpu_state_raises_on_nonzero_exit(tmp_path: Path) -> None:
    stub = tmp_path / "broken_nvidia_smi.sh"
    stub.write_text("#!/bin/sh\necho 'no gpu' 1>&2\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    with pytest.raises(RuntimeError, match="nvidia-smi failed"):
        runner.read_gpu_state(str(stub))


# ----- process_one: success ---------------------------------------------------------

def test_process_one_success_writes_outbox_deletes_input_logs_done(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox)
    result = _worker_result()

    ok = runner.process_one(
        audio, outbox, logs, "cpu", run_worker_fn=lambda p: result
    )
    assert ok is True
    assert not audio.exists(), "input must be deleted on success"

    out_json = outbox / "track.json"
    assert out_json.is_file()
    import json

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["regions"] == result["regions"]
    assert payload["worker"]["host"]
    assert payload["worker"]["device_used"] == "cpu"
    assert len(payload["worker"]["script_sha256"]) == 64

    log_text = (logs / "worker.log").read_text(encoding="utf-8")
    assert "done: track" in log_text


def test_process_one_failure_moves_to_failed_dir_and_logs(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox, "bad.wav")

    def _boom(_: Path) -> dict[str, Any]:
        raise RuntimeError("boom: worker exploded")

    ok = runner.process_one(audio, outbox, logs, "cpu", run_worker_fn=_boom)
    assert ok is False
    assert not audio.exists()
    failed = inbox / "failed" / "bad.wav"
    assert failed.is_file()
    assert not outbox.exists() or not any(outbox.iterdir())

    log_text = (logs / "worker.log").read_text(encoding="utf-8")
    assert "failed: bad.wav" in log_text
    assert "boom: worker exploded" in log_text


def test_process_one_never_overwrites_an_existing_same_stem_result(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox, "track.wav")
    outbox.mkdir()
    existing = outbox / "track.json"
    existing.write_text('{"existing": true}', encoding="utf-8")

    calls: list[Path] = []
    ok = runner.process_one(
        audio,
        outbox,
        logs,
        "cpu",
        run_worker_fn=lambda path: calls.append(path) or _worker_result(),
    )

    assert ok is False
    assert calls == []
    assert existing.read_text(encoding="utf-8") == '{"existing": true}'
    assert (inbox / "failed" / "track.wav").is_file()


@pytest.mark.parametrize("failure_stage", ["serialize", "write", "fsync", "replace"])
def test_process_one_publication_failure_never_leaves_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox, "track.wav")
    result = _worker_result()

    if failure_stage == "serialize":
        result["not_json"] = object()
    elif failure_stage == "write":
        def _partial_write_then_fail(
            _payload: object, result_file: Any, **_kwargs: object
        ) -> None:
            result_file.write('{"partial":')
            raise OSError("injected write failure")

        monkeypatch.setattr(runner.json, "dump", _partial_write_then_fail)
    elif failure_stage == "fsync":
        def _fail_fsync(_fd: int) -> None:
            raise OSError("injected fsync failure")

        monkeypatch.setattr(runner.os, "fsync", _fail_fsync)
    elif failure_stage == "replace":
        def _fail_replace(_source: Path, _target: Path) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(runner.os, "replace", _fail_replace)

    ok = runner.process_one(
        audio, outbox, logs, "cpu", run_worker_fn=lambda _: result
    )

    assert ok is False
    assert not audio.exists()
    assert (inbox / "failed" / "track.wav").is_file()
    assert list(outbox.iterdir()) == []


def test_process_one_flushes_fsyncs_and_closes_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox, "track.wav")
    events: list[str] = []
    real_fsync = runner.os.fsync
    real_replace = runner.os.replace

    def _record_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def _record_replace(source: Path, target: Path) -> None:
        assert source.parent == target.parent == outbox
        assert source != target
        assert source.name.startswith(f".{target.name}.")
        assert source.name.endswith(".tmp")
        with source.open("a", encoding="utf-8"):
            events.append("closed")
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(runner.os, "fsync", _record_fsync)
    monkeypatch.setattr(runner.os, "replace", _record_replace)

    ok = runner.process_one(
        audio, outbox, logs, "cpu", run_worker_fn=lambda _: _worker_result()
    )

    assert ok is True
    assert events == ["fsync", "closed", "replace"]
    assert [path.name for path in outbox.iterdir()] == ["track.json"]


# ----- run_once: STOP sentinel + GPU gate control flow -----------------------------

def test_run_once_stop_sentinel_skips_before_processing(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox)
    (tmp_path / "STOP").write_text("", encoding="utf-8")

    calls: list[Path] = []

    def _track_call(p: Path) -> dict[str, Any]:
        calls.append(p)
        return _worker_result()

    processed = runner.run_once(
        inbox, outbox, logs, "cpu",
        gpu_gate=False,
        run_worker_fn=_track_call,
    )
    assert processed == 0
    assert calls == []
    assert audio.exists(), "file must be left untouched when STOP is present"


def test_run_once_holds_when_gpu_busy(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox)

    calls: list[Path] = []

    def _track_call(p: Path) -> dict[str, Any]:
        calls.append(p)
        return _worker_result()

    busy_reading = runner.GpuReading(utilization_pct=99, memory_used_mb=9000)
    processed = runner.run_once(
        inbox, outbox, logs, "cpu",
        gpu_gate=True,
        gpu_reader=lambda: busy_reading,
        run_worker_fn=_track_call,
    )
    assert processed == 0
    assert calls == []
    assert audio.exists()
    log_text = (logs / "worker.log").read_text(encoding="utf-8")
    assert "hold:" in log_text


def test_run_once_proceeds_when_gpu_idle(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox)

    idle_reading = runner.GpuReading(utilization_pct=0, memory_used_mb=0)
    processed = runner.run_once(
        inbox, outbox, logs, "cpu",
        gpu_gate=True,
        gpu_reader=lambda: idle_reading,
        run_worker_fn=lambda p: _worker_result(),
    )
    assert processed == 1
    assert not audio.exists()
    assert (outbox / "track.json").is_file()


def test_run_once_processes_a_one_file_inbox_end_to_end(tmp_path: Path) -> None:
    """The full acceptance scenario: one file in, worker stubbed, outbox
    JSON with worker metadata out, input gone, done: logged."""
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    audio = _make_inbox_file(inbox, "onlyfile.wav")

    processed = runner.run_once(
        inbox, outbox, logs, "cpu",
        gpu_gate=False,
        run_worker_fn=lambda p: _worker_result(),
    )
    assert processed == 1
    assert not audio.exists()
    assert not any(inbox.iterdir()), "inbox should be empty after processing"

    import json

    payload = json.loads((outbox / "onlyfile.json").read_text(encoding="utf-8"))
    assert set(payload["worker"]) == {"script_sha256", "host", "device_used"}
    assert "done: onlyfile" in (logs / "worker.log").read_text(encoding="utf-8")


def test_run_once_resource_percent_sleeps_to_enforce_duty_cycle(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    logs = tmp_path / "logs"
    _make_inbox_file(inbox)
    resource_file = tmp_path / "RESOURCE_PERCENT"
    resource_file.write_text("50\n", encoding="utf-8")
    now = [10.0]
    sleeps: list[float] = []

    def _worker(_: Path) -> dict[str, Any]:
        now[0] += 4.0
        return _worker_result()

    processed = runner.run_once(
        inbox, outbox, logs, "cpu", gpu_gate=False,
        run_worker_fn=_worker, resource_percent_file=resource_file,
        clock_fn=lambda: now[0], sleep_fn=sleeps.append,
    )

    assert processed == 1
    assert sleeps == [1.0, 1.0, 1.0, 1.0]


@pytest.mark.parametrize("value", ["0", "101", "not-an-integer", "50.5"])
def test_run_once_rejects_invalid_resource_percent_before_work(
    tmp_path: Path, value: str,
) -> None:
    inbox = tmp_path / "inbox"
    resource_file = tmp_path / "RESOURCE_PERCENT"
    _make_inbox_file(inbox)
    resource_file.write_text(value, encoding="utf-8")

    with pytest.raises(ValueError, match="resource percent"):
        runner.run_once(
            inbox, tmp_path / "outbox", tmp_path / "logs", "cpu", gpu_gate=False,
            resource_percent_file=resource_file,
        )


def test_run_once_stop_sentinel_interrupts_duty_sleep(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _make_inbox_file(inbox)
    resource_file = tmp_path / "RESOURCE_PERCENT"
    resource_file.write_text("50", encoding="utf-8")
    now = [0.0]
    sleeps: list[float] = []

    def _sleep(duration: float) -> None:
        sleeps.append(duration)
        (tmp_path / "STOP").write_text("", encoding="utf-8")

    def _worker(_: Path) -> dict[str, Any]:
        now[0] += 3.0
        return _worker_result()

    processed = runner.run_once(
        inbox, tmp_path / "outbox", tmp_path / "logs", "cpu", gpu_gate=False,
        run_worker_fn=_worker, resource_percent_file=resource_file,
        clock_fn=lambda: now[0], sleep_fn=_sleep,
    )

    assert processed == 1
    assert sleeps == [1.0]


def test_run_loop_stop_sentinel_interrupts_empty_poll_sleep(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    sleeps: list[float] = []

    def _sleep(duration: float) -> None:
        sleeps.append(duration)
        (tmp_path / "STOP").write_text("", encoding="utf-8")

    runner.run_loop(
        inbox, tmp_path / "outbox", tmp_path / "logs", "cpu",
        gpu_gate=False, poll_seconds=60, sleep_fn=_sleep,
    )

    assert sleeps == [1.0]


# ----- CLI parser --------------------------------------------------------------------

def test_build_parser_rejects_once_and_loop_together(tmp_path: Path) -> None:
    parser = runner.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--inbox", str(tmp_path / "in"),
            "--outbox", str(tmp_path / "out"),
            "--logs", str(tmp_path / "logs"),
            "--once", "--loop",
        ])


def test_build_parser_defaults(tmp_path: Path) -> None:
    parser = runner.build_parser()
    args = parser.parse_args([
        "--inbox", str(tmp_path / "in"),
        "--outbox", str(tmp_path / "out"),
        "--logs", str(tmp_path / "logs"),
    ])
    assert args.device == "auto"
    assert args.gpu_gate is False
    assert args.loop is False
    assert args.poll_seconds == runner.DEFAULT_POLL_SECONDS


def test_build_parser_accepts_resource_and_gpu_gate_thresholds(tmp_path: Path) -> None:
    args = runner.build_parser().parse_args([
        "--inbox", str(tmp_path / "in"), "--outbox", str(tmp_path / "out"),
        "--logs", str(tmp_path / "logs"), "--gpu-utilization-threshold", "50",
        "--gpu-memory-threshold-mb", "2048", "--resource-percent-file", str(tmp_path / "RESOURCE_PERCENT"),
    ])
    assert args.gpu_utilization_threshold == 50
    assert args.gpu_memory_threshold_mb == 2048
    assert args.resource_percent_file == tmp_path / "RESOURCE_PERCENT"


def test_main_rejects_missing_resource_percent_file_before_empty_inbox_loop(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="resource percent"):
        runner.main([
            "--inbox", str(tmp_path / "inbox"), "--outbox", str(tmp_path / "outbox"),
            "--logs", str(tmp_path / "logs"), "--resource-percent-file",
            str(tmp_path / "missing-resource-percent"), "--loop",
        ])


def test_run_worker_cmd_sets_explicit_resource_controls() -> None:
    wrapper = (Path(__file__).parents[2] / "scripts" / "run_worker.cmd").read_text(
        encoding="utf-8",
    )
    assert r"set BENCH_PYTHON=D:\tmp\demucs-bench\.venv\Scripts\python.exe" in wrapper
    assert r"set HF_HOME=D:\tmp\demucs-bench\hf-home" in wrapper
    assert r"set MDT_PATH_MAP=D:\mdt-data\path-map.json" in wrapper
    assert r"set MDT_FFMPEG=D:\tools\ffmpeg\bin\ffmpeg.exe" in wrapper
    assert "set OMP_NUM_THREADS=3" in wrapper
    assert "set MKL_NUM_THREADS=3" in wrapper
    assert "set OPENBLAS_NUM_THREADS=3" in wrapper
    assert 'cd /d "%REPO_ROOT%" || exit /b 1' in wrapper
    assert '"%BENCH_PYTHON%" -m scripts.vocal_worker_runner' in wrapper
    assert "--windows-below-normal" in wrapper
    assert "--cpu-affinity-mask 0x07" in wrapper
    assert "start " not in wrapper.lower()
    assert "exit /b %ERRORLEVEL%" in wrapper
    assert "--gpu-utilization-threshold 50" in wrapper
    assert "--gpu-memory-threshold-mb 2048" in wrapper
    assert "--resource-percent-file \"%RESOURCE_PERCENT_FILE%\"" in wrapper


def test_build_parser_accepts_windows_process_resource_controls(tmp_path: Path) -> None:
    args = runner.build_parser().parse_args([
        "--inbox", str(tmp_path / "in"), "--outbox", str(tmp_path / "out"),
        "--logs", str(tmp_path / "logs"), "--windows-below-normal",
        "--cpu-affinity-mask", "0x07",
    ])
    assert args.windows_below_normal is True
    assert args.cpu_affinity_mask == 7
