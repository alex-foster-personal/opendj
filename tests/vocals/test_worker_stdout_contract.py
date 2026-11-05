"""The vocal worker's stdout carries one framed result, and nothing else is trusted.

A new user's first vocals request ended in ``json.JSONDecodeError: Expecting
value: line 1 column 1`` after the worker had logged ``done``: ``run_worker``
handed the worker's whole stdout to a bare ``json.loads``, and on a first run
the model weights download announced itself on that stream ahead of the result
(observed in the installed macOS app: stdout began with DOWNLOAD_LINE below).

[if] the vocal worker's stdout is not exactly one framed result [then] run_worker names what it got, [else stop].

Every launch below is a real subprocess started through the real
``_worker_command`` (the packaged-app form: an interpreter plus a script named
by environment). The "real main" workers run ``scripts/vocal_region_worker.py``'s
own ``main()``; only ``analyse`` is replaced, because torch and demucs never
enter the repo environment.

Regression one-liners:
  - if a library that prints to stdout during analysis loses the result then broken
  - if a quiet worker's result differs from what the worker computed then broken
  - if an empty stdout raises anything but an error naming the worker and exit code then broken
  - if a non-zero exit hides the worker's own stderr message then broken
  - if an unframed JSON line is accepted as a result then broken
  - if two result frames are accepted then broken
  - if the worker and the engine disagree about the frame then broken
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.vocals import cache as vcache
from apps.vocals import cli as vcli
from apps.vocals.worker_output import RESULT_FRAME
from tests.vocals._worker_script import VOCAL_REGION_WORKER_SCRIPT, load_vocal_region_worker

pytestmark = pytest.mark.requirement("VOCALS-05")

WORKER_SCRIPT = VOCAL_REGION_WORKER_SCRIPT
FRAME = RESULT_FRAME
# The first line of the worker's stdout on a first run in the installed app.
DOWNLOAD_LINE = (
    'Downloading: "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"'
    " to /Users/dev/.cache/torch/hub/checkpoints/955717e8-8726e21a.th"
)

# What the real worker printed for the 30 s acceptance voice fixture on CPU.
RESULT: dict[str, Any] = {
    "schema": vcache.VOCAL_CACHE_SCHEMA,
    "source": "demucs-htdemucs",
    "audio_path": "track.wav",
    "source_sample_rate": 44100,
    "analysis_sample_rate": 44100,
    "duration_s": 30.0,
    "device": "cpu",
    "params": {
        "model": "htdemucs",
        "hop_s": 0.5,
        "on_ratio": 0.1,
        "off_ratio": 0.05,
        "thresholds_adapted": False,
        "merge_gap_s": 1.5,
        "min_region_s": 1.0,
        "confidence_gain": 2.5,
    },
    "timings": {"load_s": 1.9, "separate_s": 24.9},
    "regions": [{"start_s": 0.0, "end_s": 9.0, "confidence": 1.0}],
    "coverage_pct": 30.0,
    "fps": 2.0,
}

_REAL_MAIN = """
import importlib.util, json, os, subprocess, sys
spec = importlib.util.spec_from_file_location("vocal_region_worker", {worker!r})
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
NOISY = {noisy!r}
def analyse(audio_path, device_pref):
    if NOISY:
        print({download!r})                                      # Python-level
        os.write(1, b"  3%|#         | 2.4M/80.2M\\n")            # C-level write
        subprocess.run([sys.executable, "-c", "print('decoder banner')"], check=True)
    return json.loads({result!r})
worker.analyse = analyse
worker.main()
"""


def real_main_worker(tmp_path: Path, *, noisy: bool) -> Path:
    """The shipped worker's own ``main()`` around a stand-in ``analyse``."""
    script = tmp_path / ("noisy_main.py" if noisy else "quiet_main.py")
    script.write_text(
        _REAL_MAIN.format(worker=str(WORKER_SCRIPT), noisy=noisy, result=json.dumps(RESULT), download=DOWNLOAD_LINE),
        encoding="utf-8",
    )
    return script


def scripted_worker(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / f"{name}.py"
    script.write_text("import sys\n" + body, encoding="utf-8")
    return script


def launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: Path) -> dict[str, Any]:
    """Run ``script`` as the worker through the real launch path."""
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setenv(vcli.PACKAGED_WORKER_PYTHON_ENV, sys.executable)
    monkeypatch.setenv(vcli.PACKAGED_WORKER_SCRIPT_ENV, str(script))
    assert vcli._worker_command(audio)[:2] == [sys.executable, str(script)]
    return vcli.run_worker(audio, timeout_s=60)


