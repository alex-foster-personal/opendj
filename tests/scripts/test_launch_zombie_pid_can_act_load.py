"""Load and contract checks for launch::pid_can_act_rejects_a_zombie_child (#11382)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from apps.shared import platform_paths

CRATE = platform_paths.PROJECT_ROOT / "apps" / "desktop" / "src-tauri"
LAUNCH_RS = CRATE / "src" / "launch.rs"
TEST_NAME = "launch::tests::pid_can_act_rejects_a_zombie_child"
PARALLEL_RUNS = 12
REQUIRE_ENV = "MDT_REQUIRE_DESKTOP_CARGO"
# opendj-desktop shares one target/; build once under this lock, then run the binary concurrently.
_CARGO_INVOCATION_LOCK = threading.Lock()
_TEST_EXECUTABLES: list[str] = []


def _desktop_build_blocker() -> str | None:
    """Return a reason when opendj-desktop cannot be cargo-tested here."""
    if shutil.which("cargo") is None:
        return "no cargo here, so opendj-desktop cannot be built from this checkout"
    if shutil.which("pkg-config") is None:
        return "no pkg-config here, so Tauri system libs cannot be probed"
    for pkg in ("gdk-3.0", "libsoup-3.0", "webkit2gtk-4.1"):
        if subprocess.run(["pkg-config", "--exists", pkg], check=False).returncode != 0:
            return f"pkg-config missing {pkg}"
    return None


def _ensure_payload_dir() -> None:
    (CRATE / "payload").mkdir(exist_ok=True)


def _require_desktop_cargo_or_skip() -> None:
    """Fail fast in the test thread when Tauri system libs are missing."""
    blocker = _desktop_build_blocker()
    if blocker is None:
        return
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{blocker}, and {REQUIRE_ENV}=1 requires the build")
    pytest.skip(f"UNAVAILABLE: {blocker}; contracts CI builds desktop and reruns this")


def _build_test_executables() -> list[str]:
    """Build the desktop test binary once under the lock; return its executable paths."""
    _ensure_payload_dir()
    with _CARGO_INVOCATION_LOCK:
        if _TEST_EXECUTABLES:
            return _TEST_EXECUTABLES
        proc = subprocess.run(
            ["cargo", "test", "--locked", "--no-run", "--message-format=json"],
            cwd=CRATE,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"cargo test --no-run failed (exit {proc.returncode}):\n{proc.stderr}")
        _TEST_EXECUTABLES.extend(_test_executables_from_cargo_json(proc.stdout))
        if not _TEST_EXECUTABLES:
            raise AssertionError("cargo test --no-run reported no test executable")
        return _TEST_EXECUTABLES


def _test_executables_from_cargo_json(stdout: str) -> list[str]:
    """Pick test-profile executables out of cargo --message-format=json output."""
    found: list[str] = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        msg = json.loads(line)
        if msg.get("reason") != "compiler-artifact" or not msg.get("profile", {}).get("test"):
            continue
        exe = msg.get("executable")
        if exe and exe not in found:
            found.append(exe)
    return found


def _run_zombie_test() -> None:
    """Run the prebuilt desktop unit test once outside the build lock; raise on failure."""
    ran = False
    for exe in _build_test_executables():
        proc = subprocess.run(
            [exe, TEST_NAME, "--exact"],
            cwd=CRATE,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"{TEST_NAME} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
        ran = ran or "1 passed" in proc.stdout
    if not ran:
        raise AssertionError(f"{TEST_NAME} did not run in any test executable")


def _zombie_test_block(launch_rs: str) -> str:
    """Source of pid_can_act_rejects_a_zombie_child, without following helpers."""
    match = re.search(
        r"fn pid_can_act_rejects_a_zombie_child\(\) \{.*?\n    \}\n",
        launch_rs,
        flags=re.DOTALL,
    )
    assert match is not None, "zombie test must exist in launch.rs"
    return match.group(0)


def test_pid_can_act_zombie_green_under_parallel_cargo_on_linux() -> None:
    """[if] parallel cargo test on a loaded runner [then] pid_can_act zombie test stays green, [else stop]."""
    if sys.platform != "linux":
        pytest.skip("UNAVAILABLE: parallel load gate is linux-light runner scoped")
    _require_desktop_cargo_or_skip()
    _build_test_executables()
    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        futures = [pool.submit(_run_zombie_test) for _ in range(PARALLEL_RUNS)]
        for fut in as_completed(futures):
            fut.result()


def test_pid_can_act_zombie_waits_for_readiness_not_fixed_sleep() -> None:
    """[if] the child has not become a zombie yet [then] the test waits on process_state, [else stop]."""
    text = LAUNCH_RS.read_text(encoding="utf-8")
    block = _zombie_test_block(text)
    assert "spawn_zombie_child()" in block
    assert "while !matches!(process_state(child_pid), Some('Z') | Some('z'))" in block
    assert "std::time::Instant::now() < deadline" in block
    assert "thread::sleep(Duration::from_millis(50));" not in block


def test_pid_can_act_zombie_reap_tail_unchanged() -> None:
    """[if] a zombie answers pid_alive and fails pid_can_act [then] waitpid reaps it dead, [else stop]."""
    _require_desktop_cargo_or_skip()
    text = LAUNCH_RS.read_text(encoding="utf-8")
    block = _zombie_test_block(text)
    assert 'assert!(pid_alive(child_pid), "zombie should answer kill -0")' in block
    assert '!pid_can_act(child_pid), "zombie must not count as acting"' in block
    assert "libc::waitpid(raw_pid, &mut status, 0)" in block
    assert "assert!(!pid_alive(child_pid));" in block
    _run_zombie_test()


def test_cargo_json_parser_picks_test_executables_only() -> None:
    """[if] cargo emits build and test artifacts [then] only test-profile executables are run, [else stop]."""
    lines = [
        json.dumps({"reason": "compiler-artifact", "profile": {"test": False}, "executable": "/tmp/app"}),
        "not json",
        json.dumps({"reason": "compiler-artifact", "profile": {"test": True}, "executable": "/tmp/app-test"}),
        json.dumps({"reason": "compiler-artifact", "profile": {"test": True}, "executable": None}),
        json.dumps({"reason": "build-finished", "success": True}),
    ]
    assert _test_executables_from_cargo_json("\n".join(lines)) == ["/tmp/app-test"]


def test_parallel_runs_do_not_hold_build_lock() -> None:
    """[if] the parallel gate runs the zombie test [then] the cargo lock is not held around the run, [else stop]."""
    src = Path(__file__).read_text(encoding="utf-8")
    run_body = src[src.index("def _run_zombie_test") : src.index("def _zombie_test_block")]
    assert "_CARGO_INVOCATION_LOCK" not in run_body
