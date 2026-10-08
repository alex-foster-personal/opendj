#!/usr/bin/env python3
"""Installed-app acceptance: prove an installed Open DJ build really works (OPS-54..OPS-57).

Runs ON the target Mac against an installed ``.app``, in an isolated HOME, and
drives the app only through its own HTTP surface (agent-native parity). Stdlib
only, Python 3.11+ (acceptance.sh checks), so it runs from a copied file with no
environment at all. ``(private path)`` is the
operator entry point (it ships this file and the fixture to a remote Mac);
``ship_dmg.sh`` calls that after an install. ADR: (private path)

Every check is a PRESENCE check with a control, and a check that cannot
measure reports UNKNOWN, which fails the run exactly like FAIL
((private path)). The JSON result file is the contract.

Requirements (statuses per the house docstring convention):

- ✔︎ ✅ OPS-54 isolation: the app runs with HOME pointed at a per-run root, so
  its app data dir (library, state.db, engine logs) is a fresh empty dir; the
  run refuses silver outright, refuses when another instance of the same
  bundle identifier is running (the single-instance plugin would hand our
  launch to it), proves the engine's own reported data_dir is the isolated
  one BEFORE writing anything, and proves afterwards that the real data dir's
  state.db was not modified.
  - [if] the host is silver [then ⛔️] exit 2 before launching anything
  - [if] /setup/status reports a data_dir outside the run root [then ⛔️]
    the app is quit and the run fails before any import
  - [if] the real state.db bytes changed during the run [then ⛔️]
    check `isolation` FAILs
- ✔︎ ✅ OPS-55 checks, each PASS/FAIL/UNKNOWN with numbers:
  manifest_sha, first_run_import, deck_load, audio_plays, stems_lyrics,
  analysis, clean_quit, memory (report-only).
  - [if] the running engine's build-info sha differs from --expect-sha
    [then ⛔️] manifest_sha FAILs
  - [if] master RMS is zero while a deck plays, or non-zero while paused
    [then ⛔️] audio_plays FAILs (UNKNOWN when the paused control is non-zero)
  - [if] a stems job reports success but no bundle exists for the track
    [then ⛔️] stems_lyrics FAILs naming the job id and its duration
  - [if] the pending-scope stems job succeeds, or its failure does not name
    the truncated control [then ⛔️] stems_lyrics FAILs
  - [if] a required stem part is missing or truncated [then ⛔️]
    stems_lyrics FAILs
  - [if] any import job this run did not start is present [then ⛔️]
    first_run_import is UNKNOWN
  - [if] quit takes longer than QUIT_TOTAL_MAX_S, the engine exits slower
    than ENGINE_SIGTERM_EXIT_MAX_MS after SIGTERM, any SIGKILL sweep ran, or a
    process from the bundle or run root survives [then ⛔️] clean_quit FAILs
- ✔︎ ✅ OPS-56 JSON result + exit code: 0 only when every check is PASS; 1 on
  any FAIL/UNKNOWN; 2 when a precondition refused the run.
  - [if] any check is UNKNOWN [then ⛔️] exit 1 and overall FAIL
  - [if] fixture staging fails [then ⛔️] a REFUSED result is written and the process exits 2
- ✔︎ ✅ OPS-57 shell-agnostic: shell specifics (data dir, launch, process
  family, renderer processes, quit log) live behind the Shell class; the
  checks only use HTTP.
- ✔︎ ✅ OPS-60 analysis: after the folder import, with no rekordbox master.db
  under the run HOME and the import job in folder mode, the check drives the
  same HTTP a new user does. GET /tracks/index must answer 200,
  POST /analysis-queue/run must answer 202, and every
  POST /ahead-analysis/bump must answer 200. Another status fails the
  check, and a stored result cannot clear it. When those calls succeed,
  the check reads the stored result. A job
  phase of done is recorded and never a pass. Each known-answer track must
  show BPM in its band, a beatgrid whose first beat is the constructed time,
  the expected key, a waveform, and a finite loudness. The truncated mp3
  must show a per-track error and must not be the reason the others have
  no stored result.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ----- CFG ------------------------------------------------------------------
SCHEMA = "opendj-app-acceptance/v1"
FIXTURE_SCHEMA = "opendj-acceptance-fixture/v1"
BOOT_TIMEOUT_S = 120
IMPORT_TIMEOUT_S = 180
PAGE_TIMEOUT_S = 60
LOAD_TIMEOUT_S = 60
PLAY_SAMPLE_S = 5.0
PLAY_START_MAX_S = 5.0
PAUSE_SILENCE_MAX_S = 5.0
STEMS_TIMEOUT_S = 900
LYRICS_TIMEOUT_S = 600
# All known-answer fixture tracks, plus the corrupt control, must finish
# inside this. The live run records the measured seconds in the check numbers.
# Cold beatgrid on demon-llama (Wed 7 Oct 2026, 7 short tracks) wrote
# the last key at 273 s. 180 s expired during the model load.
ANALYSIS_BUDGET_S = 360
# Success statuses for the three calls that drive analysis. Another status
# fails the check; a stored result cannot clear it.
TRACKS_INDEX_OK = 200
ANALYSIS_QUEUE_RUN_OK = 202
AHEAD_BUMP_OK = 200
QUIT_TOTAL_MAX_S = 10.0
ENGINE_SIGTERM_EXIT_MAX_MS = 5000
DURATION_TOLERANCE_MS = 1500
# A playing deck through the master: measured RMS above this. Paused: below it.
RMS_AUDIBLE_MIN = 1e-3
# (private path), RAM / decoded PCM row: the in-app memory
# meter warns at 800 MB and alarms at 1600 MB. Report-only in v1, no gate.
RSS_WARN_MB = 800
RSS_ALARM_MB = 1600
MEMORY_CYCLES = 3
CLEANUP_SIGTERM_WAIT_S = 30
TERMINAL_LANE_STATES = ("done", "failed", "skipped", "cancelled")
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")
REFUSED_HOST_MARKERS = ("silver",)
WEBKIT_PROCESS_MARKER = "/WebKit.framework/"
ACCEPTANCE_ROOT = Path.home() / ".opendj-acceptance" / "runs"

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


# ----- result model ------------------------------------------------------------
@dataclass
class Check:
    id: str
    verdict: str = UNKNOWN
    summary: str = "not run"
    numbers: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)

    def set(self, verdict: str, summary: str) -> Check:
        self.verdict, self.summary = verdict, summary
        return self


class Refused(Exception):
    """A precondition refused the run before the app was touched (exit 2)."""


class Unmeasurable(Exception):
    """The instrument could not measure; the check is UNKNOWN."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _log(msg: str) -> None:
    print(f"[acceptance {_now()}] {msg}", flush=True)


# ----- HTTP --------------------------------------------------------------------
class Engine:
    """The app's engine over loopback HTTP. Shell-agnostic."""

    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.port = port

    def call(self, method: str, path: str, body: Any = None, timeout: float = 30,
             headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, _json_or_text(raw), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, _json_or_text(err.read()), dict(err.headers or {})
        except (urllib.error.URLError, OSError) as err:
            # Status 0 = no HTTP answer at all (refused, reset, timeout).
            return 0, f"{type(err).__name__}: {err}", {}

    def get_size(self, path: str, timeout: float = 60) -> tuple[int, int]:
        """GET a binary body; returns (status, byte count). Status 0 = no answer."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as resp:
                return resp.status, len(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, 0
        except (urllib.error.URLError, OSError):
            return 0, 0

    def get(self, path: str, timeout: float = 30) -> Any:
        status, body, _ = self.call("GET", path, timeout=timeout)
        if status != 200:
            raise Unmeasurable(f"GET {path} -> {status}: {str(body)[:300]}")
        return body


def _json_or_text(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def _poll(fn: Callable[[], Any], timeout_s: float, interval_s: float = 1.0) -> Any:
    """Return fn()'s first truthy value, or None at the deadline."""
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_s)


# ----- processes ----------------------------------------------------------------
@dataclass
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    command: str


def _ps() -> list[Proc]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,rss=,command="],
                         capture_output=True, text=True, check=True).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            procs.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    if not procs:
        raise Unmeasurable("ps listed no processes")
    return procs


def _descendants(root_pid: int, procs: list[Proc]) -> list[Proc]:
    children: dict[int, list[Proc]] = {}
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc)
    found, stack = [], [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child.pid)
    return found


def _rss_mb(procs: list[Proc]) -> float:
    return round(sum(p.rss_kb for p in procs) / 1024, 1)


def _short_command(command: str) -> str:
    """Basenames of the first tokens: 'python3 stems_local_worker.py --scope'."""
    return " ".join(Path(t).name if "/" in t else t for t in command.split()[:4])


# ----- shell adapter ------------------------------------------------------------
class Shell:
    """Everything that depends on the desktop shell (Tauri today, Electron later).

    The checks never reach past this class into shell internals; a new shell
    implements these methods and every check runs unchanged. Renderer RSS is
    one of them: WKWebView processes live in WebKit.framework, Electron's
    renderers are Chromium helpers, and the memory sample asks the shell.
    """

    def __init__(self, app: Path) -> None:
        self.app = app
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        self.identifier: str = info["CFBundleIdentifier"]
        self.executable: Path = app / "Contents" / "MacOS" / info["CFBundleExecutable"]

    @staticmethod
    def detect(app: Path) -> Shell:
        if (app / "Contents" / "Resources" / "payload" / "manifest.json").is_file():
            return TauriShell(app)
        raise Refused(f"{app}: no known shell layout (Tauri payload manifest missing)")

    def manifest_identity(self) -> dict[str, Any]:
        raise NotImplementedError

    def data_dir(self, home: Path) -> Path:
        raise NotImplementedError

    def launch(self, home: Path) -> None:
        raise NotImplementedError

    def engine_port(self, home: Path) -> int | None:
        raise NotImplementedError

    def engine_pid(self, home: Path) -> int | None:
        raise NotImplementedError

    def quit_log_findings(self, home: Path) -> dict[str, Any]:
        raise NotImplementedError

    def renderer_processes(self, procs: list[Proc], seen_before: set[int]) -> list[Proc]:
        """Page renderers this launch started. Empty means RSS cannot be attributed."""
        raise NotImplementedError


