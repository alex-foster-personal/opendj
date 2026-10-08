"""A real engine on a fresh data dir: the ahead drain finishes tracks, not lanes (NATIVE-21).

the maintainer, Wed 7 Oct 2026: "in-app analyses still aren't working". A fresh folder
import of 1274 tracks on the installed 1.0.0 app had loudness at 444 of 1274
after ~85 min and beatgrid/key reporting ``waiting_on: loudness`` for every
track, because the drain ran each lane for the WHOLE library before the next
lane started. BPM and key for a stranger's import arrived hours later, or
never while a deck kept playing.

This test starts the real engine (``python -m apps.engine_core serve``) on an
empty data dir, imports a folder of real audio through the setup API, and
reads the v1 queue rows the drain writes. With more tracks than one lane chunk,
the first beatgrid item must be enqueued BEFORE the last loudness item: the
first tracks are carried through every lane before later tracks start.

Regression lines:
  - if the first beatgrid item is enqueued only after every loudness item then broken
  - if the drain enqueues no loudness work on a host that can decode then broken
  - if no beatgrid item is enqueued within the budget then broken
  - if reading the engine log raises TypeError on Python 3.11 then broken

[if] beatgrid waits for loudness on every track [then] fail, [else stop].
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.store import TERMINAL_STATUSES
from apps.webui.server import ahead_analysis as aa

pytestmark = [
    pytest.mark.requirement("NATIVE-25"),
    pytest.mark.slow,
    pytest.mark.requires_ffmpeg,
    pytest.mark.requires_canonical_decode,
]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_AUDIO: tuple[Path, ...] = (
    *sorted((REPO_ROOT / "tests/fixtures/phase7-dedup").glob("*.*")),
    REPO_ROOT / "tests/fixtures/conformance/03-8-hot-cues/audio/cues.mp3",
)
BOOT_BUDGET_S: float = 90.0
IMPORT_BUDGET_S: float = 120.0
DRAIN_BUDGET_S: float = 600.0


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def _wait_for(what: str, budget_s: float, probe: Any) -> Any:
    deadline = time.monotonic() + budget_s
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except (OSError, sqlite3.OperationalError) as exc:
            last = exc
        else:
            if last:
                return last
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {budget_s:.0f} s (last probe: {last!r})")


def _queue_items(db: Path) -> list[tuple[str, str, str, str]]:
    """(lane, stable_id, state, enqueued_at) for every v1 queue item, oldest first."""
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'analysis_queue_item'").fetchone()
        if has is None:
            return []
        return conn.execute(
            "SELECT lane, stable_id, state, enqueued_at FROM analysis_queue_item ORDER BY enqueued_at, rowid"
        ).fetchall()
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# the test
#-----------------------------------------------------------------------------
def engine_log_text(path: Path) -> str:
    """UTF-8 text of the engine log.

    ``Path.read_text`` does not accept ``newline`` on Python 3.11, which is
    the interpreter CI runs. Passing it raised TypeError in the finally block
    and hid the acceptance result.
    """
    return path.read_text(encoding="utf-8")


def test_a_fresh_import_gets_beatgrid_before_loudness_covers_the_whole_library(tmp_path: Path) -> None:
    """[if] beatgrid is first enqueued after the LAST loudness item [then] broken, [else stop]."""
    missing = [p for p in FIXTURE_AUDIO if not p.is_file()]
    assert not missing, f"fixture audio missing: {missing}"
    assert len(FIXTURE_AUDIO) > aa.LANE_CHUNK, "the folder must span more than one lane chunk"
    music = tmp_path / "music"
    music.mkdir()
    for n, src in enumerate(FIXTURE_AUDIO):
        shutil.copy2(src, music / f"track-{n:02d}{src.suffix}")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    port = _free_port()
    base = f"http://127.0.0.1:{port}/api/v1"
    log_path = tmp_path / "engine.log"
    env = {
        **os.environ,
        aa.AHEAD_ENV: "on",
        "AF_SERVICE_ID": "opendj.test.ahead-live-engine",
        # serve writes this log on stdout. Pin UTF-8 so the finally-block
        # read cannot raise UnicodeDecodeError when the host default is not UTF-8.
        "PYTHONIOENCODING": "utf-8",
    }
    with log_path.open("w", encoding="utf-8", newline="\n") as log_file:
        engine = subprocess.Popen(
            [sys.executable, "-m", "apps.engine_core", "serve", "--data-dir", str(data_dir), "--port", str(port)],
            cwd=REPO_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
    try:
        _wait_for("engine health", BOOT_BUDGET_S, lambda: _http("GET", f"{base}/health"))
        job = _http("POST", f"{base}/setup/import/folder", {"folders": [str(music)]})
        print(f"[INFO] folder import job {job.get('id')} {job.get('status')}")
        done = _wait_for(
            "the folder import job to finish",
            IMPORT_BUDGET_S,
            lambda: (row := _http("GET", f"{base}/jobs/{job['id']}"))["status"] in TERMINAL_STATUSES and row,
        )
        assert done["status"] == "succeeded", f"folder import ended {done['status']}: {done.get('error')}"
        listed = len(_http("GET", f"{base}/tracks/index")["items"])  # also ends the drain's boot grace
        print(f"[INFO] index served; {listed} rows listed")
        assert listed > aa.LANE_CHUNK, f"only {listed} rows imported; need more than one lane chunk"
        db = data_dir / "state" / "state.db"

        def beatgrid_and_loudness_settled() -> list[tuple[str, str, str, str]] | None:
            items = _queue_items(db)
            coverage = _http("GET", f"{base}/ahead-analysis/coverage")
            loudness = coverage.get("lanes", {}).get("loudness")
            if loudness is None:
                return None  # no coverage snapshot yet
            if loudness.get("unavailable"):
                pytest.fail(
                    "UNAVAILABLE: this host cannot produce loudness "
                    f"({loudness['unavailable']}), so lane order is unmeasured. "
                    "This is a capability report, not a pass."
                )
            loudness_open = loudness["missing"]
            has_beatgrid = any(lane == "beatgrid" for lane, *_ in items)
            return items if has_beatgrid and loudness_open == 0 else None

        items = _wait_for("a beatgrid item AND loudness on every track", DRAIN_BUDGET_S, beatgrid_and_loudness_settled)
    finally:
        engine.terminate()
        try:
            engine.wait(timeout=15)
        except subprocess.TimeoutExpired:
            engine.kill()
            engine.wait()
        print(engine_log_text(log_path)[-3000:])

    order = [(lane, at) for lane, _sid, _state, at in items]
    print("[INFO] queue items in enqueue order:", [lane for lane, _at in order])
    loudness_at = [at for lane, at in order if lane == "loudness"]
    first_beatgrid_at = min(at for lane, at in order if lane == "beatgrid")
    assert loudness_at, "if the drain enqueues no loudness work on a host that can decode then broken"
    assert first_beatgrid_at < max(loudness_at), (
        "if the first beatgrid item is enqueued only after every loudness item then broken: "
        f"first beatgrid {first_beatgrid_at}, last loudness {max(loudness_at)}, "
        f"{len(set(sid for lane, sid, *_ in items if lane == 'loudness'))} tracks of loudness first"
    )
