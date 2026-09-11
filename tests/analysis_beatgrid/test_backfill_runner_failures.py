"""`own_beatgrid.backfill`: runner exit-code and JSON-shape failure mapping.

Companion to `test_backfill_write.py`, which is at the 600-line file-size
ceiling. These tests call `run_runner` directly with a provisioned
`MDT_BEATGRID_RUNNER_PYTHON` executable under `tmp_path`.

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis.backends.base import BackendNotAvailable, TrackUnreadable, TrackVanished
from apps.analysis.backends.own_beatgrid import (
    RUNNER_PYTHON_ENV,
    RunnerPayloadError,
    run_runner,
)


def _write_runner(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_runner.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _out_path_reader() -> str:
    return (
        "def _out_path() -> Path:\n"
        "    for i, arg in enumerate(sys.argv):\n"
        "        if arg == '--out' and i + 1 < len(sys.argv):\n"
        "            return Path(sys.argv[i + 1])\n"
        "    raise SystemExit('no --out in argv')\n"
    )


def test_a_nonzero_runner_exit_is_backend_unavailable_not_per_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _write_runner(tmp_path, "sys.exit(7)")
    monkeypatch.setenv(RUNNER_PYTHON_ENV, str(script))
    audio_path = tmp_path / "a.wav"
    audio_path.write_bytes(b"not real audio")

    with pytest.raises(BackendNotAvailable, match="exited 7") as exc_info:
        run_runner(audio_path, tmp_path / "w.ckpt", device="cpu")

    caught = exc_info.value
    assert not isinstance(caught, TrackUnreadable)
    assert not isinstance(caught, TrackVanished)


def test_invalid_runner_json_is_a_payload_error_not_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        _out_path_reader()
        + "_out_path().write_text('{not json', encoding='utf-8')\n"
    )
    script = _write_runner(tmp_path, body)
    monkeypatch.setenv(RUNNER_PYTHON_ENV, str(script))
    audio_path = tmp_path / "a.wav"
    audio_path.write_bytes(b"not real audio")

    with pytest.raises(RunnerPayloadError, match="cannot parse") as exc_info:
        run_runner(audio_path, tmp_path / "w.ckpt", device="cpu")

    assert not isinstance(exc_info.value, TrackUnreadable)


def test_a_non_object_runner_json_is_a_payload_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        _out_path_reader()
        + "_out_path().write_text('[]', encoding='utf-8')\n"
    )
    script = _write_runner(tmp_path, body)
    monkeypatch.setenv(RUNNER_PYTHON_ENV, str(script))
    audio_path = tmp_path / "a.wav"
    audio_path.write_bytes(b"not real audio")

    with pytest.raises(RunnerPayloadError, match="not an object") as exc_info:
        run_runner(audio_path, tmp_path / "w.ckpt", device="cpu")

    assert not isinstance(exc_info.value, TrackUnreadable)