class TauriShell(Shell):
    def manifest_identity(self) -> dict[str, Any]:
        manifest = self.app / "Contents" / "Resources" / "payload" / "manifest.json"
        return json.loads(manifest.read_text())["identity"]

    def data_dir(self, home: Path) -> Path:
        # Tauri's app_data_dir is $HOME/Library/Application Support/<identifier>.
        return home / "Library" / "Application Support" / self.identifier

    def launch(self, home: Path) -> None:
        # Scrubbed environment, same shape as ship_dmg's OPS-32 relaunch; HOME
        # is passed to the app itself through LaunchServices with --env.
        env = {"HOME": str(home), "USER": os.environ.get("USER", ""),
               "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "AF_SERVICE_ID": "com.af.music-dj-tools.acceptance"}
        subprocess.run(["/usr/bin/open", "-n", "-g", "--env", f"HOME={home}",
                        "--env", "AF_SERVICE_ID=com.af.music-dj-tools.acceptance",
                        str(self.app)], env=env, check=True)

    def engine_port(self, home: Path) -> int | None:
        lock = self.data_dir(home) / ".engine.lock"
        try:
            return int(json.loads(lock.read_text())["port"])
        except (OSError, ValueError, KeyError):
            return None

    def engine_pid(self, home: Path) -> int | None:
        lock = self.data_dir(home) / ".engine.lock"
        try:
            return int(json.loads(lock.read_text())["pid"])
        except (OSError, ValueError, KeyError):
            return None

    def quit_log_findings(self, home: Path) -> dict[str, Any]:
        log = self.data_dir(home) / "logs" / "engine.log"
        if not log.is_file():
            raise Unmeasurable(f"no engine log at {log}")
        findings = parse_shutdown_log(log.read_text(errors="replace"))
        if not findings["shutdown_lines"]:
            raise Unmeasurable("engine.log has no [shell shutdown] lines: the shell did not log its quit")
        return findings

    def renderer_processes(self, procs: list[Proc], seen_before: set[int]) -> list[Proc]:
        # WKWebView content, GPU and networking processes live in WebKit.framework.
        return [p for p in procs if WEBKIT_PROCESS_MARKER in p.command and p.pid not in seen_before]


class ElectronShell(Shell):
    """Chromium helpers under apps/desktop/electron, not /WebKit.framework/.

    Shell.detect still returns only TauriShell: launch, data dir and the quit
    log for Electron are not implemented here. The memory sample can already
    attribute this shell's renderers, which is the part that is not WebKit.
    """

    def renderer_processes(self, procs: list[Proc], seen_before: set[int]) -> list[Proc]:
        root = str(self.app) + "/"
        found: list[Proc] = []
        for proc in procs:
            if proc.pid in seen_before or not proc.command.startswith(root):
                continue
            if "--type=renderer" in proc.command or "Helper (Renderer)" in proc.command:
                found.append(proc)
        return found


# ----- run context ---------------------------------------------------------------
@dataclass
class Fixture:
    root: Path
    tracks: list[dict[str, Any]]
    expected_tracks: int
    expected_import_playlists: int
    playlist_name: str
    playlist_titles: list[str]
    deck_title: str
    stems_lyrics_title: str
    lyrics_words: list[str]
    analysis_subjects: list[dict[str, Any]]
    negative_title: str
    negative_file: Path
    downbeat_tolerance_s: float

    def duration_s(self, title: str) -> float:
        return next(t["duration_ms"] for t in self.tracks if t["title"] == title) / 1000

    def subject(self, title: str) -> dict[str, Any] | None:
        return next((s for s in self.analysis_subjects if s["title"] == title), None)

    @staticmethod
    def load(root: Path) -> Fixture:
        manifest = root / "fixture.json"
        try:
            text = manifest.read_text()
        except OSError as exc:
            raise Refused(f"fixture {manifest} unreadable: {exc}") from exc
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as exc:
            raise Refused(f"fixture {manifest} is not valid JSON: {exc}") from exc
        if not isinstance(spec, dict):
            raise Refused(f"fixture {manifest} must be a JSON object, got {type(spec).__name__}")
        if spec.get("schema") != FIXTURE_SCHEMA:
            raise Refused(f"fixture schema {spec.get('schema')!r} is not {FIXTURE_SCHEMA!r}: this harness does not understand it")
        try:
            tracks = spec["tracks"]
            for track in tracks:
                path = root / track["file"]
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as exc:
                    raise Refused(f"fixture {path} unreadable: {exc}") from exc
                if digest != track["sha256"]:
                    raise Refused(f"fixture {track['file']} sha256 {digest[:12]} != recorded {track['sha256'][:12]}: "
                                  "regenerate with make_fixture.sh and record the new digests")
            analysis = spec["analysis"]
            negative = analysis["negative"]
            neg_path = root / negative["file"]
            try:
                neg_digest = hashlib.sha256(neg_path.read_bytes()).hexdigest()
            except OSError as exc:
                raise Refused(f"fixture {neg_path} unreadable: {exc}") from exc
            if neg_digest != negative["sha256"]:
                raise Refused(f"fixture {negative['file']} sha256 {neg_digest[:12]} != recorded "
                              f"{negative['sha256'][:12]}: regenerate with make_fixture.sh")
            titles = {t["title"] for t in tracks}
            subjects = analysis["subjects"]
            missing_subjects = [s["title"] for s in subjects if s["title"] not in titles]
            if missing_subjects or negative["title"] not in titles:
                raise Refused(f"fixture analysis titles are not tracks: {missing_subjects} "
                              f"negative={negative['title']!r}")
            return Fixture(root=root, tracks=tracks,
                           expected_tracks=spec["expected"]["tracks"],
                           expected_import_playlists=spec["expected"]["import_playlists"],
                           playlist_name=spec["playlist"]["name"],
                           playlist_titles=spec["playlist"]["titles"],
                           deck_title=spec["deck_track"], stems_lyrics_title=spec["stems_lyrics_track"],
                           lyrics_words=spec["lyrics_words"],
                           analysis_subjects=subjects, negative_title=negative["title"],
                           negative_file=neg_path,
                           downbeat_tolerance_s=float(analysis["first_downbeat_tolerance_s"]))
        except KeyError as exc:
            raise Refused(f"fixture {manifest} missing field {exc}") from exc
        except TypeError as exc:
            raise Refused(f"fixture {manifest} has a wrong-typed field: {exc}") from exc


@dataclass
class Run:
    shell: Shell
    fixture: Fixture
    home: Path
    expect_sha: str
    stems_timeout_s: float
    lyrics_timeout_s: float
    engine: Engine | None = None
    shell_pid: int | None = None
    pids_before_launch: set[int] = field(default_factory=set)
    ids_by_title: dict[str, str] = field(default_factory=dict)
    our_job_ids: set[str] = field(default_factory=set)
    memory_boot: dict[str, Any] = field(default_factory=dict)
    launched_at: float = 0.0


# ----- helpers --------------------------------------------------------------------
def _host_name() -> str:
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, check=False)
        reported = out.stdout.strip()
    except OSError:
        reported = ""
    return (reported or os.uname().nodename).lower()


def _running_same_identifier(shell: Shell) -> list[str]:
    """Running executables whose bundle carries our identifier (single-instance)."""
    hits = []
    for proc in _ps():
        exe = proc.command.split(" --", 1)[0]
        marker = ".app/Contents/MacOS/"
        if marker not in exe:
            continue
        bundle = Path(exe.split(marker, 1)[0] + ".app")
        try:
            info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        if info.get("CFBundleIdentifier") == shell.identifier:
            hits.append(f"pid {proc.pid}: {bundle}")
    return hits


def _store_file_record(path: Path) -> tuple[str, int] | None:
    """sha256 and size of one real-store file. None when it is absent.

    mtime and size can be restored after an in-place rewrite, so they are
    not the record. Any OSError other than a missing file is unreadable,
    not absent: a guard that treats it as missing can pass.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Unmeasurable(f"real store {path} unreadable: {exc}") from exc
    return digest.hexdigest(), size


def _real_store_stat(shell: Shell) -> dict[str, tuple[str, int] | None]:
    """Content hash of the real state.db AND its WAL sidecars (a WAL commit can leave the main file alone)."""
    state = shell.data_dir(Path.home()) / "state"
    stats: dict[str, tuple[str, int] | None] = {}
    for name in ("state.db", "state.db-wal", "state.db-shm"):
        stats[name] = _store_file_record(state / name)
    return stats


def memory_sample(shell: Shell, procs: list[Proc], *, shell_pid: int | None, engine_pid: int | None,
                  seen_before: set[int]) -> dict[str, Any]:
    """RSS split for one shell. Renderers come from the shell, never a WebKit path."""
    if shell_pid is None:
        raise Unmeasurable("shell pid unknown")
    shell_procs = [p for p in procs if p.pid == shell_pid]
    tree = _descendants(shell_pid, procs)
    renderers = shell.renderer_processes(procs, seen_before)
    if not shell_procs or not tree:
        raise Unmeasurable("shell or engine process not found for RSS sample")
    if not renderers:
        raise Unmeasurable("no renderer process started after launch: renderer RSS unattributable")
    main = [p for p in tree if p.pid == engine_pid]
    workers = [p for p in tree if p.pid != engine_pid]
    return {"shell_mb": _rss_mb(shell_procs), "engine_tree_mb": _rss_mb(tree),
            "engine_main_mb": _rss_mb(main), "engine_workers_mb": _rss_mb(workers),
            "engine_workers": [_short_command(p.command) for p in workers],
            "engine_tree_procs": len(tree),
            "webview_mb": _rss_mb(renderers), "webview_procs": len(renderers),
            "total_mb": _rss_mb(shell_procs + tree + renderers)}


def _memory_sample(run: Run) -> dict[str, Any]:
    return memory_sample(run.shell, _ps(), shell_pid=run.shell_pid,
                         engine_pid=run.shell.engine_pid(run.home), seen_before=run.pids_before_launch)


def _mirror(engine: Engine) -> dict[str, Any] | None:
    status, body, _ = engine.call("GET", "/api/v1/state/ui-mirror", timeout=10)
    return body if status == 200 and isinstance(body, dict) else None


def _deck(mirror: dict[str, Any], deck: str = "1") -> dict[str, Any]:
    return (mirror.get("decks") or {}).get(deck) or {}


def _command(engine: Engine, command: dict[str, Any]) -> tuple[int, Any]:
    status, body, _ = engine.call("POST", "/api/v1/commands", {"single": command}, timeout=60)
    return status, body


def _position_ms(mirror: dict[str, Any]) -> float | None:
    pos = _deck(mirror).get("position") or {}
    value = pos.get("ms") if isinstance(pos, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _jobs(engine: Engine) -> list[dict[str, Any]]:
    body = engine.get("/api/v1/jobs?limit=200")
    return body if isinstance(body, list) else body.get("jobs", [])


def _job_seconds(job: dict[str, Any]) -> float | None:
    try:
        start = datetime.fromisoformat(job["started_at"])
        end = datetime.fromisoformat(job["finished_at"])
        return round((end - start).total_seconds(), 1)
    except (KeyError, TypeError, ValueError):
        return None


# ----- pure verdict rules (unit-tested in (private path)) -----
def parse_shutdown_log(text: str) -> dict[str, Any]:
    """The shell's own quit record: '[shell shutdown]' lines from engine.log."""
    lines = [ln for ln in text.splitlines() if "[shell shutdown]" in ln]
    exit_ms = [int(m.group(1)) for ln in lines if (m := re.search(r"exited (\d+)ms after SIGTERM", ln))]
    return {"shutdown_lines": lines[-12:], "engine_exit_ms_after_sigterm": exit_ms,
            "sigkill_lines": [ln for ln in lines if "SIGKILL" in ln]}


def quit_problems(quit_s: float, survivors: int, exit_ms: list[int], sigkill_lines: list[str],
                  port_open: bool) -> list[str] | None:
    """Problems with a quit, [] when clean, None when the exit time is unmeasured."""
    if not exit_ms:
        return None
    problems = []
    if survivors:
        problems.append(f"{survivors} process(es) still alive after quit")
    if quit_s > QUIT_TOTAL_MAX_S:
        problems.append(f"quit took {quit_s}s > {QUIT_TOTAL_MAX_S}s")
    if exit_ms[-1] > ENGINE_SIGTERM_EXIT_MAX_MS:
        problems.append(f"engine exited {exit_ms[-1]} ms after SIGTERM > {ENGINE_SIGTERM_EXIT_MAX_MS} ms")
    if sigkill_lines:
        problems.append(f"shell had to SIGKILL: {sigkill_lines[-1][:200]}")
    if port_open:
        problems.append("engine port still listening after quit")
    return problems


