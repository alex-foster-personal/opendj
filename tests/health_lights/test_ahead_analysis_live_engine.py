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

A second test (NATIVE-5886) holds a deck "playing" through the real ui-mirror
PUT, arms AutoPlay through the production client (feed, activate, chart
publish), and requires a beatgrid ATTEMPT for the next-up track while the
deck plays. An xrun spike first holds the work off. This test does not
provision a checkpoint. On that host the attempt is the honest UNAVAILABLE
whose reason contains "no Beat This! checkpoint is provisioned". It is not
a skip. A third test requires the real grid. It runs only when
MDT_BEATGRID_MODEL_TESTS=1. With that gate set, UNAVAILABLE fails the test.
The real-grid test names the pinned checkpoint with MDT_BEATGRID_WEIGHTS
when this host already has that file. Tracks that are neither next-up nor
loaded on a deck must not be attempted.
Fixture audio is checksum-verified against its manifest before it is copied.

Regression lines:
  - if the first beatgrid item is enqueued only after every loudness item then broken
  - if the drain enqueues no loudness work on a host that can decode then broken
  - if loudness is unavailable and the ordering test skips then broken
  - if the next-up track gets no beatgrid attempt then broken
  - if a no-checkpoint UNAVAILABLE is treated as not an attempt then broken
  - if the model gate is on and the next-up track gets no beatgrid record then broken
  - if UNAVAILABLE is accepted while MDT_BEATGRID_MODEL_TESTS=1 then broken
  - if any track other than the next-up and deck tracks is attempted while playing then broken
  - if a new xrun does not hold while-playing work off then broken
  - if reading the engine log raises TypeError on Python 3.11 then broken
  - if fixture audio is copied before its manifest checksum matches then broken

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
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.analysis_beatgrid import weights as beatgrid_weights
from apps.engine_core.jobs.store import TERMINAL_STATUSES
from apps.shared.hashing import sha256_file
from apps.webui.server import ahead_analysis as aa

pytestmark = [
    pytest.mark.requirement("NATIVE-25"),
    pytest.mark.slow,
    pytest.mark.requires_ffmpeg,
    pytest.mark.requires_canonical_decode,
]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests/fixtures"
FIXTURE_MANIFEST = FIXTURE_ROOT / "ahead-analysis-live-engine.manifest.json"
FIXTURE_CONTRACT_VERSION = 1
BOOT_BUDGET_S: float = 90.0
IMPORT_BUDGET_S: float = 120.0
DRAIN_BUDGET_S: float = 600.0
_BIND_ATTEMPTS = 3
_BIND_COLLISION_TEXT = ("address already in use", "only one usage of each socket address")
NEXT_UP_GRID_BUDGET_S: float = 300.0
XRUN_QUIET_S: float = 15.0
#: How often the stand-in page re-sends its mirror. The real page publishes
#: once a second (ui-mirror.ts, `window.setInterval(mirror.publish, 1000)`).
MIRROR_PUBLISH_S: float = 1.0
#: What the page publishes as `xrun_sentinel.detector` before its sentinel is
#: installed (xrun-sentinel.ts, `XrunDetectorState`).
XRUN_DETECTOR_UNARMED: str = "unarmed"


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class _SpawnedEngine:
    origin: str
    proc: subprocess.Popen[Any]
    log_path: Path
    port: int


def _logged_boot_id(log_path: Path) -> str | None:
    """The boot_id this engine printed on its `[OK] opendj engine` line, once it has."""
    for line in engine_log_text(log_path).splitlines():
        if line.startswith("[OK] opendj engine ") and " boot_id=" in line:
            return line.split(" boot_id=", 1)[1].split()[0]
    return None


def _answers_health(spawned: _SpawnedEngine) -> bool:
    """True only when THIS engine answers: health must carry the boot_id it logged.

    A 200 alone is not enough. PR #7574 CI run 40472894070 (issue #7582) and main
    push CI run 40413411224 (Mon 26 Oct 2026, shard 3) both booted a process whose
    free port another runner had already taken: that other process answered health,
    so the boot passed, and the real engine then exited with "address already in
    use" or 404'd a job id it never had. Matching the boot_id keeps waiting on an
    impostor's answer until this engine exits, which `_live_engine` then retries
    as a lost port race.
    """
    if spawned.proc.poll() is not None:
        raise AssertionError(
            f"engine exited {spawned.proc.returncode}: {engine_log_text(spawned.log_path)[-2000:]}"
        )
    boot_id = _logged_boot_id(spawned.log_path)
    if boot_id is None:
        return False
    try:
        with urllib.request.urlopen(f"{spawned.origin}/api/v1/health", timeout=2) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read())
    except (OSError, ValueError):
        return False
    return isinstance(body, dict) and body.get("boot_id") == boot_id


