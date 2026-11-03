"""Prove an Electron update installs: N -> N+1 from a private local feed, in a throwaway home.

Spec: ``(private path)`` section (e), parity matrix
row 37. Runs ON the test Mac (the Air), never on silver, and never touches
``/Applications``. Stdlib only, so it runs under any python3.

Inputs are two ``scripts/electron_release.py --proof`` builds made with
``--proof-env`` naming THIS run's root (the LSEnvironment is what keeps the
app that Squirrel.Mac's ShipIt relaunches inside the throwaway home):

    HOME=<root>/home  CFFIXED_USER_HOME=<root>/home
    OPENDJ_UPDATE_FEED_URL=http://127.0.0.1:<port>

A packaged build ignores ``OPENDJ_SHELL_DATA_DIR`` (``engineDataDir`` in
``src/main.ts``), so the data dir is ``appData/com.opendj.desktop`` and
``CFFIXED_USER_HOME`` is what puts appData inside the root: measured on the
Air, Tue 3 Nov 2026, the engine landed in
the ``Application Support/com.opendj.desktop`` folder under ``<root>``'s isolated home and the real
one was untouched. The Tauri shell ignores both and resolves the folder from the
account home, so a bridge run needs a library-free account (``--bridge``).

What a run does, in order, failing fast with the step named:

1. Lays out ``<root>/{apps,home,feed}``, copies N into ``apps`` and N+1's
   zip, ``latest-mac.yml`` and ``latest.json`` into ``feed``.
2. Serves ``feed`` on 127.0.0.1:<port> and opens N with ``open -n``.
3. Waits for N's engine (``<data>/.engine.lock``) to report N in
   ``/api/v1/build-info``; writes a marker row into ``<data>/state/state.db`` and
   records that file's inode.
4. ``GET /api/v1/update/check`` must say ``update-available`` N -> N+1.
5. ``POST /api/v1/update/apply``, polls the command, then waits for a NEW engine
   (new lock pid) reporting N+1. The shell's updater downloads, Squirrel.Mac
   verifies the signature and swaps the bundle, ShipIt relaunches it.
6. Checks: bundle ``CFBundleShortVersionString`` is N+1, ``/update/check`` says
   ``up-to-date``, the same ``state.db`` (inode) still holds the marker.
7. Always: stops the app (by pid, inside the root only), the feed server, and
   deletes the root, then proves it is gone. Writes the evidence JSON.

Acceptance:

- [if] --root is not a new folder under the user's home, or is inside an Applications folder [then] refuse [else stop].
- [if] N's baked LSEnvironment does not point inside --root and at the local feed [then] refuse before launching [else stop].
- [if] the relaunched engine reports anything but N+1 within the timeout [then] exit 1 naming the last answer [else stop].
- [if] the marker row is missing after the update, or state.db's inode changed [then] exit 1 [else stop].
- [if] cleanup leaves the root behind [then] exit 1 [else stop].

-Claude
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Open DJ.app"
BUNDLE_ID = "com.opendj.desktop"
ELECTRON_EXECUTABLE = "Open DJ"
ELECTRON_FRAMEWORK = "Contents/Frameworks/Electron Framework.framework"
MARKER_TABLE = "odj_update_proof"
FEED_SERVICE_ID = "com.opendj.release-update-proof-feed"
BOOT_TIMEOUT_S = 240.0
RELAUNCH_TIMEOUT_S = 300.0
APPLY_POLL_S = 120.0
POLL_S = 2.0


class ProofError(RuntimeError):
    """A step failed. The message names the step and what was seen."""


@dataclass(frozen=True)
class Layout:
    root: Path
    #: Tauri bridge run: N is the Tauri app and N+1 arrives through latest.json.
    bridge: bool = False
    #: Bridge runs only: the library-free account's own home. The Tauri shell resolves its data dir
    #: from the account home whatever HOME says, so both shells must be baked to use it.
    account_home: Path | None = None

    @property
    def apps(self) -> Path:
        return self.root / "apps"

    @property
    def app(self) -> Path:
        return self.apps / APP_NAME

    @property
    def home(self) -> Path:
        if self.bridge:
            if self.account_home is None:
                raise ProofError("a bridge layout needs the account home")
            return self.account_home
        return self.root / "home"

    @property
    def data(self) -> Path:
        """Where both shells put the library under the isolated home (appData / bundle id)."""
        return self.home / "Library" / "Application Support" / BUNDLE_ID

    @property
    def feed(self) -> Path:
        return self.root / "feed"

    @property
    def lock(self) -> Path:
        return self.data / ".engine.lock"

    @property
    def state_db(self) -> Path:
        return self.data / "state" / "state.db"


@dataclass
class Evidence:
    steps: list[dict] = field(default_factory=list)

    def record(self, step: str, **facts: object) -> None:
        self.steps.append({"step": step, "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **facts})
        print(f"[OK] {step}: {json.dumps(facts, sort_keys=True)}", flush=True)


# ----- guards ---------------------------------------------------------------------


def check_root(root: Path, home: Path) -> None:
    resolved, home_resolved = root.resolve(), home.resolve()
    if resolved == home_resolved or home_resolved not in resolved.parents:
        raise ProofError(f"--root {root} must be a new folder under {home}")
    if "Applications" in resolved.parts:
        raise ProofError(f"--root {root} must not be inside an Applications folder")
    if resolved.exists():
        raise ProofError(f"--root {root} already exists; pick a fresh throwaway folder")


def _info_plist(app: Path) -> dict:
    with (app / "Contents" / "Info.plist").open("rb") as handle:
        return plistlib.load(handle)


def bundle_version(app: Path) -> str:
    return str(_info_plist(app)["CFBundleShortVersionString"])


def expected_environment(layout: Layout, port: int) -> dict[str, str]:
    return {
        "HOME": str(layout.home),
        "CFFIXED_USER_HOME": str(layout.home),
        "OPENDJ_UPDATE_FEED_URL": f"http://127.0.0.1:{port}",
    }


def check_environment(info: dict, subject: str, layout: Layout, port: int) -> None:
    """Refuse a build whose baked LSEnvironment would let a launch or relaunch escape the root."""
    baked = dict(info.get("LSEnvironment") or {})
    wanted = expected_environment(layout, port)
    wrong = {k: baked.get(k) for k, v in wanted.items() if baked.get(k) != v}
    if wrong:
        raise ProofError(f"{subject} LSEnvironment does not isolate this run: wanted {wanted}, differs at {wrong}")


def check_isolation(app: Path, layout: Layout, port: int) -> None:
    check_environment(_info_plist(app), str(app), layout, port)


def feed_bundle_info(feed: Path, bridge: bool) -> tuple[str, dict]:
    """N+1's Info.plist, read out of the archive the updater will install (zip, or the bridge tar.gz)."""
    inner = f"{APP_NAME}/Contents/Info.plist"
    if bridge:
        archives = sorted(feed.glob("*.app.tar.gz"))
        if len(archives) != 1:
            raise ProofError(f"{feed} must hold exactly one .app.tar.gz for a bridge run, holds {archives}")
        with tarfile.open(archives[0], "r:gz") as tar:
            member = tar.extractfile(inner)
            if member is None:
                raise ProofError(f"{archives[0]} has no {inner}")
            return str(archives[0]), plistlib.loads(member.read())
    zips = sorted(feed.glob("*-mac.zip"))
    if len(zips) != 1:
        raise ProofError(f"{feed} must hold exactly one *-mac.zip, holds {zips}")
    with zipfile.ZipFile(zips[0]) as archive:
        return str(zips[0]), plistlib.loads(archive.read(inner))