def pending_stems_problem(job_id: str | None, status: str | None, seconds: float | None,
                          unstemmed: list[str], total: int, error: Any = None,
                          control_id: str | None = None) -> str | None:
    """The pending-scope job must fail on the truncated file, and every other track needs a bundle.

    scope=pending selects the truncated control. The local worker records that
    decode failure and exits nonzero. A succeeded job never ran that rejection.
    A failed job passes only when its error names that control's stable id.
    Any other failure is a different fault. A failure that left fixture tracks
    unstemmed is still a failure.
    """
    if not job_id:
        return "stems (whole library): the stems job was refused"
    if status not in TERMINAL_JOB_STATES:
        return f"stems (whole library): job {job_id} still {status} at the deadline; unstemmed {unstemmed}"
    if unstemmed:
        return (f"stems (whole library): job {job_id} {status} in {seconds}s but "
                f"{len(unstemmed)} of {total} fixture tracks have no real stem bundle: {unstemmed}")
    if status == "failed":
        text = error if isinstance(error, str) else "" if error is None else str(error)
        if isinstance(control_id, str) and control_id and control_id in text:
            return None
        return (f"stems (whole library): job {job_id} failed in {seconds}s but the error does not "
                f"name the truncated control {control_id!r}: {text[:200]}")
    if status == "succeeded":
        return (f"stems (whole library): job {job_id} succeeded in {seconds}s but the "
                f"truncated control did not fail the pending-scope job")
    return f"stems (whole library): job {job_id} {status}: {str(error)[:200]}"


def lyrics_problem(lyrics_present: bool, item: dict[str, Any], stems_real: bool,
                   matched_words: list[str] | None = None) -> str | None:
    """Lyrics judged by what GET /lyrics serves, then by how honestly the lane explains a miss.

    Served lyrics must contain at least one word the fixture actually speaks: a
    title collision serving some other song's text is not evidence.
    """
    if lyrics_present:
        if matched_words:
            return None
        return "lyrics: served text contains none of the fixture's spoken words (an unrelated match)"
    state, reason = item.get("state"), item.get("reason")
    # A private fixture is not on LRCLIB, and ASR transcripts come from a
    # CloudSync hub a new user does not have. The honest end of a real run
    # is no_lyrics_found. up_to_date with no lines is the bug: the job never ran.
    if state == "done" and reason == "no_lyrics_found" and item.get("started_at"):
        return None
    if state == "skipped" and reason == "up_to_date":
        return "lyrics: skipped, reason up_to_date"
    if state == "done" and not reason:
        return "lyrics: the lane reported done with no reason but GET /lyrics serves nothing"
    if reason == "no_source" and stems_real:
        return ("lyrics: the lane settled no_source ('track is not in the local stem bundle index') "
                f"although this track's stem bundle exists and streams: lyrics ran {item.get('started_at')}")
    if state in TERMINAL_LANE_STATES:
        return f"lyrics: not available in this build for a track without a public match: state={state} reason={reason}"
    return f"lyrics: no lyrics and no terminal state by the deadline (state={state})"


def host_refused(local_host_name: str) -> bool:
    return any(marker in local_host_name.lower() for marker in REFUSED_HOST_MARKERS)


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def bpm_problem(title: str, observed: Any, low: float, high: float) -> str | None:
    """BPM lane. A job phase is not an input: done with no number fails."""
    if not _finite_number(observed) or not (low <= float(observed) <= high):
        return f"analysis: bpm: {title}: observed {observed}, expected {low}..{high}"
    return None


def key_problem(title: str, observed: Any, accept: list[str]) -> str | None:
    """Key lane. ``accept`` is the app's own spellings of the constructed key."""
    if not accept:
        return None
    text = observed.strip() if isinstance(observed, str) else None
    if text not in accept:
        return f"analysis: key: {title}: observed {observed!r}, expected one of {accept}"
    return None


def beatgrid_problem(title: str, beats: Any, expected_first_s: float | None,
                     tolerance_s: float) -> str | None:
    """Beatgrid lane: a grid exists and its first beat time is the constructed one."""
    if not isinstance(beats, list) or not beats or not isinstance(beats[0], dict):
        return f"analysis: beatgrid: {title}: observed {beats!r}, expected a beat list with a first time"
    first = beats[0].get("t")
    if not _finite_number(first):
        return f"analysis: beatgrid: {title}: first beat time observed {first!r}, expected a number"
    if expected_first_s is not None and abs(float(first) - expected_first_s) > tolerance_s:
        return (f"analysis: beatgrid: {title}: first beat {first} s, "
                f"expected {expected_first_s} +/- {tolerance_s} s")
    return None


def waveform_problem(title: str, preview_points: Any, local_bytes: Any, own_status: Any) -> str | None:
    """Waveform lane. Any one stored shape is enough; a status of done is not."""
    points = preview_points if isinstance(preview_points, int) and preview_points > 0 else 0
    local = local_bytes if isinstance(local_bytes, int) and local_bytes > 0 else 0
    if points or local or own_status == "ok":
        return None
    return (f"analysis: waveform: {title}: observed preview_points={preview_points} "
            f"local_bytes={local_bytes} own_status={own_status!r}")


def loudness_problem(title: str, status: Any, value: Any) -> str | None:
    """Loudness lane. The app projects loudness_lufs; a missing value fails."""
    if status == "ok" and _finite_number(value):
        return None
    return f"analysis: loudness: {title}: observed status={status!r} value={value!r}, expected a finite LUFS"


def track_analysis_problems(title: str, observed: dict[str, Any], subject: dict[str, Any],
                            tolerance_s: float) -> list[str]:
    """Every lane of one known-answer track. Ignores job phase on purpose."""
    problems: list[str] = []
    if subject.get("bpm_min") is not None and subject.get("bpm_max") is not None:
        found = bpm_problem(title, observed.get("bpm"), float(subject["bpm_min"]), float(subject["bpm_max"]))
        if found:
            problems.append(found)
    # A subject names a grid only when it names a downbeat or a BPM. A
    # sustained chord has a key and no pulse; too_few_beats is the true result.
    if subject.get("first_downbeat_s") is not None or subject.get("bpm_min") is not None:
        found = beatgrid_problem(title, observed.get("beats"), subject.get("first_downbeat_s"), tolerance_s)
        if found:
            problems.append(found)
    found = key_problem(title, observed.get("key"), list(subject.get("key_accept") or []))
    if found:
        problems.append(found)
    found = waveform_problem(title, observed.get("preview_points"), observed.get("local_bytes"),
                             observed.get("waveform_status"))
    if found:
        problems.append(found)
    found = loudness_problem(title, observed.get("loudness_status"), observed.get("loudness_value"))
    if found:
        problems.append(found)
    return problems


def corrupt_error_visible(observed: dict[str, Any]) -> bool:
    """A per-track failure the UI can show. A job phase of done or error is not one."""
    lanes = (
        ("bpm_status", "bpm_reason"),
        ("key_status", "key_reason"),
        ("loudness_status", "loudness_reason"),
        ("beatgrid_status", "beatgrid_reason"),
        ("waveform_status", "waveform_reason"),
        ("local_status", "local_reason"),
    )
    for status_key, reason_key in lanes:
        if observed.get(status_key) in ("failed", "error") and observed.get(reason_key):
            return True
    # The deck's own waveform field says not_decoded, with the decoder's reason.
    # A retryable miss is the decoder being busy, not a fact about this file.
    if (observed.get("local_status") == "not_decoded" and observed.get("local_reason")
            and observed.get("local_retryable") is not True):
        return True
    needle = observed.get("needle") or ""
    for line in observed.get("log_lines") or []:
        low = str(line).lower()
        if needle and needle in str(line) and any(word in low for word in ("error", "fail", "unreadable", "invalid")):
            return True
    return False


def corrupt_observation_problem(title: str, observed: dict[str, Any]) -> str | None:
    """The negative control: never a stored success, then a visible per-track error.

    A lane error does not excuse a BPM, key, or beat list stored beside it.
    Reject those first. Only a control with no stored success, and a visible
    error, passes.
    """
    if observed.get("bpm") is not None or observed.get("beats") or observed.get("key"):
        return (f"analysis: negative: {title}: stored a result instead of a per-track error "
                f"(bpm={observed.get('bpm')} key={observed.get('key')!r} "
                f"beats={len(observed.get('beats') or [])} beatgrid={observed.get('beatgrid_status')!r})")
    if corrupt_error_visible(observed):
        return None
    return (f"analysis: negative: {title}: no visible per-track error "
            f"(bpm_status={observed.get('bpm_status')!r} beatgrid={observed.get('beatgrid_status')!r} "
            f"reason={observed.get('beatgrid_reason') or observed.get('local_reason')!r})")


def analysis_blocked_problem(negative_title: str, missing_titles: list[str],
                             job_phase: Any, job_error: Any) -> str | None:
    """The corrupt file must not be why every other track has no stored result."""
    if not missing_titles or job_phase != "error":
        return None
    return (f"analysis: negative: {negative_title}: blocked the other tracks, "
            f"which have no stored result ({missing_titles}); job error {str(job_error)[:200]}")


# ----- checks --------------------------------------------------------------------
def check_manifest_sha(run: Run) -> Check:
    check = Check("manifest_sha")
    identity = run.shell.manifest_identity()
    installed = identity.get("git_sha_full", "")
    assert run.engine is not None
    running = run.engine.get("/api/v1/build-info")
    running_sha = running.get("git_sha_full") or running.get("identity", {}).get("git_sha_full") or ""
    check.numbers = {"expected": run.expect_sha, "installed_manifest": installed,
                     "running_engine": running_sha, "running_source": running.get("source")}
    if not installed or not running_sha:
        return check.set(UNKNOWN, "manifest or running build-info carries no full sha")
    if installed == run.expect_sha and running_sha == run.expect_sha:
        return check.set(PASS, f"installed and running sha both {installed[:12]}")
    return check.set(FAIL, f"sha mismatch: expected {run.expect_sha[:12]}, installed {installed[:12]}, running {running_sha[:12]}")