def test_library_chatter_on_stdout_does_not_lose_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a library prints to stdout during analysis [then] the worker's result still parses, [else stop]."""
    assert launch(monkeypatch, tmp_path, real_main_worker(tmp_path, noisy=True)) == RESULT


def test_quiet_worker_result_is_returned_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the worker prints nothing but its result [then] run_worker returns it unchanged, [else stop]."""
    assert launch(monkeypatch, tmp_path, real_main_worker(tmp_path, noisy=False)) == RESULT


def test_noise_before_a_framed_result_parses_the_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] noise precedes the framed result on stdout [then] the framed regions are returned, [else stop]."""
    script = scripted_worker(
        tmp_path,
        "noise_then_frame",
        f"print({DOWNLOAD_LINE!r})\nprint({FRAME!r} + {json.dumps(RESULT)!r})\n",
    )
    assert launch(monkeypatch, tmp_path, script)["regions"] == RESULT["regions"]


def test_empty_stdout_with_exit_zero_names_the_worker_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the worker exits 0 with empty stdout [then] the error names the worker and what it got, [else stop]."""
    script = scripted_worker(tmp_path, "silent", "print('done: 5 regions cov=81.0%', file=sys.stderr)\n")
    with pytest.raises(RuntimeError) as caught:
        launch(monkeypatch, tmp_path, script)
    message = str(caught.value)
    assert "vocal_region_worker (exit 0) wrote 0 result frames" in message
    assert "stdout began '' (0 chars)" in message
    assert "done: 5 regions cov=81.0%" in message  # the bounded stderr tail


def test_non_zero_exit_carries_the_workers_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the worker exits non-zero [then] the error carries its exit code and stderr message, [else stop]."""
    script = scripted_worker(
        tmp_path,
        "failing",
        "print('RuntimeError: weights download refused', file=sys.stderr)\nsys.exit(3)\n",
    )
    with pytest.raises(RuntimeError) as caught:
        launch(monkeypatch, tmp_path, script)
    message = str(caught.value)
    assert "vocal_region_worker failed (exit 3)" in message
    assert "RuntimeError: weights download refused" in message


def test_unframed_json_is_not_accepted_as_a_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] stdout ends in a JSON line with no frame [then] it is refused, never guessed at, [else stop]."""
    script = scripted_worker(tmp_path, "unframed", f"print('banner')\nprint({json.dumps(RESULT)!r})\n")
    with pytest.raises(RuntimeError) as caught:
        launch(monkeypatch, tmp_path, script)
    message = str(caught.value)
    assert "wrote 0 result frames" in message
    assert "stdout began 'banner" in message


def test_two_result_frames_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] stdout carries two result frames [then] neither is trusted, [else stop]."""
    line = f"print({FRAME!r} + {json.dumps(RESULT)!r})\n"
    with pytest.raises(RuntimeError, match="wrote 2 result frames"):
        launch(monkeypatch, tmp_path, scripted_worker(tmp_path, "twice", line + line))


def test_a_frame_that_is_not_a_json_object_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the result frame is not a JSON object [then] the error says so and quotes it, [else stop]."""
    script = scripted_worker(tmp_path, "torn", f"print({FRAME!r} + '{{\"regions\": [')\n")
    with pytest.raises(RuntimeError) as caught:
        launch(monkeypatch, tmp_path, script)
    assert "result frame that is not a JSON object" in str(caught.value)
    assert '{"regions": [' in str(caught.value)


def test_worker_and_engine_agree_on_the_frame() -> None:
    """[if] the worker's frame differs from the one the engine parses [then] this fails, [else stop]."""
    from apps.vocals import worker_output

    worker = load_vocal_region_worker(module_name="vocal_region_worker_frame")
    assert worker.RESULT_FRAME == worker_output.RESULT_FRAME == FRAME


def test_worker_stdout_is_exactly_the_framed_line(tmp_path: Path) -> None:
    """[if] anything in the worker process writes to stdout [then] it lands on stderr, [else stop]."""
    audio = tmp_path / "track.wav"
    audio.write_bytes(b"RIFF")
    proc = subprocess.run(
        [sys.executable, str(real_main_worker(tmp_path, noisy=True)), "--device", "cpu", str(audio)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == FRAME + json.dumps(RESULT) + "\n"
    for chatter in (DOWNLOAD_LINE, "2.4M/80.2M", "decoder banner"):
        assert chatter in proc.stderr