# ----- engine ------------------------------------------------------------------------


def read_lock(lock: Path) -> dict | None:
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("port"), int) else None


def http_json(method: str, url: str, timeout: float = 10.0) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body or b"{}")
        except ValueError:
            return error.code, {"raw": body.decode("utf-8", "replace")}


class EscapeWatch:
    """Fails the run the moment any engine writes the machine's REAL library lock.

    Measured on the Air, Tue 3 Nov 2026: a Tauri proof build ignored HOME and
    CFFIXED_USER_HOME and booted on the real library; nothing in the run noticed
    for four minutes because it only watched the throwaway lock.
    """

    def __init__(self, real_lock: Path) -> None:
        self.real_lock = real_lock
        self.before = self._fingerprint()

    def _fingerprint(self) -> tuple[float, int] | None:
        try:
            stat = self.real_lock.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime, stat.st_size)

    def check(self) -> None:
        if self._fingerprint() != self.before:
            raise ProofError(
                f"an engine wrote the REAL library lock {self.real_lock}: the build is not isolated; stop now"
            )


def real_library_lock() -> Path:
    return Path.home() / "Library" / "Application Support" / BUNDLE_ID / ".engine.lock"


def app_traces(home: Path) -> list[Path]:
    """Every per-app folder or file either shell leaves under an account's Library (data, caches,
    ShipIt, WebKit, prefs, saved state, logs)."""
    library = home / "Library"
    if not library.is_dir():
        return []
    return sorted(
        entry
        for area in library.iterdir()
        if area.is_dir()
        for entry in area.iterdir()
        if entry.name.startswith((BUNDLE_ID, "Open DJ", "opendj"))
    )