def _rows(body: Any) -> list[dict[str, Any]]:
    """A list endpoint's rows, whether it answers a bare list or an envelope."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        rows = body.get("tracks", body.get("items", []))
        return rows if isinstance(rows, list) else []
    return []


def _import_fixture(run: Run, check: Check) -> str | None:
    """Run the folder import and wait for its job. Returns a FAIL reason or None."""
    engine = run.engine
    assert engine is not None
    library = run.home / "fixture-library"
    code, job, _ = engine.call("POST", "/api/v1/setup/import/folder", {"folders": [str(library)]})
    if code != 202:
        return f"folder import refused: {code} {str(job)[:300]}"
    run.our_job_ids.add(job["id"])
    t0 = time.monotonic()

    def finished() -> dict[str, Any] | None:
        current = engine.get(f"/api/v1/jobs/{job['id']}")
        return current if current.get("status") in TERMINAL_JOB_STATES else None

    done = _poll(finished, IMPORT_TIMEOUT_S)
    check.numbers["import_seconds"] = round(time.monotonic() - t0, 1)
    if done is None:
        return f"import job {job['id']} not finished after {IMPORT_TIMEOUT_S}s"
    if done["status"] != "succeeded":
        return f"import job {done['status']}: {done.get('error')}"
    return None


def _playlist_roundtrip(run: Run, check: Check) -> str | None:
    """Write the fixture playlist through the app's own API and read it back.

    A folder import has no playlist format to read, so this is the playlist
    half of the first-run check. Returns a FAIL reason or None.
    """
    engine = run.engine
    assert engine is not None
    code, created, headers = engine.call("POST", "/api/v1/playlists", {"name": run.fixture.playlist_name})
    if code != 201:
        return f"playlist create refused: {code} {str(created)[:200]}"
    pid = created["playlist_id"]
    etag = headers.get("ETag") or headers.get("etag") or created.get("etag")
    ids = [run.ids_by_title[t] for t in run.fixture.playlist_titles]
    code, filled, _ = engine.call("PUT", f"/api/v1/playlists/{pid}/tracks", {"stable_ids": ids},
                                  headers={"If-Match": str(etag)} if etag else None)
    if code != 200:
        return f"playlist fill refused: {code} {str(filled)[:200]}"
    after = engine.get("/api/v1/health").get("state_db") or {}
    readback = [r.get("stable_id") for r in _rows(engine.get(f"/api/v1/playlists/{pid}/tracks"))]
    check.numbers.update({"playlists_after_create": after.get("playlists"),
                          "playlist_tracks_readback": len(readback), "expected_playlist_tracks": len(ids)})
    if after.get("playlists") != run.fixture.expected_import_playlists + 1 or readback != ids:
        return (f"playlist readback {after.get('playlists')} playlists, members {readback}; "
                f"expected 1 playlist with members {ids} in that order")
    return None


def import_problem(fixture: Fixture, state: dict[str, Any], titles: set[str]) -> str | None:
    """The imported library against the fixture's own numbers."""
    expected = fixture.expected_tracks
    if state.get("tracks") != expected or state.get("tracks_playable") != expected:
        return f"tracks={state.get('tracks')} playable={state.get('tracks_playable')}, fixture has {expected}"
    missing = sorted({t["title"] for t in fixture.tracks} - titles)
    if missing:
        return f"fixture titles missing from /tracks: {missing}"
    if state.get("playlists") != fixture.expected_import_playlists:
        return f"import created playlists={state.get('playlists')}, fixture expects {fixture.expected_import_playlists}"
    return None


def foreign_import_jobs(jobs: list[dict[str, Any]], our_job_ids: set[str]) -> list[dict[str, Any]]:
    """Every import job this harness did not start, whatever its kind.

    Rekordbox import and folder import share setup.import-rekordbox today.
    Another process can still enqueue a different kind whose name contains
    import, change the isolated library, and walk past a guard that matches
    only that one kind.
    """
    foreign: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        kind = job.get("kind")
        if not isinstance(kind, str) or "import" not in kind.casefold():
            continue
        if job.get("id") in our_job_ids:
            continue
        foreign.append(job)
    return foreign


def check_first_run_import(run: Run) -> Check:
    check = Check("first_run_import")
    engine = run.engine
    assert engine is not None
    status = engine.get("/api/v1/setup/status")
    check.numbers.update({"fresh_library_empty": status.get("library_empty"),
                          "fresh_should_show_wizard": status.get("should_show_wizard"),
                          "fresh_tracks": status.get("tracks")})
    if status.get("library_empty") is not True or status.get("tracks") != 0:
        return check.set(FAIL, f"fresh data dir is not an empty first run: tracks={status.get('tracks')}")
    if status.get("should_show_wizard") is not True:
        return check.set(FAIL, "first run on an empty data dir does not offer the setup wizard (should_show_wizard false)")
    failure = _import_fixture(run, check)
    if failure:
        return check.set(FAIL, failure)
    state = engine.get("/api/v1/health").get("state_db") or {}
    rows = _rows(engine.get("/api/v1/tracks?limit=500"))
    run.ids_by_title = {str(r["title"]): str(r["stable_id"]) for r in rows if r.get("stable_id") and r.get("title")}
    check.numbers.update({"tracks": state.get("tracks"), "tracks_playable": state.get("tracks_playable"),
                          "import_playlists": state.get("playlists"),
                          "expected_tracks": run.fixture.expected_tracks,
                          "expected_import_playlists": run.fixture.expected_import_playlists,
                          "titles_listed": sorted(t for t in run.ids_by_title if t)})
    foreign = foreign_import_jobs(_jobs(engine), run.our_job_ids)
    if foreign:
        for job in foreign:
            payload = job.get("payload")
            roots = payload.get("roots") if isinstance(payload, dict) else None
            check.evidence.append(
                f"foreign import job {job.get('id')} kind={job.get('kind')} roots={roots}")
        return check.set(UNKNOWN, f"{len(foreign)} import job(s) this run did not start: someone else drove the isolated app")
    failure = import_problem(run.fixture, state, set(run.ids_by_title)) or _playlist_roundtrip(run, check)
    if failure:
        return check.set(FAIL, failure)
    return check.set(PASS, f"{state.get('tracks')}/{run.fixture.expected_tracks} tracks imported in "
                           f"{check.numbers['import_seconds']}s, playlist round-trips "
                           f"{check.numbers['playlist_tracks_readback']} tracks")


def _open_performance(run: Run, check: Check) -> dict[str, Any] | None:
    engine = run.engine
    assert engine is not None
    code, body, _ = engine.call("POST", "/api/v1/shell/navigate", {"route": "/performance"})
    if code != 202:
        raise Unmeasurable(f"shell navigate refused: {code} {str(body)[:200]}")

    def page_ready() -> dict[str, Any] | None:
        mirror = _mirror(engine)
        lease = engine.call("GET", "/api/v1/state/ui-mirror/lease", timeout=10)[1]
        return mirror if mirror and isinstance(lease, dict) and lease.get("held") else None

    t0 = time.monotonic()
    mirror = _poll(page_ready, PAGE_TIMEOUT_S)
    check.numbers["performance_page_s"] = round(time.monotonic() - t0, 1)
    return mirror


def check_deck_load(run: Run) -> Check:
    check = Check("deck_load")
    engine = run.engine
    assert engine is not None
    if _open_performance(run, check) is None:
        return check.set(UNKNOWN, f"performance page never published a mirror lease within {PAGE_TIMEOUT_S}s")
    track_id = run.ids_by_title.get(run.fixture.deck_title)
    if not track_id:
        return check.set(UNKNOWN, f"fixture deck track {run.fixture.deck_title!r} not in library")
    t0 = time.monotonic()
    code, body = _command(engine, {"type": "load", "deck": 1, "stable_id": track_id})
    check.numbers["load_command_status"] = code
    if code != 200:
        return check.set(FAIL, f"load command -> {code}: {str(body)[:300]}")
    loaded = _poll(lambda: (m := _mirror(engine)) and _deck(m).get("stable_id") == track_id and m, LOAD_TIMEOUT_S, 0.25)
    check.numbers["load_to_mirror_s"] = round(time.monotonic() - t0, 2)
    if not loaded:
        return check.set(FAIL, f"deck 1 never reported {track_id} loaded within {LOAD_TIMEOUT_S}s")
    duration = engine.get(f"/api/v1/tracks/{track_id}").get("duration_ms")
    expected_ms = run.fixture.duration_s(run.fixture.deck_title) * 1000
    anlz = engine.get(f"/api/v1/tracks/{track_id}/anlz?points=200", timeout=60)
    preview = (anlz.get("waveform") or {}).get("preview") or {}
    local = anlz.get("local_waveform") or {}
    preview_len = preview.get("length") or len(preview.get("mid") or [])
    local_b64 = local.get("preview_b64") or ""
    local_bytes = len(base64.b64decode(local_b64)) if local_b64 else 0
    check.numbers.update({"duration_ms": duration, "expected_duration_ms": expected_ms,
                          "waveform_kind": (anlz.get("waveform") or {}).get("kind"),
                          "waveform_preview_points": preview_len, "local_waveform_status": local.get("status"),
                          "local_waveform_bytes": local_bytes, "local_waveform_reason": local.get("reason")})
    if not isinstance(duration, (int, float)) or abs(duration - expected_ms) > DURATION_TOLERANCE_MS:
        return check.set(FAIL, f"duration {duration} ms, fixture {expected_ms} ms")
    if not preview_len and not local_bytes:
        return check.set(FAIL, f"no waveform for the loaded track (local_waveform {local.get('status')}: {local.get('reason')})")
    return check.set(PASS, f"deck 1 loaded {run.fixture.deck_title!r} in {check.numbers['load_to_mirror_s']}s, "
                           f"duration {duration} ms, waveform present")


def audio_verdict(moving: bool, silent: bool, playing: dict[str, Any], paused: dict[str, Any],
                  device_ok: bool) -> tuple[str, str]:
    """Playback judged by the page's own meter, with the paused deck as the negative control."""
    if (paused.get("rms_max") is None or playing.get("rms_max") is None
            or paused.get("fresh_samples", 0) <= 0 or playing.get("fresh_samples", 0) <= 0):
        return UNKNOWN, "no fresh meter reading: the output level is unmeasurable"
    paused_moved = (paused["position_last"] or 0) - (paused["position_first"] or 0)
    if not silent and paused_moved <= 0 and paused["rms_max"] >= RMS_AUDIBLE_MIN:
        return UNKNOWN, (f"paused deck is frozen but the meter still reads a FRESH rms {paused['rms_max']:.4f}: "
                         "the meter does not read zero when silent")
    if not silent:
        return FAIL, (f"pause does not silence the output within {PAUSE_SILENCE_MAX_S}s: playhead moved "
                      f"{paused_moved:.0f} ms and rms {paused['rms_max']:.4f} in the 2s after")
    advanced = (playing["position_last"] or 0) - (playing["position_first"] or 0)
    if not playing["playing_seen"] or not moving:
        return FAIL, f"deck never moved within {PLAY_START_MAX_S}s of the play command"
    if advanced < 1000:
        return FAIL, f"playhead advanced only {advanced:.0f} ms in {PLAY_SAMPLE_S}s"
    if playing["rms_max"] < RMS_AUDIBLE_MIN:
        return FAIL, (f"deck plays (playhead +{advanced:.0f} ms) but the meter max is {playing['rms_max']} "
                      f"< {RMS_AUDIBLE_MIN}: silent output ({playing['fresh_samples']} fresh samples)")
    if not device_ok:
        return FAIL, f"playback is audible (rms max {playing['rms_max']:.4f}) but the output-health surface is broken"
    return PASS, f"rms max {playing['rms_max']:.4f} while playing (+{advanced:.0f} ms), silent after pause"


