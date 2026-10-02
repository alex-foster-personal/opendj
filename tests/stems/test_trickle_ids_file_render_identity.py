"""`trickle --ids-file` chooses WHICH tracks render, never HOW one renders.

PERFBATCH-03 evidence for STEM-43's change to ``apps/stems/cli.py``. The
separation itself lives in ``scripts/stem_bundle_worker.py``; the CLI's only
influence on it is the command line and environment it hands that worker. So
the claim under test is: for a track rendered both with and without
``--ids-file``, the worker receives a byte-identical command line and device.

No separation runs here. ``MDT_STEM_WORKER_PYTHON`` (the CLI's own documented
override) points the real ``run_worker`` at a recorder that writes down exactly
what the worker process was given. The subprocess, the argv and the environment
are the production ones; only the model is absent.

Mini-PRD
========
* [if] a listed track gets a different worker argv or device than it gets with
  no list [then ⛔️] the identity test fails naming the difference.
* [if] ``--ids-file`` renders a track the unrestricted run would not [then ⛔️]
  the subset test fails.
* [if] the recorder was never reached [then ⛔️] the control fails, so an empty
  record cannot read as "identical".

-Claude
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.stems import cli as stems_cli
from apps.vocals.cli import VocalTrack

STABLE_IDS: tuple[str, ...] = ("track-a", "track-b", "track-c")
RECORD_ENV: str = "MDT_TEST_WORKER_RECORD"
RECORDER_SOURCE: str = """#!{python}
import json, os, sys
with open(os.environ["{record_env}"], "a", encoding="utf-8") as record:
    record.write(json.dumps({{
        "argv": sys.argv[1:],
        "device_env": os.environ.get("MDT_STEM_WORKER_DEVICE"),
    }}) + "\\n")
print(json.dumps({{"realtime_factor": 1.0, "device_used": "recorder"}}))
"""


def _tracks(audio_dir: Path) -> list[VocalTrack]:
    return [
        VocalTrack(
            stable_id=stable_id,
            vendor_id=stable_id,
            title=stable_id,
            length_s=60 + index,
            folder_path=None,
            analysis_data_path=None,
            audio_path=audio_dir / f"{stable_id}.wav",
            audio_on_disk=True,
        )
        for index, stable_id in enumerate(STABLE_IDS)
    ]


def _worker_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_name: str, extra_argv: list[str]
) -> dict[str, dict[str, Any]]:
    """Run a live trickle and return what the worker was handed, per stable id."""
    recorder = tmp_path / f"recorder-{run_name}.py"
    recorder.write_text(
        RECORDER_SOURCE.format(python=sys.executable, record_env=RECORD_ENV),
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    record = tmp_path / f"record-{run_name}.jsonl"
    record.write_text("", encoding="utf-8")
    monkeypatch.setenv("MDT_STEM_WORKER_PYTHON", str(recorder))
    monkeypatch.setenv(RECORD_ENV, str(record))
    monkeypatch.delenv("MDT_STEM_WORKER_DEVICE", raising=False)
    monkeypatch.delenv("MDT_VOCAL_WORKER_DEVICE", raising=False)
    # One data dir for both runs, so the out-dir argument is comparable. The
    # recorder writes no bundle, so every track is still todo on the second run.
    data_dir = tmp_path / "data"
    monkeypatch.setattr(stems_cli, "load_tracks", lambda _ctx, _playlist: _tracks(tmp_path))
    monkeypatch.setattr(stems_cli, "best_playlist_rank", lambda _ctx, _tracks: {})
    args = stems_cli.build_parser().parse_args(
        ["trickle", "--live", "--limit", "10", "--data-dir", str(data_dir), *extra_argv]
    )
    assert args.func(args) == 0
    calls = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    by_id = {call["argv"][call["argv"].index("--stable-id") + 1]: call for call in calls}
    assert len(by_id) == len(calls), "a track was rendered twice in one run"
    return by_id


def _ids_file(tmp_path: Path, ids: list[str]) -> Path:
    path = tmp_path / "ids.txt"
    path.write_text("".join(f"{stable_id}\n" for stable_id in ids), encoding="utf-8")
    return path


def test_control_the_recorder_sees_every_track_of_an_unrestricted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _worker_calls(tmp_path, monkeypatch, "all", [])
    assert set(calls) == set(STABLE_IDS)
    assert calls["track-b"]["argv"][0] == str(stems_cli.WORKER_SCRIPT)


def test_a_listed_track_gets_the_same_worker_call_as_without_a_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrestricted = _worker_calls(tmp_path, monkeypatch, "all", [])
    listed = ["track-b", "track-c"]
    restricted = _worker_calls(
        tmp_path, monkeypatch, "listed", ["--ids-file", str(_ids_file(tmp_path, listed))]
    )
    assert set(restricted) == set(listed)
    for stable_id in listed:
        assert restricted[stable_id] == unrestricted[stable_id], stable_id


def test_a_list_never_adds_a_track_the_unrestricted_run_would_not_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids_file = _ids_file(tmp_path, ["track-a", "not-in-the-library"])
    restricted = _worker_calls(tmp_path, monkeypatch, "listed", ["--ids-file", str(ids_file)])
    assert set(restricted) == {"track-a"}