def remove_app_traces(home: Path) -> list[str]:
    removed = []
    for entry in app_traces(home):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(str(entry))
    if app_traces(home):
        raise ProofError(f"app traces still under {home}/Library: {app_traces(home)}")
    return removed


def check_bridge_account(layout: Layout) -> None:
    """The Tauri shell resolves its data dir from the real home whatever HOME or CFFIXED_USER_HOME say,
    and cleanup removes every app trace in that home, so the account must hold none before the run."""
    if layout.bridge and (traces := app_traces(Path.home())):
        raise ProofError(
            "--bridge needs a macOS account with no Open DJ files: the Tauri shell resolves its data dir "
            f"from the real home, and cleanup would remove these, which this run did not create: {traces}"
        )


def wait_for_engine(
    lock: Path, version: str, timeout: float, escape: EscapeWatch, not_pid: int | None = None
) -> tuple[str, dict]:
    """The origin of an engine (other than ``not_pid``) whose build-info reports ``version``."""
    deadline = time.monotonic() + timeout
    last: object = "no engine lock yet"
    while time.monotonic() < deadline:
        escape.check()
        payload = read_lock(lock)
        if payload is not None and payload.get("pid") != not_pid:
            origin = f"http://{payload.get('host') or '127.0.0.1'}:{payload['port']}"
            try:
                status, info = http_json("GET", f"{origin}/api/v1/build-info", timeout=5.0)
            except (urllib.error.URLError, OSError) as error:
                last = f"{origin} not answering: {error}"
            else:
                last = {"status": status, "app_version": info.get("app_version"), "pid": payload.get("pid")}
                if status == 200 and info.get("app_version") == version:
                    return origin, payload
        time.sleep(POLL_S)
    raise ProofError(f"no engine reported app_version {version} within {timeout:.0f} s; last: {last}")


def write_marker(state_db: Path, marker: str) -> int:
    with sqlite3.connect(state_db, timeout=30) as db:
        db.execute(f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (marker TEXT NOT NULL)")
        db.execute(f"INSERT INTO {MARKER_TABLE} (marker) VALUES (?)", (marker,))
    return state_db.stat().st_ino


def read_markers(state_db: Path) -> list[str]:
    with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=30) as db:
        return [row[0] for row in db.execute(f"SELECT marker FROM {MARKER_TABLE}")]


# ----- processes ------------------------------------------------------------------------


def ancestor_pids(pid: int) -> set[int]:
    """This process and every parent up to launchd: never signalled, whatever their arguments say."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    parent = {int(a): int(b) for a, b in (line.split() for line in out.splitlines() if line.strip())}
    chain = {pid}
    while pid in parent and parent[pid] not in chain and parent[pid] > 1:
        pid = parent[pid]
        chain.add(pid)
    return chain


def pids_under(root: Path, exclude: set[int]) -> list[int]:
    """Processes whose command line names the root: the app, its helpers, and the payload engine (whose
    setproctitle name still carries its interpreter path under the root). The root is a fresh folder only
    this run uses, so the only other processes naming it are this run's own ancestors (the ssh shell,
    caffeinate), which the caller passes in ``exclude``."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True).stdout
    found: list[int] = []
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if pid_text.isdigit() and f"{root}/" in command and int(pid_text) not in exclude:
            found.append(int(pid_text))
    return found


def stop_everything(root: Path, server: subprocess.Popen | None) -> list[str]:
    """Stop every process whose command line names the root (the app, its engine, the feed)."""
    notes: list[str] = []
    exclude = ancestor_pids(os.getpid())
    if server is not None and server.poll() is None:
        server.terminate()
        server.wait(timeout=10)
    for sig, wait in ((signal.SIGTERM, 20.0), (signal.SIGKILL, 5.0)):
        pids = pids_under(root, exclude)
        if not pids:
            break
        notes.append(f"{sig.name} {pids}")
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and pids_under(root, exclude):
            time.sleep(0.5)
    left = pids_under(root, exclude)
    if left:
        raise ProofError(f"processes still running under {root}: {left}")
    return notes


# ----- the run ---------------------------------------------------------------------------