def check_audio_plays(run: Run) -> Check:
    check = Check("audio_plays")
    engine = run.engine
    assert engine is not None
    code, health, _ = engine.call("GET", "/api/v1/audio/output-health", timeout=15)
    if not isinstance(health, dict):
        return check.set(UNKNOWN, f"output-health -> {code}: {str(health)[:200]}")
    device_ok = health.get("verdict") == "ok" and health.get("device_delivering") is True
    check.numbers.update({"output_health_status": code, "output_health": health,
                          "output_device": health.get("default_device_name"), "device_ok": device_ok})
    if not device_ok:
        check.defects.append(f"output-health cannot measure the device: HTTP {code} "
                             f"{health.get('verdict')} {health.get('reason') or str(health)[:200]}")
    mirror = _mirror(engine)
    if not mirror or not _deck(mirror).get("stable_id"):
        return check.set(UNKNOWN, "no loaded deck to play (deck_load did not pass)")
    start_pos = _position_ms(mirror) or 0.0
    t0 = time.monotonic()
    code, body = _command(engine, {"type": "play", "deck": 1, "playing": True})
    if code != 200:
        return check.set(FAIL, f"play command -> {code}: {str(body)[:300]}")
    moving = _poll(lambda: (m := _mirror(engine)) and (_position_ms(m) or 0) > start_pos + 200, PLAY_START_MAX_S, 0.1)
    check.numbers["play_to_motion_s"] = round(time.monotonic() - t0, 2) if moving else None
    playing = _sample(engine, PLAY_SAMPLE_S)
    t1 = time.monotonic()
    code, body = _command(engine, {"type": "play", "deck": 1, "playing": False})
    if code != 200:
        return check.set(UNKNOWN, f"pause command -> {code}: the negative control could not run")
    silent = _poll(lambda: _is_silent(engine), PAUSE_SILENCE_MAX_S, 0.25)
    check.numbers["pause_to_silence_s"] = round(time.monotonic() - t1, 2) if silent else None
    paused = _sample(engine, 2.0)
    check.numbers.update({"playing": playing, "paused": paused, "rms_audible_min": RMS_AUDIBLE_MIN,
                          "context_state": (_mirror(engine) or {}).get("context_state")})
    verdict, summary = audio_verdict(bool(moving), bool(silent), playing, paused, device_ok)
    if verdict != PASS:
        check.defects.append(summary)
    elif verdict == PASS:
        summary += (f", motion {check.numbers['play_to_motion_s']}s after play, silent "
                    f"{check.numbers['pause_to_silence_s']}s after pause, device {health.get('default_device_name')!r}")
    return check.set(verdict, summary)


def _is_silent(engine: Engine) -> bool:
    """Paused means a fresh meter under the audible line and a frozen playhead."""
    first = _mirror(engine)
    if not first:
        return False
    if "received_at" not in first:
        raise Unmeasurable("ui-mirror carries no received_at: two publishes cannot be told apart")
    # The page publishes about once a second: compare two DIFFERENT publishes,
    # or an unchanged document reads as a frozen playhead.
    second = _poll(lambda: (m := _mirror(engine)) and m.get("received_at") != first.get("received_at") and m, 3.0, 0.2)
    if not second:
        return False
    return pause_meter_is_silent(_meter(second), _position_ms(first) == _position_ms(second))


def pause_meter_is_silent(meter: dict[str, Any] | None, frozen: bool) -> bool:
    """Silence is a fresh meter under the audible line and a frozen playhead.

    A stale meter keeps its last value. That is not a measurement of silence.
    """
    if not frozen or not isinstance(meter, dict):
        return False
    rms = meter.get("rms")
    if meter.get("fresh") is not True or isinstance(rms, bool) or not isinstance(rms, (int, float)):
        return False
    return rms < RMS_AUDIBLE_MIN


def fresh_meter_rms(levels: list[float]) -> float | None:
    """The highest fresh meter level. No fresh sample is unmeasured, not zero."""
    if not levels:
        return None
    return max(levels)


def _meter(mirror: dict[str, Any]) -> dict[str, Any] | None:
    meter = (mirror.get("audio_health") or {}).get("meter")
    return meter if isinstance(meter, dict) else None


def _sample(engine: Engine, seconds: float) -> dict[str, Any]:
    """Sample the page's own output meter (audio_health.meter: rms + age + fresh).

    A level only counts when the meter calls it FRESH: the meter keeps its
    last value when sampling stops, so a stale rms is not a current level.
    """
    levels, stale, positions, playing_seen, audible_seen, meters = [], 0, [], False, False, 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        mirror = _mirror(engine)
        if mirror:
            meter = _meter(mirror)
            if meter is not None:
                meters += 1
                if meter.get("fresh") is True and isinstance(meter.get("rms"), (int, float)):
                    levels.append(float(meter["rms"]))
                else:
                    stale += 1
            pos = _position_ms(mirror)
            if pos is not None:
                positions.append(pos)
            playing_seen |= bool(_deck(mirror).get("playing"))
            audible_seen |= bool(_deck(mirror).get("audible"))
        time.sleep(0.25)
    return {"meter_samples": meters, "fresh_samples": len(levels), "stale_samples": stale,
            "rms_max": fresh_meter_rms(levels),
            "rms_nonzero_samples": sum(1 for v in levels if v >= RMS_AUDIBLE_MIN),
            "position_first": positions[0] if positions else None,
            "position_last": positions[-1] if positions else None,
            "playing_seen": playing_seen, "audible_seen": audible_seen}


def check_memory(run: Run) -> Check:
    check = Check("memory")
    engine = run.engine
    assert engine is not None
    check.numbers["boot"] = run.memory_boot
    ids = [run.ids_by_title[t["title"]] for t in run.fixture.tracks if t["title"] in run.ids_by_title]
    for cycle in range(MEMORY_CYCLES):
        track_id = ids[cycle % len(ids)]
        for command in ({"type": "load", "deck": 1, "stable_id": track_id}, {"type": "unload", "deck": 1}):
            code, body = _command(engine, command)
            if code != 200:
                return check.set(UNKNOWN, f"cycle {cycle + 1} {command['type']} -> {code}: {str(body)[:200]}")
    time.sleep(2)
    after = _memory_sample(run)
    check.numbers["after_cycles"] = after
    check.numbers["cycles"] = MEMORY_CYCLES
    check.numbers["thresholds_mb"] = {"warn": RSS_WARN_MB, "alarm": RSS_ALARM_MB,
                                      "source": "(private path) RAM / decoded PCM"}
    check.numbers["delta_total_mb"] = round(after["total_mb"] - run.memory_boot["total_mb"], 1)
    over = [k for k in ("engine_tree_mb", "webview_mb") if after[k] >= RSS_WARN_MB]
    note = f"; over the {RSS_WARN_MB} MB meter warn line: {over}" if over else ""
    return check.set(PASS, f"report-only: boot {run.memory_boot['total_mb']} MB -> {after['total_mb']} MB after "
                           f"{MEMORY_CYCLES} load/eject cycles (engine tree {after['engine_tree_mb']}, of which workers "
                           f"{after['engine_workers_mb']}; webview {after['webview_mb']}){note}")


def _enqueue(engine: Engine, lane: str, track_id: str) -> tuple[int, Any]:
    code, body, _ = engine.call("POST", "/api/v1/library-jobs/enqueue",
                                {"lane": lane, "stable_ids": [track_id]})
    return code, body


REQUIRED_STEM_PARTS = ("vocals", "drums", "bass", "other")
MIN_STEM_PART_BYTES = 10_000


def stem_bundle_is_real(parts: list[str], seconds: float, min_seconds: float,
                        part_responses: dict[str, tuple[int, int]]) -> bool:
    """Each required part must stream, and the shared frames must cover the track.

    The manifest publishes one frame count. A bundle can name four parts and
    still hide a missing or truncated drums, bass, or other file when only
    the vocals body is read. Each part's own response is the coverage check
    the manifest cannot make: HTTP 200 and more than MIN_STEM_PART_BYTES.
    """
    if any(name not in parts for name in REQUIRED_STEM_PARTS):
        return False
    if seconds < 0.9 * min_seconds:
        return False
    for name in REQUIRED_STEM_PARTS:
        code, size = part_responses.get(name, (0, 0))
        if code != 200 or size <= MIN_STEM_PART_BYTES:
            return False
    return True


def _stem_bundle(engine: Engine, track_id: str, min_seconds: float) -> dict[str, Any] | None:
    """Presence of real stem output for one track, never a job status.

    Real = the track's stem state is local, the manifest names vocals, drums,
    bass and other, the shared frame count covers the track, and each of
    those parts streams its own body.
    """
    body = engine.call("GET", f"/api/v1/tracks/{track_id}/stems")[1]
    state = engine.call("GET", f"/api/v1/tracks/{track_id}/stems/state")[1]
    if not (isinstance(body, dict) and isinstance(body.get("parts"), dict)
            and isinstance(state, dict) and state.get("state") == "local"):
        return None
    parts = sorted(body["parts"])
    seconds = body.get("frame_count", 0) / max(body.get("sample_rate_hz", 1), 1)
    part_responses = {
        name: engine.get_size(f"/api/v1/tracks/{track_id}/stems/{name}")
        for name in REQUIRED_STEM_PARTS
    }
    vocal_code, vocal_size = part_responses["vocals"]
    return {"parts": parts, "seconds": round(seconds, 1), "model": body.get("model"),
            "part_responses": {name: {"status": code, "bytes": size}
                               for name, (code, size) in part_responses.items()},
            "probe_part": "vocals", "probe_part_status": vocal_code, "probe_part_bytes": vocal_size,
            "real": stem_bundle_is_real(parts, seconds, min_seconds, part_responses)}


def pending_scope_stems_payload() -> dict[str, Any]:
    """The whole-library stems job: every pending file, including the truncated control.

    Naming stable ids and leaving scope unset lets the job succeed without
    ever decoding the corrupt file. scope=pending is resolved by the worker
    after that file's bytes have been replaced, so the failure is part of
    the run. An explicit id list is the omission this check exists to reject.
    """
    return {"scope": "pending", "tier": "LOCAL"}


def _enqueue_record(code: int, body: Any) -> dict[str, Any]:
    """The part of an enqueue response that proves a job was created."""
    record: dict[str, Any] = {"status": code, "stable_ids": [], "already_running": []}
    if not isinstance(body, dict):
        return record
    items = body.get("items")
    if isinstance(items, list):
        record["stable_ids"] = [
            item["stable_id"] for item in items
            if isinstance(item, dict) and isinstance(item.get("stable_id"), str) and item["stable_id"]
        ]
    running = body.get("already_running")
    if isinstance(running, list):
        record["already_running"] = [item for item in running if isinstance(item, str) and item]
    return record


def stems_start_problem(numbers: dict[str, Any], evidence: list[str], track_id: str) -> str | None:
    """A start that threw, or a required job that was never created, is a failure.

    The later bundle check cannot pass this. A per-track lane can report a
    real bundle from some other path while this start never created its job.
    """
    start_error = numbers.get("start_error")
    if isinstance(start_error, str) and start_error:
        return f"stems_lyrics: start failed: {start_error}"
    for line in evidence:
        if isinstance(line, str) and line.startswith("enqueue failed:"):
            return f"stems_lyrics: start failed: {line}"
    for lane in ("stems", "lyrics"):
        record = numbers.get(f"{lane}_enqueue")
        status = record.get("status") if isinstance(record, dict) else None
        ids = record.get("stable_ids") if isinstance(record, dict) else None
        running = record.get("already_running") if isinstance(record, dict) else None
        created = (
            status == 201
            and isinstance(ids, list)
            and isinstance(running, list)
            and (track_id in ids or track_id in running)
        )
        if not created:
            return f"stems_lyrics: {lane} job was not created for {track_id} (status {status})"
    status = numbers.get("pending_job_status")
    job_id = numbers.get("pending_job_id")
    if status != 201 or not isinstance(job_id, str) or not job_id:
        return (
            f"stems_lyrics: pending stems job was not created (status {status}, id {job_id!r})"
        )
    return None


