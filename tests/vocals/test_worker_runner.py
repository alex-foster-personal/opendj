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