def point_bridge_at_local_feed(manifest_path: Path, port: int) -> None:
    """The release names the immutable GitHub tag; the proof serves the same signed bytes locally."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["platforms"]["darwin-aarch64"]
    archive = entry["url"].rsplit("/", 1)[-1]
    if not (manifest_path.parent / archive).is_file():
        raise ProofError(f"the bridge latest.json names {archive}, which is not in the feed")
    entry["url"] = f"http://127.0.0.1:{port}/{archive}"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def require_port_free(port: int) -> None:
    """The port is baked into the builds, so a squatter would answer the app's feed requests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ProofError(
                f"127.0.0.1:{port} is already in use ({error}); rebuild the proof pair with a free port in "
                "OPENDJ_UPDATE_FEED_URL (lsof -nP -iTCP:<port> -sTCP:LISTEN names the holder)"
            ) from error


def start_feed(layout: Layout, port: int) -> subprocess.Popen:
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(layout.feed)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "AF_SERVICE_ID": FEED_SERVICE_ID},
    )
    time.sleep(1.0)
    status, _ = http_json("GET", f"http://127.0.0.1:{port}/latest.json")
    if status != 200:
        raise ProofError(f"the local feed did not serve latest.json (HTTP {status})")
    return server


def poll_apply(origin: str, command_id: str) -> object:
    """The command's last state. The old engine vanishing mid-poll IS the relaunch."""
    started, last = time.monotonic(), None
    while time.monotonic() - started < APPLY_POLL_S:
        try:
            _, state = http_json("GET", f"{origin}/api/v1/update/apply/{command_id}", timeout=5.0)
        except (urllib.error.URLError, OSError):
            return last
        last = state
        if state.get("state") == "failed":
            raise ProofError(f"the shell reported the apply failed: {state}")
        if state.get("state") == "succeeded":
            return last
        time.sleep(POLL_S)
    return last


def lay_out_root(args: argparse.Namespace, layout: Layout) -> None:
    """Copy N into the root and N+1's feed files beside it; nothing is launched yet."""
    check_isolation(args.n_app, layout, args.port)
    subject, info = feed_bundle_info(args.feed, layout.bridge)
    check_environment(info, subject, layout, args.port)
    if str(info.get("CFBundleShortVersionString")) != args.expect_version:
        raise ProofError(
            f"{subject} is {info.get('CFBundleShortVersionString')}, not --expect-version {args.expect_version}"
        )
    for name in ("latest-mac.yml", "latest.json"):
        if not (args.feed / name).is_file():
            raise ProofError(f"{args.feed} has no {name}")
    for path in (layout.apps, layout.feed):
        path.mkdir(parents=True)
    # A bridge run's data dir is created by the Tauri shell in the account home; never pre-made there.
    if not layout.bridge:
        layout.data.mkdir(parents=True)
    subprocess.run(["/usr/bin/ditto", str(args.n_app), str(layout.app)], check=True)
    for item in args.feed.iterdir():
        if item.suffix in (".zip", ".yml", ".json", ".blockmap", ".gz", ".sig"):
            shutil.copy2(item, layout.feed / item.name)
    if layout.bridge:
        point_bridge_at_local_feed(layout.feed / "latest.json", args.port)