def start_stems_lyrics(run: Run, check: Check) -> None:
    """Start both user paths right after import so the slow work overlaps the deck checks.

    Per track (right-click / library lane): stems + lyrics for the vocal
    track. Whole library: one stems.separate job with scope=pending. That
    scope includes the truncated negative control, and the job must fail.
    A non-201 response or a body that does not name the created job raises.
    _guarded_call records that exception, and check_stems_lyrics fails on it.
    """
    engine = run.engine
    assert engine is not None
    track_id = run.ids_by_title.get(run.fixture.stems_lyrics_title)
    if not track_id:
        return
    check.numbers["stems_tiers"] = engine.call("GET", "/api/v1/stems/tiers")[1]
    for lane in ("stems", "lyrics"):
        code, body = _enqueue(engine, lane, track_id)
        check.numbers[f"{lane}_enqueue_status"] = code
        check.numbers[f"{lane}_enqueue"] = _enqueue_record(code, body)
        check.evidence.append(f"enqueue {lane} -> {code}: {str(body)[:300]}")
    payload = pending_scope_stems_payload()
    code, job, _ = engine.call("POST", "/api/v1/jobs",
                               {"kind": "stems.separate", "payload": payload})
    check.numbers["pending_job_status"] = code
    check.numbers["pending_job_payload"] = payload
    if isinstance(job, dict) and isinstance(job.get("id"), str) and job["id"]:
        check.numbers["pending_job_id"] = job["id"]
    check.evidence.append(f"stems.separate {json.dumps(payload)} -> {code}: {str(job)[:300]}")
    problem = stems_start_problem(check.numbers, check.evidence, track_id)
    if problem:
        raise RuntimeError(problem)


def track_stems_problem(stems: dict[str, Any] | None, item: dict[str, Any]) -> str | None:
    """The per-track (library lane) stems request, judged by the bundle it left."""
    if stems and stems["real"]:
        return None
    state = item.get("state")
    if state == "done":
        return (f"stems: the lane reported done ({item.get('started_at')} -> {item.get('finished_at')}) "
                f"but no real stem bundle exists for the track ({stems})")
    if state in TERMINAL_LANE_STATES:
        return f"stems: honest failure, state={state} reason={item.get('reason')}"
    return f"stems: no bundle and no terminal state by the deadline (state={state})"


def _worker_mentions(logs_dir: Path) -> dict[str, int]:
    """How often each data-dir log names a stems worker (0 everywhere = output not captured)."""
    if not logs_dir.is_dir():
        return {}
    return {log.name: sum(log.read_text(errors="replace").count(w)
                          for w in ("stems_local_worker", "stems_r2_first_worker"))
            for log in sorted(logs_dir.glob("*")) if log.is_file()}