def _lost_port_race(spawned: _SpawnedEngine) -> bool:
    """True only when the engine exited because its own port was already bound.

    One log line must carry both the collision text and the exact endpoint the
    engine was given, so an unrelated failure that merely mentions a bound address
    is raised as it is rather than retried. The exit status is not matched: the
    same refused bind exits 3 in main CI run 39004860947 and 1 on a cloud host.
    """
    if spawned.proc.poll() is None:
        return False
    return any(
        f"('127.0.0.1', {spawned.port})" in line
        and any(text in line.lower() for text in _BIND_COLLISION_TEXT)
        for line in engine_log_text(spawned.log_path).splitlines()
    )


def _start_engine(data_dir: Path, port: int, log_path: Path, env: dict[str, str]) -> _SpawnedEngine:
    with log_path.open("w", encoding="utf-8", newline="\n") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apps.engine_core",
                "serve",
                "--data-dir",
                str(data_dir),
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    return _SpawnedEngine(
        origin=f"http://127.0.0.1:{port}",
        proc=proc,
        log_path=log_path,
        port=port,
    )


def _stop_engine(spawned: _SpawnedEngine) -> None:
    spawned.proc.terminate()
    try:
        spawned.proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        spawned.proc.kill()
        spawned.proc.wait()


def _own_job(spawned: _SpawnedEngine, base: str, job_id: str) -> dict[str, Any]:
    if spawned.proc.poll() is not None:
        raise AssertionError(
            f"engine exited {spawned.proc.returncode} while waiting on job {job_id}: "
            f"{engine_log_text(spawned.log_path)[-2000:]}"
        )
    try:
        return _http("GET", f"{base}/jobs/{job_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            health: Any
            try:
                health = _http("GET", f"{base}/health")
            except OSError as health_exc:
                health = health_exc
            raise AssertionError(
                f"folder import job {job_id} is unknown to the responding server "
                f"(HTTP 404); engine.poll()={spawned.proc.poll()!r}; "
                f"health={health!r}; log tail:\n{engine_log_text(spawned.log_path)[-2000:]}"
            ) from exc
        raise


def _http(method: str, url: str, body: dict[str, Any] | None = None, *, lease: str | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} | ({} if lease is None else {"x-opendj-lease": lease})
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def _wait_for(what: str, budget_s: float, probe: Any, *, sleep_s: float = 1.0) -> Any:
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
        time.sleep(sleep_s)
    raise AssertionError(f"{what} did not happen within {budget_s:.0f} s (last probe: {last!r})")


def _queue_items(db: Path) -> list[tuple[str, str, str, str, str]]:
    """(lane, stable_id, state, enqueued_at, reason) for every v1 queue item, oldest first."""
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'analysis_queue_item'").fetchone()
        if has is None:
            return []
        rows = conn.execute(
            "SELECT lane, stable_id, state, enqueued_at, reason "
            "FROM analysis_queue_item ORDER BY enqueued_at, rowid"
        ).fetchall()
    finally:
        conn.close()
    return [(lane, sid, state, at, reason or "") for lane, sid, state, at, reason in rows]


def _verified_fixture_audio(manifest_path: Path = FIXTURE_MANIFEST) -> tuple[Path, ...]:
    """Manifest paths, after version and sha256 both match, before any copy.

    A glob of an unmanifested directory would accept a replaced or extra file
    and then treat that new audio as the acceptance subject.
    """
    if not manifest_path.is_file():
        pytest.fail(f"ahead-analysis live-engine fixture manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("contract_version")
    if version != FIXTURE_CONTRACT_VERSION or manifest.get("fixture") != "ahead-analysis-live-engine":
        pytest.fail(
            f"Unsupported ahead-analysis fixture manifest at {manifest_path}: "
            f"expected contract_version={FIXTURE_CONTRACT_VERSION}, "
            "fixture='ahead-analysis-live-engine', got "
            f"contract_version={version!r}, fixture={manifest.get('fixture')!r}."
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        pytest.fail(f"{manifest_path} pins no files")
    root = FIXTURE_ROOT.resolve()
    paths: list[Path] = []
    for rel, pinned in files.items():
        if not isinstance(rel, str) or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
            pytest.fail(f"{manifest_path} has an unsafe path: {rel!r}")
        path = (root / rel).resolve()
        if root not in path.parents:
            pytest.fail(f"{manifest_path} path escapes fixtures: {rel}")
        if not path.is_file():
            pytest.fail(f"canonical fixture missing: {path}")
        if not isinstance(pinned, dict):
            pytest.fail(f"{manifest_path} entry {rel} is not an object")
        expected = pinned.get("sha256")
        actual = sha256_file(path)
        if actual != expected:
            pytest.fail(
                f"{rel} does not match its manifest checksum. Canonical fixtures are "
                "immutable during feature work; regenerating one to make a test pass is "
                f"forbidden. expected {expected}, got {actual}"
            )
        size = path.stat().st_size
        if size != pinned.get("bytes"):
            pytest.fail(
                f"{rel} matches by digest but not by size "
                f"({pinned.get('bytes')} recorded, {size} read)"
            )
        paths.append(path)
    return tuple(paths)


def engine_log_text(path: Path) -> str:
    """UTF-8 text of the engine log.

    ``Path.read_text`` does not accept ``newline`` on Python 3.11, which is
    the interpreter CI runs. Passing it raised TypeError in the finally block
    and hid the acceptance result.
    """
    return path.read_text(encoding="utf-8")


#-----------------------------------------------------------------------------
# a real engine with a folder imported
#-----------------------------------------------------------------------------
@dataclass(frozen=True)
class LiveEngine:
    origin: str
    base: str
    db: Path
    ids: list[str]


def host_beatgrid_weights_env(
    *,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """Point an empty data dir at the pinned checkpoint when this host has it.

    An explicit ``MDT_BEATGRID_WEIGHTS`` is the caller's file. Return nothing
    so the child inherits it, including when that path is unusable: the
    producer refuses a bad override instead of falling through.

    Otherwise name the Torch Hub cache file only when it is present and its
    sha256 is the pin. Production does not search that cache. This test is
    the caller who means that exact file. A missing or unpinned file leaves
    the producer unavailable, and the acceptance fails rather than passing.
    """
    env = os.environ if environ is None else environ
    if env.get(beatgrid_weights.WEIGHTS_PATH_ENV, "").strip():
        return {}
    root = Path.home() if home is None else home
    path = (
        root / ".cache" / "torch" / "hub" / "checkpoints" / beatgrid_weights.CHECKPOINT_FILENAME
    )
    if not path.is_file():
        return {}
    if beatgrid_weights.sha256_of(path) != beatgrid_weights.CHECKPOINT_SHA256:
        return {}
    return {beatgrid_weights.WEIGHTS_PATH_ENV: str(path)}


@contextmanager
def _live_engine(tmp_path: Path, *, provision_beatgrid: bool = False) -> Iterator[LiveEngine]:
    """The engine on an empty data dir with the fixture folder imported; boot grace NOT yet ended."""
    audio = _verified_fixture_audio()
    assert len(audio) > aa.LANE_CHUNK, "the folder must span more than one lane chunk"
    music = tmp_path / "music"
    music.mkdir()
    for n, src in enumerate(audio):
        shutil.copy2(src, music / f"track-{n:02d}{src.suffix}")
    env = {
        **os.environ,
        aa.AHEAD_ENV: "on",
        "AF_SERVICE_ID": "opendj.test.ahead-live-engine",
        # serve writes this log on stdout. Pin UTF-8 so the finally-block
        # read cannot raise UnicodeDecodeError when the host default is not UTF-8.
        "PYTHONIOENCODING": "utf-8",
    }
    if provision_beatgrid:
        env.update(host_beatgrid_weights_env())
    else:
        # This path is the no-checkpoint host. A developer override must not
        # turn the attempt assertion into a real model run.
        env.pop(beatgrid_weights.WEIGHTS_PATH_ENV, None)
    lost: list[int] = []
    spawned: _SpawnedEngine | None = None
    data_dir: Path | None = None
    try:
        for _attempt in range(_BIND_ATTEMPTS):
            port = _free_port()
            data_dir = tmp_path / f"data-{_attempt}"
            data_dir.mkdir()
            log_path = tmp_path / "engine.log"
            spawned = _start_engine(data_dir, port, log_path, env)
            try:
                _wait_for(
                    "engine health",
                    BOOT_BUDGET_S,
                    lambda current=spawned: _answers_health(current),
                )
            except AssertionError as exc:
                if not _lost_port_race(spawned):
                    raise AssertionError(
                        f"{exc}\n{engine_log_text(spawned.log_path)[-2000:]}"
                    ) from exc
                lost.append(port)
                _stop_engine(spawned)
                spawned = None
                continue
            break
        else:
            raise AssertionError(f"lost the loopback port race {len(lost)} times: ports {lost}")
        assert spawned is not None and data_dir is not None
        base = f"{spawned.origin}/api/v1"
        job = _http("POST", f"{base}/setup/import/folder", {"folders": [str(music)]})
        print(f"[INFO] folder import job {job.get('id')} {job.get('status')}")
        try:
            done = _wait_for(
                "the folder import job to finish",
                IMPORT_BUDGET_S,
                lambda: (row := _own_job(spawned, base, job["id"]))["status"] in TERMINAL_STATUSES
                and row,
            )
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}\nengine.poll()={spawned.proc.poll()!r}\n"
                f"{engine_log_text(spawned.log_path)[-2000:]}"
            ) from exc
        assert done["status"] == "succeeded", f"folder import ended {done['status']}: {done.get('error')}"
        db = data_dir / "state" / "state.db"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            ids = [row[0] for row in conn.execute("SELECT stable_id FROM tracks ORDER BY file_path")]
        finally:
            conn.close()
        assert len(ids) > aa.LANE_CHUNK, f"only {len(ids)} rows imported; need more than one lane chunk"
        yield LiveEngine(origin=spawned.origin, base=base, db=db, ids=ids)
    finally:
        if spawned is not None:
            _stop_engine(spawned)
            print(engine_log_text(spawned.log_path)[-3000:])


def _end_boot_grace(engine: LiveEngine) -> None:
    listed = len(_http("GET", f"{engine.base}/tracks/index")["items"])
    print(f"[INFO] index served (boot grace over); {listed} rows listed")


def _own_record_ids(db: Path, backend: str) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return {row[0] for row in conn.execute("SELECT stable_id FROM analysis WHERE backend = ?", (backend,))}
    finally:
        conn.close()


_NO_CHECKPOINT = "no Beat This! checkpoint is provisioned"
_ATTEMPT_RANK = {"attempted": 0, "unavailable": 1, "record": 2}


def _no_checkpoint_unavailable(reason: str) -> bool:
    """True when the producer refused because this host has no Beat This! checkpoint.

    The queue names that refusal on the job. That terminal UNAVAILABLE is the
    attempt: the scheduler picked the track and reported the honest reason.
    """
    return _NO_CHECKPOINT in reason


def _note_attempt(
    observed: dict[str, str], reasons: dict[str, str], sid: str, kind: str, reason: str
) -> None:
    previous = observed.get(sid)
    if previous is None or _ATTEMPT_RANK[kind] >= _ATTEMPT_RANK[previous]:
        observed[sid] = kind
        if reason:
            reasons[sid] = reason


def _node_executable() -> str:
    found = shutil.which("node")
    if found is None:
        pytest.fail(
            "UNAVAILABLE: node is not on PATH, so the production AutoPlay client "
            "cannot be armed. This is a capability report, not a pass."
        )
    assert found is not None
    return found


def _arm_autoplay_next_up(engine: LiveEngine, chart: list[str]) -> None:
    """Arm AutoPlay through the production client and require the engine to store that head."""
    script = REPO_ROOT / "apps/webui/frontend/tests/live/arm-autoplay-next-up.mjs"
    proc = subprocess.run(
        [_node_executable(), str(script)],
        cwd=REPO_ROOT / "apps/webui/frontend",
        input=json.dumps(
            {"origin": engine.origin, "feed": engine.ids, "chart": chart, "writer": MirrorPage.CLIENT_ID}
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "if arming AutoPlay does not store next-up on the live engine then broken: "
            f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        )
    stored = json.loads(proc.stdout.strip().splitlines()[-1])
    seen = _http("GET", f"{engine.base}/ahead-analysis/coverage")["while_playing"]["next_up"]
    assert stored == chart, f"production client reported {stored}, chart was {chart}"
    assert seen == chart, (
        "if the production AutoPlay client does not set the engine next-up then broken: "
        f"{seen}"
    )


class MirrorPage:
    """A stand-in for the page's mirror publisher: one playing deck, re-sent every second.

    The engine reads no xrun counter from a mirror it received more than
    ``aa.MIRROR_MAX_AGE_S`` ago, because a page that stopped publishing has
    hung. One PUT followed by a long wait is that hung page, so this keeps
    publishing the current document the way the real page does. Like the page,
    it holds the mirror lease, and the AutoPlay client writes next-up under it.
    """

    CLIENT_ID = "ahead-live-engine-test"

    def __init__(self, engine: LiveEngine) -> None:
        self._engine = engine
        self._body: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._republish, name="mirror-page", daemon=True)
        self.sent: int = 0
        self.failures: list[OSError] = []

    def __enter__(self) -> MirrorPage:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10.0)
        assert not self._thread.is_alive(), "the stand-in page did not stop publishing"
        if exc_type is None:
            assert not self.failures, f"the stand-in page failed to publish its mirror: {self.failures}"

    def publish(self, *, deck_track: str | None, xruns: int, detector: str = aa.XRUN_DETECTOR_ARMED) -> None:
        """Replace the document and send it now; the loop keeps re-sending it."""
        deck: dict[str, Any] = {"playing": True}
        if deck_track is not None:
            deck["stable_id"] = deck_track
        with self._lock:
            self._body = {
                "client_id": self.CLIENT_ID,
                "decks": {"1": deck},
                "xrun_sentinel": {"xruns": xruns, "detector": detector},
            }
            self._send_locked()

    def _send_locked(self) -> None:
        assert self._body is not None
        # Stamped under the lock, so the engine never sees an older document after a newer one.
        body = {**self._body, "published_at": datetime.now(UTC).isoformat()}
        _http("PUT", f"{self._engine.base}/state/ui-mirror", body, lease=self.CLIENT_ID)
        self.sent += 1

    def _republish(self) -> None:
        while not self._stop.wait(MIRROR_PUBLISH_S):
            with self._lock:
                if self._body is None:
                    continue
                try:
                    self._send_locked()
                except OSError as exc:
                    self.failures.append(exc)


#-----------------------------------------------------------------------------
# the tests
#-----------------------------------------------------------------------------
def test_an_explicit_weights_override_is_left_unchanged(tmp_path: Path) -> None:
    """[if] MDT_BEATGRID_WEIGHTS is set [then] the test does not replace it, [else stop]."""
    pinned = tmp_path / "pinned.ckpt"
    pinned.write_bytes(b"not-the-real-checkpoint")
    got = host_beatgrid_weights_env(
        environ={beatgrid_weights.WEIGHTS_PATH_ENV: str(pinned)},
        home=tmp_path,
    )
    assert got == {}


def test_an_unpinned_hub_file_is_not_handed_to_the_engine(tmp_path: Path) -> None:
    """[if] the hub file digest is not the pin [then] it is not used, [else stop]."""
    path = tmp_path / ".cache" / "torch" / "hub" / "checkpoints" / beatgrid_weights.CHECKPOINT_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not-the-real-checkpoint")
    got = host_beatgrid_weights_env(environ={}, home=tmp_path)
    assert got == {}


def test_a_fresh_import_gets_beatgrid_before_loudness_covers_the_whole_library(tmp_path: Path) -> None:
    """[if] beatgrid is first enqueued after the LAST loudness item [then] broken, [else stop]."""
    with _live_engine(tmp_path) as engine:
        _end_boot_grace(engine)

        def beatgrid_and_loudness_settled() -> list[tuple[str, str, str, str, str]] | None:
            items = _queue_items(engine.db)
            coverage = _http("GET", f"{engine.base}/ahead-analysis/coverage")
            loudness = coverage.get("lanes", {}).get("loudness")
            if loudness is None:
                return None  # no coverage snapshot yet
            if loudness.get("unavailable"):
                pytest.fail(
                    "UNAVAILABLE: this host cannot produce loudness "
                    f"({loudness['unavailable']}), so NATIVE-25 lane order is unmeasured. "
                    "This is a capability report, not a pass."
                )
            loudness_open = loudness["missing"]
            has_beatgrid = any(lane == "beatgrid" for lane, *_ in items)
            return items if has_beatgrid and loudness_open == 0 else None

        items = _wait_for("a beatgrid item AND loudness on every track", DRAIN_BUDGET_S, beatgrid_and_loudness_settled)

    order = [(lane, at) for lane, _sid, _state, at, _reason in items]
    print("[INFO] queue items in enqueue order:", [lane for lane, _at in order])
    loudness_at = [at for lane, at in order if lane == "loudness"]
    first_beatgrid_at = min(at for lane, at in order if lane == "beatgrid")
    assert loudness_at, "if the drain enqueues no loudness work on a host that can decode then broken"
    assert first_beatgrid_at < max(loudness_at), (
        "if the first beatgrid item is enqueued only after every loudness item then broken: "
        f"first beatgrid {first_beatgrid_at}, last loudness {max(loudness_at)}, "
        f"{len(set(sid for lane, sid, *_ in items if lane == 'loudness'))} tracks of loudness first"
    )


@dataclass(frozen=True)
class WhilePlaying:
    """What the real drain attempted while one deck played."""

    status: str
    deck_track: str
    next_up: str
    others: tuple[str, ...]
    beatgrid_ids: tuple[str, ...]
    kinds: tuple[tuple[str, str], ...]
    forbidden: tuple[str, ...]
    lanes: tuple[str, ...]
    state: str
    record_ids: tuple[str, ...]
    reasons: tuple[tuple[str, str], ...]

    def kind(self, sid: str) -> str | None:
        for item, name in self.kinds:
            if item == sid:
                return name
        return None

    def reason(self, sid: str) -> str:
        for item, text in self.reasons:
            if item == sid:
                return text
        return ""


def _drive_while_playing(tmp_path: Path, *, provision_beatgrid: bool = False) -> WhilePlaying:
    """Play a deck, hold on a blind detector and on an xrun, then let the real drain attempt beatgrid.

    The engine, the queue, and the producer are the shipping ones. With
    ``provision_beatgrid`` false the data dir has no checkpoint, which is the
    host this test asserts UNAVAILABLE for.
    """
    with _live_engine(tmp_path, provision_beatgrid=provision_beatgrid) as engine, MirrorPage(engine) as page:
        deck_track, next_up, *others = engine.ids
        allowed = {deck_track, next_up}

        def state() -> str:
            return str(_http("GET", f"{engine.base}/ahead-analysis/coverage")["state"])

        # 1. a deck plays with no track named, and the xrun baseline is read
        page.publish(deck_track=None, xruns=0)
        _end_boot_grace(engine)
        _wait_for("the drain to pause for playback", 60.0, lambda: state() == "paused_playing")
        # 2. the detector is not armed, THEN AutoPlay's production client names
        #    next-up: the count is not a counter, so nothing may start.
        page.publish(deck_track=None, xruns=0, detector=XRUN_DETECTOR_UNARMED)
        _arm_autoplay_next_up(engine, [next_up])
        _wait_for(
            "the hold for a detector that is not armed",
            30.0,
            lambda: state() == aa.PAUSED_NO_XRUN_COUNTER_STATE,
        )
        time.sleep(aa.PAUSED_INTERVAL_S + 1.0)
        blind = _queue_items(engine.db)
        assert blind == [] and state() == aa.PAUSED_NO_XRUN_COUNTER_STATE, (
            f"if an unarmed xrun detector lets while-playing work run then broken: {state()} {blind}"
        )
        # 3. the detector arms with an xrun spike already counted: still nothing may start
        page.publish(deck_track=None, xruns=3)
        _wait_for("the xrun hold", 30.0, lambda: state() == aa.PAUSED_XRUN_STATE)
        time.sleep(XRUN_QUIET_S)
        held = _queue_items(engine.db)
        assert held == [], f"if a new xrun does not hold while-playing work off then broken: {held}"
        # 4. the hold expires: the next-up track is attempted while the deck still plays.
        #    The page keeps publishing through this wait; a mirror sent once
        #    would be older than aa.MIRROR_MAX_AGE_S long before the hold ends.
        page.publish(deck_track=deck_track, xruns=3)
        sent_before_wait = page.sent
        backend = dict(aa.LANE_ORDER)["beatgrid"]
        observed: dict[str, str] = {}
        attempt_reasons: dict[str, str] = {}

        def settled() -> dict[str, Any] | None:
            items = _queue_items(engine.db)
            beatgrid = [row for row in items if row[0] == "beatgrid"]
            forbidden = tuple(sid for _lane, sid, *_rest in beatgrid if sid not in allowed)
            if forbidden:
                return {"status": "forbidden", "forbidden": forbidden, "items": items, "beatgrid": beatgrid}
            coverage = _http("GET", f"{engine.base}/ahead-analysis/coverage")
            records = _own_record_ids(engine.db, backend)
            for _lane, sid, state, _at, reason in beatgrid:
                if sid in records or state == "done":
                    _note_attempt(observed, attempt_reasons, sid, "record", reason)
                elif _no_checkpoint_unavailable(reason):
                    _note_attempt(observed, attempt_reasons, sid, "unavailable", reason)
                elif state in {"failed", "skipped", "refused"}:
                    _note_attempt(observed, attempt_reasons, sid, "attempted", reason)
            job = coverage.get("last_job") or {}
            if job.get("while_playing") and job.get("lane") == "beatgrid":
                errors = job.get("errors") or {}
                for sid in job.get("ids") or []:
                    if not isinstance(sid, str):
                        continue
                    job_reason = errors.get(sid)
                    text = job_reason if isinstance(job_reason, str) else ""
                    if sid in records:
                        _note_attempt(observed, attempt_reasons, sid, "record", text)
                    elif _no_checkpoint_unavailable(text):
                        _note_attempt(observed, attempt_reasons, sid, "unavailable", text)
                    elif text:
                        _note_attempt(observed, attempt_reasons, sid, "attempted", text)
            # last_job keeps only the newest run. The lane snapshot keeps the
            # no-checkpoint reason after the drain has gone idle. A higher rank
            # replaces an earlier "attempted" that had an empty queue reason.
            if coverage.get("state") == "paused_playing":
                failed = ((coverage.get("lanes") or {}).get("beatgrid") or {}).get("failed_reasons") or {}
                named = [why for why in failed if isinstance(why, str) and _no_checkpoint_unavailable(why)]
                if named:
                    for _lane, sid, _state, _at, _reason in beatgrid:
                        if sid not in records:
                            _note_attempt(observed, attempt_reasons, sid, "unavailable", named[0])
            if coverage.get("state") == "paused_playing" and next_up in observed:
                return {
                    "status": "settled",
                    "items": items,
                    "beatgrid": beatgrid,
                    "coverage": coverage,
                    "records": records,
                    "observed": dict(observed),
                    "reasons": dict(attempt_reasons),
                }
            return None

        found = _wait_for(
            f"a settled beatgrid attempt for next-up {next_up}",
            aa.XRUN_COOLDOWN_S + NEXT_UP_GRID_BUDGET_S,
            settled,
            sleep_s=0.25,
        )
        assert page.sent > sent_before_wait, "the control: the stand-in page kept publishing during the wait"
    beatgrid = found["beatgrid"]
    items = found["items"]
    coverage = found.get("coverage") or {}
    return WhilePlaying(
        status=str(found["status"]),
        deck_track=deck_track,
        next_up=next_up,
        others=tuple(others),
        beatgrid_ids=tuple(sid for _lane, sid, *_rest in beatgrid),
        kinds=tuple(sorted((found.get("observed") or {}).items())),
        forbidden=tuple(found.get("forbidden") or ()),
        lanes=tuple(sorted({lane for lane, *_rest in items})),
        state=str(coverage.get("state", "")),
        record_ids=tuple(sorted(found.get("records") or ())),
        reasons=tuple(sorted((found.get("reasons") or {}).items())),
    )


def _assert_next_up_scheduling(result: WhilePlaying) -> None:
    """Positive: next-up was attempted first. Negative: any other library track was not."""
    assert result.status != "forbidden", (
        "if any track other than the next-up and deck tracks is attempted while playing then broken: "
        f"{list(result.forbidden)}"
    )
    assert result.status == "settled", result.status
    assert result.beatgrid_ids, "if the next-up track gets no beatgrid attempt then broken"
    assert result.beatgrid_ids[0] == result.next_up, (
        f"the next-up track must be the first beatgrid attempt: {result.beatgrid_ids}"
    )
    assert set(result.beatgrid_ids) <= {result.deck_track, result.next_up}, (
        "if any track other than the next-up and deck tracks is attempted while playing then broken: "
        f"{sorted(set(result.beatgrid_ids) - {result.deck_track, result.next_up})}"
    )
    kind = result.kind(result.next_up)
    assert kind in {"record", "unavailable", "attempted"}, (
        f"if the next-up track gets no beatgrid attempt then broken: {kind}"
    )
    if kind == "unavailable":
        assert _no_checkpoint_unavailable(result.reason(result.next_up)), (
            "if a no-checkpoint UNAVAILABLE is treated as not an attempt then broken: "
            f"{result.reason(result.next_up)}"
        )
    assert not (set(result.others) & set(result.beatgrid_ids))
    if result.deck_track in result.beatgrid_ids:
        assert result.beatgrid_ids.index(result.next_up) < result.beatgrid_ids.index(result.deck_track), (
            f"next-up must be attempted before the deck track: {result.beatgrid_ids}"
        )
    assert set(result.lanes) <= set(aa.PLAYING_LANES), f"only beatgrid/key may run while playing: {result.lanes}"
    assert result.state != "green", "the deck still plays, so the library must not have been drained"


@pytest.mark.skip(
    reason="quarantined under #8856: the next-up beatgrid attempt timed out at 360 s on main run 41995165329, green on the next head (Thu 5 Nov 2026)"
)
@pytest.mark.requirement("NATIVE-5886")
def test_while_a_deck_plays_only_the_next_up_track_is_gridded_and_an_xrun_holds_it(tmp_path: Path) -> None:
    """[if] no checkpoint is provisioned [then] next-up beatgrid is attempted UNAVAILABLE, [else stop]."""
    result = _drive_while_playing(tmp_path, provision_beatgrid=False)
    _assert_next_up_scheduling(result)
    assert result.kind(result.next_up) == "unavailable", (
        "if a host with no Beat This! checkpoint does not report UNAVAILABLE then broken: "
        f"{result.kind(result.next_up)} {result.reason(result.next_up)}"
    )
    assert _no_checkpoint_unavailable(result.reason(result.next_up)), (
        "if the beatgrid lane does not name the missing checkpoint then broken: "
        f"{result.reason(result.next_up)}"
    )
    print(
        "[INFO] beatgrid attempts:",
        list(result.beatgrid_ids),
        dict(result.kinds),
        f"next-up={result.kind(result.next_up)}",
        result.reason(result.next_up),
    )


@pytest.mark.requirement("NATIVE-5886")
@pytest.mark.skipif(
    os.environ.get("MDT_BEATGRID_MODEL_TESTS") != "1",
    reason=(
        "needs the Beat This! checkpoint and about a minute of CPU; "
        "set MDT_BEATGRID_MODEL_TESTS=1 to run the real grid"
    ),
)
def test_while_a_deck_plays_the_next_up_track_gets_a_real_beatgrid(tmp_path: Path) -> None:
    """[if] the beatgrid model gate is on [then] next-up gets a real grid, [else stop]."""
    if os.environ.get("MDT_BEATGRID_MODEL_TESTS") != "1":
        pytest.fail("MDT_BEATGRID_MODEL_TESTS=1 is required; this test must not skip once entered")
    result = _drive_while_playing(tmp_path, provision_beatgrid=True)
    _assert_next_up_scheduling(result)
    kind = result.kind(result.next_up)
    reason = result.reason(result.next_up)
    if kind == "unavailable" or _no_checkpoint_unavailable(reason):
        pytest.fail(
            "UNAVAILABLE: MDT_BEATGRID_MODEL_TESTS=1 and this host has no Beat This! "
            f"checkpoint ({reason or kind}). A missing checkpoint is a failure here, "
            "not a pass and not a skip."
        )
    assert kind == "record", (
        "if the model gate is on and the next-up track gets no beatgrid record then broken: "
        f"{kind} {reason}"
    )
    assert result.next_up in result.record_ids