def run_proof(args: argparse.Namespace, layout: Layout, evidence: Evidence, servers: list[subprocess.Popen]) -> None:
    # A Tauri bundle's CFBundleShortVersionString drops the prerelease
    # ("1.0.0" for 1.0.0-alpha.1), so a bridge run names N's engine version.
    n_version = args.n_version or bundle_version(args.n_app)
    next_version = args.expect_version
    require_port_free(args.port)
    lay_out_root(args, layout)
    evidence.record(
        "layout", root=str(layout.root), n_version=n_version, feed=sorted(p.name for p in layout.feed.iterdir())
    )

    # A bridge run's account library is the proof library (check_bridge_account proved it absent at start).
    escape = EscapeWatch(layout.root / "no-real-library" / ".engine.lock" if layout.bridge else real_library_lock())
    servers.append(start_feed(layout, args.port))
    subprocess.run(["/usr/bin/open", "-n", str(layout.app)], check=True)
    started = time.monotonic()
    origin, lock = wait_for_engine(layout.lock, n_version, BOOT_TIMEOUT_S, escape)
    evidence.record("n booted", origin=origin, pid=lock.get("pid"), boot_s=round(time.monotonic() - started, 1))

    marker = f"proof-{int(time.time())}"
    inode = write_marker(layout.state_db, marker)
    evidence.record("marker written", state_db=str(layout.state_db), inode=inode, marker=marker)

    status, check = http_json("GET", f"{origin}/api/v1/update/check")
    if check.get("status") != "update-available" or check.get("available_version") != next_version:
        raise ProofError(f"update/check before apply: HTTP {status} {check}")
    evidence.record(
        "check before",
        status=check["status"],
        current=check.get("current_version"),
        available=check.get("available_version"),
        endpoint=check.get("endpoint"),
    )

    applied_at = time.monotonic()
    status, accepted = http_json("POST", f"{origin}/api/v1/update/apply")
    if status != 202 or not accepted.get("command_id"):
        raise ProofError(f"update/apply refused: HTTP {status} {accepted}")
    evidence.record("apply accepted", command_id=accepted["command_id"])
    evidence.record("apply command", last_state=poll_apply(origin, accepted["command_id"]))

    origin2, lock2 = wait_for_engine(layout.lock, next_version, RELAUNCH_TIMEOUT_S, escape, not_pid=lock.get("pid"))
    escape.check()
    evidence.record(
        "n+1 booted", origin=origin2, pid=lock2.get("pid"), apply_to_boot_s=round(time.monotonic() - applied_at, 1)
    )

    installed = bundle_version(layout.app)
    if installed != next_version:
        raise ProofError(f"the bundle at {layout.app} says {installed}, expected {next_version}")
    executable = str(_info_plist(layout.app).get("CFBundleExecutable"))
    if executable != ELECTRON_EXECUTABLE or not (layout.app / ELECTRON_FRAMEWORK).is_dir():
        raise ProofError(f"the bundle at {layout.app} is not the Electron app (CFBundleExecutable {executable!r})")
    status, after = http_json("GET", f"{origin2}/api/v1/update/check")
    if after.get("status") != "up-to-date":
        raise ProofError(f"update/check after apply: HTTP {status} {after}")
    markers = read_markers(layout.state_db)
    inode_after = layout.state_db.stat().st_ino
    if marker not in markers or inode_after != inode:
        raise ProofError(f"library continuity: markers {markers}, inode {inode} -> {inode_after}")
    evidence.record(
        "n+1 verified",
        bundle_version=installed,
        executable=executable,
        check_after=after["status"],
        markers=markers,
        inode=inode_after,
        data_dir=str(layout.data),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-app", type=Path, required=True, help="the proof build N (Open DJ.app)")
    parser.add_argument("--feed", type=Path, required=True, help="N+1's output dir (zip, latest-mac.yml, latest.json)")
    parser.add_argument("--expect-version", required=True, help="N+1's version")
    parser.add_argument("--root", type=Path, required=True, help="fresh throwaway folder under $HOME")
    parser.add_argument(
        "--port", type=int, required=True, help="the port baked into the builds' OPENDJ_UPDATE_FEED_URL"
    )
    parser.add_argument(
        "--evidence", type=Path, required=True, help="where to write the evidence JSON (outside --root)"
    )
    parser.add_argument(
        "--n-version", default=None, help="the app_version N's engine reports (default: N's bundle version)"
    )
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="N is a Tauri build; N+1 arrives as the Electron app through the Tauri updater (latest.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    layout = Layout(
        args.root.expanduser().resolve(), bridge=args.bridge, account_home=Path.home() if args.bridge else None
    )
    evidence = Evidence()
    servers: list[subprocess.Popen] = []
    failure: str | None = None
    try:
        check_root(layout.root, Path.home())
        check_bridge_account(layout)
    except ProofError as error:
        # Refused before anything was created: nothing of ours to clean, and a
        # pre-existing folder must never be deleted.
        args.evidence.write_text(json.dumps({"failure": str(error), "steps": []}, indent=2) + "\n", encoding="utf-8")
        print(f"[ERROR] {error}", file=sys.stderr, flush=True)
        return 1
    try:
        run_proof(args, layout, evidence, servers)
    except Exception as error:  # every failure is recorded and fails the run; cleanup still runs
        failure = f"{type(error).__name__}: {error}"
        print(f"[ERROR] {failure}", file=sys.stderr, flush=True)
    finally:
        if layout.root.exists():
            try:
                notes = stop_everything(layout.root, servers[0] if servers else None)
                shutil.rmtree(layout.root)
                traces = remove_app_traces(layout.home) if layout.bridge else []
                evidence.record(
                    "cleanup", stopped=notes, root_exists=layout.root.exists(), account_traces_removed=traces
                )
            except (ProofError, OSError) as error:
                failure = failure or f"cleanup: {error}"
                print(f"[ERROR] cleanup: {error}", file=sys.stderr, flush=True)
        args.evidence.write_text(
            json.dumps({"failure": failure, "steps": evidence.steps}, indent=2) + "\n", encoding="utf-8"
        )
    if layout.root.exists():
        print(f"[ERROR] {layout.root} still exists after cleanup", file=sys.stderr)
        return 1
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