@dataclass
class StemsProbe:
    engine: Engine
    track_id: str
    track_seconds: float
    pending_id: str | None

    def stems(self) -> dict[str, Any] | None:
        return _stem_bundle(self.engine, self.track_id, self.track_seconds)

    def lyrics(self) -> dict[str, Any] | None:
        code, body, _ = self.engine.call("GET", f"/api/v1/tracks/{self.track_id}/lyrics")
        return body if code == 200 and isinstance(body, dict) and body.get("lines") else None

    def lane_item(self, lane: str) -> dict[str, Any]:
        body = self.engine.call("GET", f"/api/v1/library-jobs?lane={lane}&include=settled")[1]
        items = body.get("items", []) if isinstance(body, dict) else []
        return next((i for i in items if i.get("stable_id") == self.track_id), {})

    def pending_job(self) -> dict[str, Any]:
        if not self.pending_id:
            return {}
        return self.engine.call("GET", f"/api/v1/jobs/{self.pending_id}")[1] or {}

    def wait(self, deadline: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Until every path has real output or a terminal state, or the deadline."""
        stems = lyrics = None
        while time.monotonic() < deadline:
            stems = stems if stems and stems["real"] else self.stems()
            lyrics = lyrics or self.lyrics()
            stems_settled = bool(stems and stems["real"]) or self.lane_item("stems").get("state") in TERMINAL_LANE_STATES
            lyrics_settled = bool(lyrics) or self.lane_item("lyrics").get("state") in TERMINAL_LANE_STATES
            pending_settled = not self.pending_id or self.pending_job().get("status") in TERMINAL_JOB_STATES
            if stems_settled and lyrics_settled and pending_settled:
                break
            time.sleep(5)
        return stems, lyrics


def check_stems_lyrics(run: Run, check: Check, deadline: float) -> Check:
    engine = run.engine
    assert engine is not None
    track_id = run.ids_by_title.get(run.fixture.stems_lyrics_title)
    if not track_id:
        return check.set(UNKNOWN, f"fixture track {run.fixture.stems_lyrics_title!r} not in library")
    start_problem = stems_start_problem(check.numbers, check.evidence, track_id)
    if start_problem:
        check.defects.append(start_problem)
        return check.set(FAIL, start_problem)
    probe = StemsProbe(engine, track_id, run.fixture.duration_s(run.fixture.stems_lyrics_title),
                       check.numbers.get("pending_job_id"))
    stems, lyrics = probe.wait(deadline)
    # The analysis negative control has replaced this title's bytes with a
    # truncated mp3. A stem bundle of that file is not the question.
    per_track = {t["title"]: _stem_bundle(engine, run.ids_by_title.get(t["title"], ""), t["duration_ms"] / 1000)
                 for t in run.fixture.tracks if t["title"] != run.fixture.negative_title}
    pending = probe.pending_job()
    items = {lane: probe.lane_item(lane) for lane in ("stems", "lyrics")}
    words_code, words, _ = engine.call("GET", f"/api/v1/tracks/{track_id}/lyrics/words")
    mentions = _worker_mentions(run.shell.data_dir(run.home) / "logs")
    check.numbers.update({
        "per_track_stems": per_track, "library_job_items": items, "stems": stems,
        "lyrics_present": bool(lyrics), "lyrics_words_status": words_code, "lyrics_words": str(words)[:400],
        "pending_job": {**{k: pending.get(k) for k in ("id", "status", "message", "error", "started_at",
                                                        "finished_at", "worker_argv")},
                        "seconds": _job_seconds(pending) if pending else None},
        "worker_output_in_data_dir_logs": mentions,
        "library_readiness": engine.call("GET", "/api/v1/library/readiness")[1],
        "jobs": [{"id": j["id"], "kind": j["kind"], "status": j["status"], "seconds": _job_seconds(j),
                  "payload": j.get("payload"), "message": j.get("message")}
                 for j in _jobs(engine) if j.get("kind", "").startswith(("stems", "lyrics"))]})
    if lyrics:
        text = " ".join(str(line.get("text", "")) for line in lyrics.get("lines", [])).lower()
        check.numbers["lyrics_known_words_matched"] = [w for w in run.fixture.lyrics_words if w in text]
    if not sum(mentions.values()):
        check.defects.append(f"stems worker output is not captured: 0 mentions of the worker in {sorted(mentions)}")
    unstemmed = [title for title, bundle in per_track.items() if not (bundle and bundle["real"])]
    control_id = run.ids_by_title.get(run.fixture.negative_title)
    check.numbers["truncated_control_id"] = control_id
    problems = [p for p in (
        pending_stems_problem(probe.pending_id, pending.get("status"), check.numbers["pending_job"]["seconds"],
                              unstemmed, len(per_track), pending.get("error"), control_id),
        track_stems_problem(stems, items["stems"]),
        lyrics_problem(bool(lyrics), items["lyrics"], bool(stems and stems["real"]),
                       check.numbers.get("lyrics_known_words_matched")),
    ) if p]
    if not problems:
        return check.set(PASS, f"pending stems job failed on the truncated control and all "
                               f"{len(per_track)} fixture tracks except that control stemmed; "
                               "each required part streamed; vocal track lyrics present")
    check.defects += problems
    return check.set(FAIL, "; ".join(problems))


def _rekordbox_masters(home: Path) -> list[str]:
    if not home.is_dir():
        return []
    return [str(path) for path in home.rglob("master.db")]


def _import_job(run: Run) -> dict[str, Any]:
    engine = run.engine
    assert engine is not None
    for job_id in run.our_job_ids:
        _code, body, _headers = engine.call("GET", f"/api/v1/jobs/{job_id}", timeout=15)
        if isinstance(body, dict) and body.get("id") == job_id:
            return body
    return {}


def _preview_points(waveform: dict[str, Any]) -> int:
    preview = waveform.get("preview") if isinstance(waveform.get("preview"), dict) else {}
    length = preview.get("length")
    if isinstance(length, int) and length > 0:
        return length
    mid = preview.get("mid") if isinstance(preview.get("mid"), list) else []
    return len(mid)


def _local_bytes(local: dict[str, Any]) -> int:
    encoded = local.get("preview_b64") or ""
    if isinstance(encoded, str) and encoded:
        try:
            return len(base64.b64decode(encoded))
        except (ValueError, TypeError):
            return 0
    size = local.get("bytes")
    return size if isinstance(size, int) and size > 0 else 0


def _lane(provenance: dict[str, Any], name: str) -> dict[str, Any]:
    item = provenance.get(name)
    return item if isinstance(item, dict) else {}


def _observe_track(engine: Engine, track_id: str) -> dict[str, Any]:
    """Stored analysis for one track, read the way the library and the deck read it."""
    _status, track, _headers = engine.call("GET", f"/api/v1/tracks/{track_id}", timeout=30)
    if not isinstance(track, dict):
        track = {}
    provenance = track.get("provenance") if isinstance(track.get("provenance"), dict) else {}
    bpm_lane = _lane(provenance, "bpm")
    key_lane = _lane(provenance, "key")
    loud_lane = _lane(provenance, "loudness_lufs")
    _code, anlz, _headers = engine.call("GET", f"/api/v1/tracks/{track_id}/anlz?points=200", timeout=30)
    if not isinstance(anlz, dict):
        anlz = {}
    grid = anlz.get("beatgrid") if isinstance(anlz.get("beatgrid"), dict) else {}
    waveform = anlz.get("waveform") if isinstance(anlz.get("waveform"), dict) else {}
    local = anlz.get("local_waveform") if isinstance(anlz.get("local_waveform"), dict) else {}
    beats = grid.get("beats") if isinstance(grid.get("beats"), list) else []
    bpm = track.get("bpm") if _finite_number(track.get("bpm")) else bpm_lane.get("value")
    key = track.get("key") if isinstance(track.get("key"), str) and track.get("key") else key_lane.get("value")
    return {
        "bpm": bpm if _finite_number(bpm) else None,
        "key": key if isinstance(key, str) and key else None,
        "bpm_status": bpm_lane.get("status"), "bpm_reason": bpm_lane.get("reason"),
        "key_status": key_lane.get("status"), "key_reason": key_lane.get("reason"),
        "loudness_status": loud_lane.get("status"), "loudness_value": loud_lane.get("value"),
        "loudness_reason": loud_lane.get("reason"),
        "beats": beats, "beatgrid_status": grid.get("status"), "beatgrid_reason": grid.get("reason"),
        "waveform_status": waveform.get("status"), "waveform_reason": waveform.get("reason"),
        "preview_points": _preview_points(waveform), "local_bytes": _local_bytes(local),
        "local_status": local.get("status"), "local_reason": local.get("reason"),
        "local_retryable": local.get("retryable"),
        "needle": track_id, "log_lines": [],
    }


def _compact_observation(observed: dict[str, Any]) -> dict[str, Any]:
    beats = observed.get("beats") or []
    first = beats[0].get("t") if beats and isinstance(beats[0], dict) else None
    kept = {key: observed.get(key) for key in (
        "bpm", "key", "bpm_status", "bpm_reason", "key_status", "key_reason",
        "loudness_status", "loudness_value", "loudness_reason", "beatgrid_status",
        "beatgrid_reason", "waveform_status", "waveform_reason", "preview_points",
        "local_bytes", "local_status", "local_reason", "local_retryable")}
    kept["first_beat_s"] = first
    kept["beat_count"] = len(beats)
    return kept


def _subject_terminal(observed: dict[str, Any], subject: dict[str, Any], tolerance_s: float) -> bool:
    """Ready to judge only when every required lane is present. A failed lane keeps waiting for the others."""
    return not track_analysis_problems(subject["title"], observed, subject, tolerance_s)


def _negative_terminal(observed: dict[str, Any] | None) -> bool:
    if not observed:
        return False
    if corrupt_error_visible(observed):
        return True
    return bool(observed.get("bpm") is not None or observed.get("beats") or observed.get("key"))


def _replace_truncated(run: Run, track: dict[str, Any]) -> str | None:
    """Point the imported carrier at the truncated mp3. Only inside this run's HOME.

    A symlink whose lexical path is inside HOME can resolve outside it.
    write_bytes follows that link, so refuse a symlink and any resolved path
    that is not under the run root, and open the target with O_NOFOLLOW.
    """
    raw = track.get("file_path")
    title = run.fixture.negative_title
    if not isinstance(raw, str) or not raw:
        return f"analysis: negative: {title}: track has no file_path ({raw!r})"
    dest = Path(raw)
    if dest.is_symlink():
        return f"analysis: negative: {title}: file_path {dest} is a symlink"
    try:
        resolved = dest.resolve(strict=True)
    except OSError as exc:
        return f"analysis: negative: {title}: could not resolve {dest}: {exc}"
    home = run.home.resolve()
    if home != resolved and home not in resolved.parents:
        return (f"analysis: negative: {title}: file_path {dest} resolves outside the isolated HOME "
                f"({resolved})")
    payload = run.fixture.negative_file.read_bytes()
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        return f"analysis: negative: {title}: could not replace {dest.name}: {exc}"
    try:
        os.write(fd, payload)
    except OSError as exc:
        os.close(fd)
        return f"analysis: negative: {title}: could not replace {dest.name}: {exc}"
    os.close(fd)
    return None


def _refresh_view(engine: Engine) -> tuple[Any, Any, list[str]]:
    _code, body, _headers = engine.call("GET", "/api/v1/ingest/refresh/status", timeout=15)
    if not isinstance(body, dict):
        return None, None, []
    tail = body.get("log_tail") if isinstance(body.get("log_tail"), list) else []
    return body.get("phase"), body.get("error"), [str(line) for line in tail]


def analysis_drive_problems(index_status: Any, run_status: Any, bumps: dict[str, Any]) -> list[str]:
    """The HTTP that drives analysis. A stored result does not excuse a failed call.

    GET /tracks/index is 200, POST /analysis-queue/run is 202, and every
    POST /ahead-analysis/bump/{id} is 200. 503 means the drain is not armed.
    """
    problems: list[str] = []
    if index_status != TRACKS_INDEX_OK:
        problems.append(
            f"analysis: http: GET /tracks/index -> {index_status}, expected {TRACKS_INDEX_OK}")
    if run_status != ANALYSIS_QUEUE_RUN_OK:
        problems.append(
            f"analysis: http: POST /analysis-queue/run -> {run_status}, expected {ANALYSIS_QUEUE_RUN_OK}")
    if not bumps:
        problems.append("analysis: http: no POST /ahead-analysis/bump was sent")
    else:
        bad = {title: code for title, code in bumps.items() if code != AHEAD_BUMP_OK}
        if bad:
            shown = ", ".join(f"{title}={code}" for title, code in sorted(bad.items()))
            problems.append(
                f"analysis: http: POST /ahead-analysis/bump expected {AHEAD_BUMP_OK}, got {shown}")
    return problems


def check_analysis(run: Run) -> Check:
    """Known-answer analysis over the app's own HTTP, after a folder import with no rekordbox.

    POST /analysis-queue/run (202) is the setup wizard's analyze action.
    POST /ahead-analysis/bump (200) is what opening a track does to the installed
    app's own-lane drain (loudness, waveform, beatgrid, key). GET /tracks/index
    (200) ends that drain's startup grace, which is what opening the library does.
    Another status fails the check, and a stored result cannot clear it.
    The refresh job's phase is recorded and is never the verdict.
    """
    check = Check("analysis")
    engine = run.engine
    assert engine is not None
    masters = _rekordbox_masters(run.home)
    job = _import_job(run)
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    mode = payload.get("mode")
    roots = payload.get("roots") or payload.get("folders") or []
    library = str(run.home / "fixture-library")
    folder_import = mode == "folder" and any(str(Path(root)) == library for root in roots)
    check.numbers.update({"rekordbox_master_db": masters, "import_mode": mode, "import_roots": list(roots),
                          "octave_policy": "not accepted; see fixture.json analysis.octave_policy",
                          "relative_key_policy": "not accepted; see fixture.json analysis.relative_key_policy"})
    problems: list[str] = []
    if masters:
        problems.append(f"analysis: import: rekordbox master.db reachable at {masters}")
    if not folder_import:
        problems.append(
            f"analysis: import: source mode {mode!r} roots {list(roots)}, expected a folder import of {library}")

    negative_id = run.ids_by_title.get(run.fixture.negative_title)
    if not negative_id:
        problems.append(f"analysis: negative: {run.fixture.negative_title}: not in the library after import")
    else:
        _code, carrier, _headers = engine.call("GET", f"/api/v1/tracks/{negative_id}", timeout=30)
        replaced = _replace_truncated(run, carrier if isinstance(carrier, dict) else {})
        if replaced:
            problems.append(replaced)
        else:
            check.numbers["negative_replaced"] = carrier.get("file_path") if isinstance(carrier, dict) else True

    # Grace ends when the library index is served. Do that after the bytes
    # are already the truncated file, so the drain never measures the carrier.
    index_code, _body, _headers = engine.call("GET", "/api/v1/tracks/index", timeout=60)
    run_code, run_body, _headers = engine.call("POST", "/api/v1/analysis-queue/run", timeout=30)
    check.numbers["tracks_index_status"] = index_code
    check.numbers["analysis_queue_run"] = {"status": run_code, "body": str(run_body)[:400]}
    bumps: dict[str, int] = {}
    for title, stable_id in run.ids_by_title.items():
        code, _body, _headers = engine.call("POST", f"/api/v1/ahead-analysis/bump/{stable_id}", timeout=15)
        bumps[title] = code
    check.numbers["bumps"] = bumps
    drive = analysis_drive_problems(index_code, run_code, bumps)
    if drive:
        problems.extend(drive)
        check.evidence.append(
            "analysis HTTP did not return its success status; stored results are not accepted")

    if not drive:
        subjects = run.fixture.analysis_subjects
        started = time.monotonic()
        deadline = started + ANALYSIS_BUDGET_S
        observations: dict[str, dict[str, Any]] = {}
        phase: Any = None
        error: Any = None
        while True:
            phase, error, logs = _refresh_view(engine)
            wanted = [(subject["title"], run.ids_by_title.get(subject["title"])) for subject in subjects]
            if negative_id:
                wanted.append((run.fixture.negative_title, negative_id))
            for title, stable_id in wanted:
                if not stable_id:
                    continue
                observed = _observe_track(engine, stable_id)
                observed["log_lines"] = [line for line in logs if stable_id in line][:8]
                observations[title] = observed
            pending = [subject["title"] for subject in subjects
                       if subject["title"] not in observations
                       or not _subject_terminal(observations[subject["title"]], subject, run.fixture.downbeat_tolerance_s)]
            negative_done = not negative_id or _negative_terminal(observations.get(run.fixture.negative_title))
            if not pending and negative_done:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(3)
        elapsed = round(time.monotonic() - started, 1)
        phase, error, _logs = _refresh_view(engine)
        check.numbers.update({"analysis_seconds": elapsed, "analysis_budget_s": ANALYSIS_BUDGET_S,
                              "refresh_phase": phase, "refresh_error": error,
                              "tracks": {title: _compact_observation(obs) for title, obs in observations.items()}})
        check.evidence.append(f"refresh phase {phase} is recorded and is not a pass; error={str(error)[:200]}")
        missing = [subject["title"] for subject in subjects
                   if not (observations.get(subject["title"]) or {}).get("bpm")
                   and not (observations.get(subject["title"]) or {}).get("beats")
                   and not (observations.get(subject["title"]) or {}).get("key")]
        if len(missing) == len(subjects):
            blocked = analysis_blocked_problem(run.fixture.negative_title, missing, phase, error)
            if blocked:
                problems.append(blocked)
        for subject in subjects:
            title = subject["title"]
            if title not in run.ids_by_title:
                problems.append(f"analysis: bpm: {title}: not in the library after import")
                continue
            problems.extend(track_analysis_problems(title, observations.get(title) or {}, subject,
                                                    run.fixture.downbeat_tolerance_s))
        if negative_id:
            found = corrupt_observation_problem(
                run.fixture.negative_title, observations.get(run.fixture.negative_title) or {})
            if found:
                problems.append(found)
    if problems:
        check.defects += problems
        return check.set(FAIL, "; ".join(problems))
    summary = ", ".join(
        f"{subject['title']} bpm {observations[subject['title']].get('bpm')} "
        f"key {observations[subject['title']].get('key')}"
        for subject in subjects
    )
    return check.set(PASS, f"analyzed in {elapsed}s (budget {ANALYSIS_BUDGET_S}s); {summary}")


def check_clean_quit(run: Run) -> Check:
    check = Check("clean_quit")
    engine = run.engine
    assert engine is not None
    bundle_marker = str(run.shell.app) + "/Contents/"
    home_marker = str(run.home)

    def survivors() -> list[Proc]:
        return [p for p in _ps() if bundle_marker in p.command or home_marker in p.command]

    before = survivors()
    check.numbers["processes_before_quit"] = len(before)
    if len(before) < 2:
        return check.set(UNKNOWN, f"orphan probe sees {len(before)} app processes before quit: it cannot see "
                                  "the app, so an empty result after would mean nothing")
    t0 = time.monotonic()
    code, body, _ = engine.call("POST", "/api/v1/lifecycle/quit", {"force": True})
    if code != 200:
        return check.set(FAIL, f"lifecycle quit -> {code}: {str(body)[:200]}")
    _poll(lambda: not survivors(), 40, 0.2)
    quit_s = round(time.monotonic() - t0, 2)
    left = survivors()
    port_open = _port_listening(engine.port)
    findings = run.shell.quit_log_findings(run.home)
    exit_ms = findings["engine_exit_ms_after_sigterm"]
    check.numbers.update({"quit_seconds": quit_s, "quit_max_s": QUIT_TOTAL_MAX_S,
                          "engine_exit_ms_after_sigterm": exit_ms[-1] if exit_ms else None,
                          "engine_exit_max_ms": ENGINE_SIGTERM_EXIT_MAX_MS,
                          "survivors": [f"{p.pid} {p.command[:160]}" for p in left],
                          "port_listening_after": port_open})
    check.evidence += findings["shutdown_lines"]
    problems = quit_problems(quit_s, len(left), exit_ms, findings["sigkill_lines"], port_open)
    if problems is None:
        return check.set(UNKNOWN, "shell logged no 'exited Nms after SIGTERM' line: engine exit time unmeasured")
    if problems:
        check.defects += problems
        return check.set(FAIL, "; ".join(problems))
    return check.set(PASS, f"quit in {quit_s}s, engine exited {exit_ms[-1]} ms after SIGTERM, 0 survivors, port released")


def _port_listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _stage_fixture_library(src: Path, dest: Path) -> None:
    """Copy the fixture library. A ditto failure is a REFUSED result, not a crash."""
    try:
        subprocess.run(["/usr/bin/ditto", str(src), str(dest)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"fixture staging failed (ditto exit {exc.returncode})"
        if detail:
            message = f"{message}: {detail[:400]}"
        raise Refused(message) from exc
    except OSError as exc:
        raise Refused(f"fixture staging failed: /usr/bin/ditto: {exc}") from exc


# ----- orchestration ----------------------------------------------------------------
def _boot(run: Run) -> str | None:
    """Launch and wait for a healthy engine in the isolated data dir. None = ok."""
    run.pids_before_launch = {p.pid for p in _ps()}
    run.launched_at = time.monotonic()
    run.shell.launch(run.home)
    port = _poll(lambda: run.shell.engine_port(run.home), BOOT_TIMEOUT_S)
    if port is None:
        return f"no .engine.lock in {run.shell.data_dir(run.home)} within {BOOT_TIMEOUT_S}s"
    engine = run.engine = Engine(port)
    healthy = _poll(lambda: engine.call("GET", "/api/v1/health", timeout=5)[0] == 200, BOOT_TIMEOUT_S)
    if not healthy:
        return f"engine on port {port} never answered /api/v1/health 200"
    shells = [p for p in _ps() if p.command.startswith(str(run.shell.executable)) and p.pid not in run.pids_before_launch]
    if len(shells) != 1:
        return f"expected 1 new shell process, found {len(shells)}"
    run.shell_pid = shells[0].pid
    return None


def run_acceptance(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    app = Path(args.app).resolve()
    if not (app / "Contents" / "Info.plist").is_file():
        raise Refused(f"{app} is not an app bundle")
    host = _host_name()
    if host_refused(host):
        raise Refused(f"host {host!r} is silver: the acceptance never runs there (the maintainer's live store)")
    shell = Shell.detect(app)
    running = _running_same_identifier(shell)
    if running:
        raise Refused(f"another {shell.identifier} instance is running ({'; '.join(running)}); "
                      "the single-instance plugin would swallow the launch. Quit it first.")
    fixture = Fixture.load(Path(args.fixture).resolve())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(args.run_root).resolve() if args.run_root else ACCEPTANCE_ROOT / stamp
    home = root / "home"
    try:
        real_before = _real_store_stat(shell)
    except Unmeasurable as exc:
        raise Refused(
            f"real store unreadable, acceptance cannot prove it stays unchanged: {exc}"
        ) from exc
    if home.exists():
        raise Refused(f"{home} already exists: every run needs a fresh HOME")
    home.mkdir(parents=True)
    _stage_fixture_library(fixture.root / "library", home / "fixture-library")
    run = Run(shell=shell, fixture=fixture, home=home, expect_sha=args.expect_sha,
              stems_timeout_s=args.stems_timeout, lyrics_timeout_s=args.lyrics_timeout)
    result: dict[str, Any] = {"schema": SCHEMA, "started_at": _now(), "host": host, "app": str(app),
                              "identifier": shell.identifier, "shell": type(shell).__name__,
                              "run_root": str(root), "identity": shell.manifest_identity(), "checks": []}
    checks: list[Check] = []
    try:
        return _launch_and_check(run, result, checks, real_before)
    except Exception as exc:  # the app must never outlive the harness
        checks.append(Check("harness").set(UNKNOWN, f"harness error {type(exc).__name__}: {exc}"))
        return _finish(result, checks, run, real_before)


def _launch_and_check(run: Run, result: dict[str, Any], checks: list[Check],
                      real_before: dict[str, tuple[str, int] | None]) -> tuple[dict[str, Any], int]:
    shell, home = run.shell, run.home
    isolation = Check("isolation")
    checks.append(isolation)
    _log(f"launching {shell.app} with HOME={home}")
    boot_error = _boot(run)
    if boot_error:
        isolation.set(UNKNOWN, f"app did not boot: {boot_error}")
        return _finish(result, checks, run, real_before)
    assert run.engine is not None
    status = run.engine.get("/api/v1/setup/status")
    reported = Path(status.get("data_dir", ""))
    isolation.numbers = {"reported_data_dir": str(reported), "expected_data_dir": str(shell.data_dir(home)),
                         "boot_seconds": round(time.monotonic() - run.launched_at, 1)}
    if reported != shell.data_dir(home):
        isolation.set(FAIL, f"engine data_dir {reported} is NOT the isolated {shell.data_dir(home)}: stopping before any write")
        _quit_quietly(run)
        return _finish(result, checks, run, real_before)
    isolation.set(PASS, f"engine data_dir is the isolated run dir (boot {isolation.numbers['boot_seconds']}s)")
    try:
        run.memory_boot = _memory_sample(run)
    except Unmeasurable as exc:
        run.memory_boot = {"error": str(exc)}
    stems_check = Check("stems_lyrics")
    checks.extend(_guarded(fn, run) for fn in (check_manifest_sha, check_first_run_import))
    # Analysis before stems: the negative control replaces one track's bytes,
    # and that has to happen before a drain measures the intact carrier.
    # The stems job is scope=pending, so the truncated file is in it and the
    # local worker must fail the job. The other tracks still need bundles.
    # clean_quit stays last.
    checks.append(_guarded(check_analysis, run))
    stems_deadline = time.monotonic() + max(run.stems_timeout_s, run.lyrics_timeout_s)
    _guarded_call(start_stems_lyrics, run, stems_check)
    checks.extend(_guarded(fn, run) for fn in (check_deck_load, check_audio_plays))
    checks.append(_guarded(lambda r: check_memory(r) if r.memory_boot.get("total_mb") else
                           Check("memory").set(UNKNOWN, f"boot RSS unmeasured: {r.memory_boot.get('error')}"), run, "memory"))
    checks.append(_guarded(lambda r: check_stems_lyrics(r, stems_check, stems_deadline), run, "stems_lyrics"))
    checks.append(_guarded(check_clean_quit, run))
    return _finish(result, checks, run, real_before)


def _guarded(fn: Callable[[Run], Check], run: Run, check_id: str | None = None) -> Check:
    name = check_id or fn.__name__.replace("check_", "")
    _log(f"check {name} ...")
    try:
        check = fn(run)
    except Unmeasurable as exc:
        check = Check(name).set(UNKNOWN, f"unmeasurable: {exc}")
    except Exception as exc:  # a crashed instrument is UNKNOWN, never a pass
        check = Check(name).set(UNKNOWN, f"instrument error {type(exc).__name__}: {exc}")
    _log(f"check {name}: {check.verdict} - {check.summary}")
    return check


def _guarded_call(fn: Callable[[Run, Check], None], run: Run, check: Check) -> None:
    """Record a start exception where check_stems_lyrics cannot ignore it.

    Evidence alone is not a verdict. start_error is, and the later check
    fails when it is set even if a per-track bundle exists.
    """
    try:
        fn(run, check)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        check.evidence.append(f"enqueue failed: {message}")
        check.numbers["start_error"] = message


def _quit_quietly(run: Run) -> None:
    if run.engine is not None:
        run.engine.call("POST", "/api/v1/lifecycle/quit", {"force": True}, timeout=10)


def _finish(result: dict[str, Any], checks: list[Check], run: Run,
            real_before: dict[str, tuple[str, int] | None]) -> tuple[dict[str, Any], int]:
    leftover = _cleanup(run)
    if leftover:
        checks.append(Check("harness_cleanup").set(FAIL, leftover))
    guard = Check("real_store_untouched")
    try:
        real_after = _real_store_stat(run.shell)
    except Unmeasurable as exc:
        guard.set(FAIL, f"real store unreadable, not proven unchanged: {exc}")
    else:
        guard.numbers = {"before": real_before, "after": real_after}
        if real_before == real_after:
            guard.set(PASS, "real state.db and its WAL sidecars unchanged" if real_before.get("state.db") else
                      "no real state.db on this host before or after")
        else:
            changed = [n for n in real_after if real_before.get(n) != real_after[n]]
            guard.set(FAIL, f"the real app data dir CHANGED during the run: {changed}")
    checks.append(guard)
    result["checks"] = [asdict(c) for c in checks]
    result["finished_at"] = _now()
    failed = [c.id for c in checks if c.verdict != PASS]
    result["overall"] = PASS if not failed else FAIL
    result["failed_checks"] = failed
    result["defects"] = [f"{c.id}: {d}" for c in checks for d in c.defects]
    return result, 0 if not failed else 1


def _run_processes(run: Run) -> list[Proc]:
    shell_exe = str(run.shell.executable)
    return [p for p in _ps() if p.pid not in run.pids_before_launch
            and (p.command.startswith(shell_exe) or str(run.home) in p.command)]


def _cleanup(run: Run) -> str | None:
    """Leave nothing running. Returns a note when it had to signal anything."""
    if not run.launched_at or not _run_processes(run):
        return None
    port = run.shell.engine_port(run.home)
    if port is not None:
        Engine(port).call("POST", "/api/v1/lifecycle/quit", {"force": True}, timeout=10)
        if _poll(lambda: not _run_processes(run), 40, 0.5):
            return None
    left = _run_processes(run)
    for proc in left:
        with contextlib.suppress(OSError):
            os.kill(proc.pid, 15)
    _poll(lambda: not _run_processes(run), CLEANUP_SIGTERM_WAIT_S, 0.5)
    survivors = _run_processes(run)
    note = f"harness had to SIGTERM {len(left)} leftover process(es) after the run: " + \
        "; ".join(f"{p.pid} {p.command[:100]}" for p in left)
    if survivors:
        note += (f"; {len(survivors)} STILL ALIVE {CLEANUP_SIGTERM_WAIT_S}s later (left for a human, never SIGKILLed): "
                 + "; ".join(f"{p.pid} {p.command[:100]}" for p in survivors))
    return note


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--app", required=True, help="installed .app bundle to test")
    parser.add_argument("--expect-sha", required=True, help="full git sha the build must carry")
    parser.add_argument("--fixture", required=True, help="fixture dir (fixture.json + library/)")
    parser.add_argument("--result", required=True, help="JSON result file to write")
    parser.add_argument("--run-root", help="per-run root (default ~/.opendj-acceptance/runs/<utc stamp>)")
    parser.add_argument("--stems-timeout", type=float, default=STEMS_TIMEOUT_S)
    parser.add_argument("--lyrics-timeout", type=float, default=LYRICS_TIMEOUT_S)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.expect_sha):
        print(f"[ERROR] --expect-sha must be a full 40-char sha, got {args.expect_sha!r}", file=sys.stderr)
        return 2
    try:
        result, code = run_acceptance(args)
    except Refused as exc:
        result, code = {"schema": SCHEMA, "overall": "REFUSED", "refused": str(exc), "at": _now()}, 2
    Path(args.result).write_text(json.dumps(result, indent=2, default=str) + "\n")
    _print_report(result)
    return code


def _print_report(result: dict[str, Any]) -> None:
    print("\n==== OPEN DJ INSTALLED-APP ACCEPTANCE ====")
    if result.get("overall") == "REFUSED":
        print(f"REFUSED: {result['refused']}")
        return
    print(f"app {result['app']} sha {result['identity'].get('git_sha_full', '')[:12]} host {result['host']}")
    for check in result["checks"]:
        print(f"  {check['verdict']:<8} {check['id']:<22} {check['summary']}")
    for defect in result["defects"]:
        print(f"  DEFECT  {defect}")
    print(f"OVERALL {result['overall']}" + (f" (failed: {', '.join(result['failed_checks'])})" if result["failed_checks"] else ""))


if __name__ == "__main__":
    sys.exit(main())
