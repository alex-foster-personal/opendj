"""LINK-122 / LINK-123: the engine provides Carabiner itself, supervised, and says who provides it.

The pure decisions run on real inputs; the spawn and supervise loop runs a REAL child process
(``fake_carabiner_main``, launched through an executable ``Carabiner`` file exactly as the engine
launches the real binary) that speaks Carabiner's ``status`` protocol, so spawn, kill, respawn,
reuse and terminate are exercised for real on Linux. The real download, hdiutil and codesign path
needs a Mac (tests/ableton_link/test_carabiner_provider.py covers what Linux can run).

- [if] the engine spawns a Carabiner while one already answers on the port [then] fail
- [if] a spawned child is not answering ``status`` when provision() returns [then] fail
- [if] the child does not carry AF_SERVICE_ID=com.af.opendj.carabiner [then] fail
- [if] a killed child is not respawned with a new pid that answers [then] fail
- [if] a child exits after stop() and is respawned anyway [then] fail
- [if] stop() leaves the child running [then] fail
- [if] the respawn back-off grows without a cap or starts above its first step [then] fail
- [if] a binary that exits or never answers is not a named failure (spawn_failed, not_answering) [then] fail
- [if] the App Store sandbox, a foreign port holder or a non-macOS host spawns anything [then] fail, [else stop].
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from apps.ableton_link.carabiner_provider import CarabinerProvisionError
from apps.ableton_link.carabiner_supervisor import (
    RESPAWN_BACKOFF_S,
    SERVICE_ID,
    CarabinerSupervisor,
    answers_status,
    decide_provision,
    respawn_delay_s,
)
from tests.ableton_link.carabiner_sim import SimCarabiner
from tests.ableton_link.fake_carabiner_main import write_fake_binary

pytestmark = [pytest.mark.requirement("LINK-122"), pytest.mark.requirement("LINK-123")]

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<pid>/environ")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(predicate: Callable[[], bool], deadline_s: float = 8.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        if predicate():
            return time.monotonic() - started
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {deadline_s} s")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); /proc says whether it is really running.
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().split()[2] != "Z"


class Installs:
    """The install seam: returns the fake binary and counts how often the engine asked."""

    def __init__(self, binary: Path) -> None:
        self.binary, self.calls = binary, 0

    def __call__(self) -> Path:
        self.calls += 1
        return self.binary


def _supervisor(port: int, install: Callable[[], Path], **kwargs: object) -> CarabinerSupervisor:
    return CarabinerSupervisor(
        port, install=install, platform="darwin", sandboxed=lambda: False,
        backoff=(0.2, 0.4, 0.8), **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def serving(tmp_path: Path) -> Iterator[tuple[CarabinerSupervisor, Installs]]:
    install = Installs(write_fake_binary(tmp_path, "serve"))
    supervisor = _supervisor(_free_port(), install)
    yield supervisor, install
    supervisor.stop()


# ----------------------------------------------------------------------------- pure decisions
@pytest.mark.parametrize(
    ("port_open", "answers", "platform", "sandboxed", "expected"),
    [
        (True, True, "darwin", False, "reuse"),
        (True, True, "linux", False, "reuse"),  # an external Carabiner is used on any OS
        (True, True, "darwin", True, "reuse"),
        (True, False, "darwin", False, "refuse_port_held"),
        (False, False, "linux", False, "refuse_platform"),
        (False, False, "darwin", True, "refuse_sandbox"),
        (False, False, "darwin", False, "spawn"),
    ],
)
def test_decide_provision_reuses_spawns_or_refuses_by_name(
    port_open: bool, answers: bool, platform: str, sandboxed: bool, expected: str
) -> None:
    assert decide_provision(port_open=port_open, answers_status=answers, platform=platform, sandboxed=sandboxed) == expected


def test_respawn_back_off_starts_at_its_first_step_and_is_capped() -> None:
    delays = [respawn_delay_s(n) for n in range(1, 12)]
    assert delays[0] == RESPAWN_BACKOFF_S[0]
    assert delays == sorted(delays), delays
    assert max(delays) == RESPAWN_BACKOFF_S[-1] and delays[-1] == delays[-2] == RESPAWN_BACKOFF_S[-1]


def test_answers_status_needs_a_status_line_not_just_an_open_port() -> None:
    sim = SimCarabiner()
    try:
        assert answers_status(sim.port) is True  # positive control: a real status line
    finally:
        sim.close()
    mute = socket.socket()
    mute.bind(("127.0.0.1", 0))
    mute.listen(1)
    try:
        assert answers_status(int(mute.getsockname()[1]), timeout_s=0.3) is False  # open, never answers
    finally:
        mute.close()


# ----------------------------------------------------------------------------- real child process
@linux_only
def test_spawns_a_child_that_answers_with_the_service_id(serving: tuple[CarabinerSupervisor, Installs]) -> None:
    supervisor, install = serving
    assert supervisor.provision() == "spawn"
    status = supervisor.status()
    assert status.label == "supervised" and status.pid is not None, status
    assert answers_status(supervisor.port), "provision() returned before the child answered status"
    assert install.calls == 1
    environ = Path(f"/proc/{status.pid}/environ").read_bytes().split(b"\0")
    assert f"AF_SERVICE_ID={SERVICE_ID}".encode() in environ
    cmdline = Path(f"/proc/{status.pid}/cmdline").read_bytes().split(b"\0")
    assert [b"--port", str(supervisor.port).encode(), b"--daemon"] == cmdline[-4:-1], cmdline


def test_reuses_a_carabiner_that_already_answers_and_spawns_nothing(tmp_path: Path) -> None:
    sim = SimCarabiner()
    install = Installs(write_fake_binary(tmp_path, "serve"))
    supervisor = _supervisor(sim.port, install)
    try:
        assert supervisor.provision() == "reuse"
        status = supervisor.status()
        assert status.label == "external" and status.pid is None, status
        assert install.calls == 0, "the engine fetched or spawned Carabiner although one was answering"
    finally:
        supervisor.stop()
        sim.close()


def test_a_killed_child_is_respawned_and_answers_again(serving: tuple[CarabinerSupervisor, Installs]) -> None:
    supervisor, _install = serving
    supervisor.provision()
    first = supervisor.status().pid
    assert first is not None
    os.kill(first, signal.SIGKILL)
    took = _wait_for(lambda: supervisor.status().label == "supervised" and supervisor.status().pid not in (None, first))
    assert answers_status(supervisor.port)
    status = supervisor.status()
    assert status.label == "supervised" and status.restarts == 1, status
    print(f"[INFO] respawned as pid {status.pid} {took:.2f} s after SIGKILL of {first}")


def test_stop_terminates_the_child_and_nothing_respawns(serving: tuple[CarabinerSupervisor, Installs]) -> None:
    supervisor, _install = serving
    supervisor.provision()
    pid = supervisor.status().pid
    assert pid is not None and _alive(pid)
    supervisor.stop()
    assert not _alive(pid), "engine shutdown left Carabiner running"
    time.sleep(1.0)  # longer than the whole test back-off: a respawn would have happened by now
    assert not answers_status(supervisor.port), "a Carabiner came back after stop()"
    with pytest.raises(CarabinerProvisionError) as refused:
        supervisor.provision()
    assert refused.value.code == "spawn_failed"


def test_a_binary_that_exits_at_once_is_spawn_failed_with_its_log(tmp_path: Path) -> None:
    supervisor = _supervisor(_free_port(), Installs(write_fake_binary(tmp_path, "exit")))
    with pytest.raises(CarabinerProvisionError) as failed:
        supervisor.provision()
    assert failed.value.code == "spawn_failed"
    assert "code 3" in str(failed.value) and "refusing to start" in str(failed.value)
    assert supervisor.status().label == "failed:spawn_failed"
    assert failed.value.user_message.startswith("[carabiner_spawn_failed] ")


def test_a_binary_that_never_answers_is_not_answering_and_is_stopped(tmp_path: Path) -> None:
    supervisor = _supervisor(_free_port(), Installs(write_fake_binary(tmp_path, "silent")), answer_deadline_s=1.0)
    with pytest.raises(CarabinerProvisionError) as failed:
        supervisor.provision()
    assert failed.value.code == "not_answering"
    assert supervisor.status().label == "failed:not_answering"
    assert supervisor.status().pid is None, "a child that never answered was left running"


def test_sandbox_port_holder_and_linux_never_spawn(tmp_path: Path) -> None:
    install = Installs(write_fake_binary(tmp_path, "serve"))
    sandboxed = CarabinerSupervisor(_free_port(), install=install, platform="darwin", sandboxed=lambda: True)
    with pytest.raises(CarabinerProvisionError) as refused:
        sandboxed.provision()
    assert refused.value.code == "sandboxed" and "App Store build" in str(refused.value)

    mute = socket.socket()
    mute.bind(("127.0.0.1", 0))
    mute.listen(1)
    try:
        held = _supervisor(int(mute.getsockname()[1]), install)
        with pytest.raises(CarabinerProvisionError) as port_held:
            held.provision()
        assert port_held.value.code == "port_held"
    finally:
        mute.close()

    linux = CarabinerSupervisor(_free_port(), install=install, platform="linux", sandboxed=lambda: False)
    assert linux.provision() == "refuse_platform"
    assert linux.status().label == "unmanaged" and "only on macOS" in (linux.status().detail or "")
    assert install.calls == 0, "a refused host fetched Carabiner"


def test_an_install_failure_is_recorded_by_code_and_a_retry_can_succeed(tmp_path: Path) -> None:
    binary = write_fake_binary(tmp_path, "serve")
    attempts: list[int] = []

    def offline_then_online() -> Path:
        attempts.append(1)
        if len(attempts) == 1:
            raise CarabinerProvisionError("download_failed", "offline")
        return binary

    supervisor = _supervisor(_free_port(), offline_then_online)
    try:
        with pytest.raises(CarabinerProvisionError):
            supervisor.provision()
        assert supervisor.status().label == "failed:download_failed"
        assert supervisor.provision() == "spawn"
        assert supervisor.status().label == "supervised"
    finally:
        supervisor.stop()
