"""Deterministic REC CLI capture polling coverage (issue #11824).

[if] capture is pending [then] REC waits for writing or raises, [else stop].
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from apps.sets import rec_cli

pytestmark = pytest.mark.requirement("SET-12")


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class ScriptedRead:
    statuses: list[dict[str, str]]
    calls: int = 0

    def __call__(self) -> dict[str, str]:
        assert self.calls < len(self.statuses), "unexpected extra recorder status read"
        status = self.statuses[self.calls]
        self.calls += 1
        return status


# Hand-authored status dictionaries below are synthetic read-dependency inputs.
@pytest.mark.parametrize("already_recording", [False, True])
def test_recording_returns_status_without_further_polling(already_recording: bool) -> None:
    """[if] capture records [then] return its status without another poll, [else stop]."""
    clock = FakeClock()
    recording = {"capture": "recording", "session_id": "writing-session"}
    initial = recording if already_recording else {"capture": "starting"}
    read = ScriptedRead([] if already_recording else [recording])

    result = rec_cli.wait_until_recording(
        "http://recorder.invalid",
        initial,
        read=read,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        poll_s=0.5,
    )

    assert result is recording
    assert read.calls == (0 if already_recording else 1)
    assert clock.sleeps == ([] if already_recording else [0.5])


def test_starting_timeout_names_last_status() -> None:
    """[if] starting exceeds the deadline [then] raise with the last status, [else stop]."""
    clock = FakeClock()
    last_status = {"capture": "starting", "session_id": "last-pending-session"}
    read = ScriptedRead([{"capture": "starting"}, {"capture": "starting"}, last_status])

    with pytest.raises(rec_cli.RecCommandFailed) as failure:
        rec_cli.wait_until_recording(
            "http://recorder.invalid",
            {"capture": "starting"},
            timeout_s=1.0,
            poll_s=0.5,
            read=read,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert "did not start within 1 s" in str(failure.value)
    assert str(last_status) in str(failure.value)
    assert read.calls == 3
    assert clock.now > 1.0
    assert clock.sleeps == [0.5, 0.5, 0.5]


@pytest.mark.parametrize("capture", ["failed", "stopped"])
def test_terminal_capture_raises_without_further_polling(capture: str) -> None:
    """[if] capture fails or stops [then] raise with its reason or status, [else stop]."""
    clock = FakeClock()
    terminal = {"capture": capture}
    if capture == "failed":
        terminal["capture_error"] = "master tap received no audio frames"
    read = ScriptedRead([terminal])

    with pytest.raises(rec_cli.RecCommandFailed) as failure:
        rec_cli.wait_until_recording(
            "http://recorder.invalid",
            {"capture": "starting"},
            read=read,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            poll_s=0.5,
        )

    expected = terminal["capture_error"] if capture == "failed" else str(terminal)
    assert expected in str(failure.value)
    assert read.calls == 1
    assert clock.sleeps == [0.5]


def test_waiting_permission_continues_until_recording() -> None:
    """[if] permission is pending then granted [then] return recording status, [else stop]."""
    clock = FakeClock()
    recording = {"capture": "recording", "session_id": "permission-granted-session"}
    read = ScriptedRead([{"capture": "waiting_permission"}, recording])

    result = rec_cli.wait_until_recording(
        "http://recorder.invalid",
        {"capture": "starting"},
        read=read,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        poll_s=0.5,
    )

    assert result is recording
    assert read.calls == 2
    assert clock.sleeps == [0.5, 0.5]
