"""Contract inputs are synthesized, never claimed as upstream release evidence.

[if] a watched gate changes [then] emit one wake per revision, [else stop].
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest

from scripts.lane_wakeup import GateUpdate, Kind, WakeWatcher, WatchSpec, iter_wakeups, main

pytestmark = pytest.mark.requirement("OPS-12748")

HEAD = "a" * 40
KINDS: frozenset[Kind] = frozenset({"ci.completed", "review.posted", "queue.result"})
CI_TERMINAL = (
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "neutral",
    "skipped",
    "action_required",
    "stale",
    "startup_failure",
)


def _spec(**changes: object) -> WatchSpec:
    values = asdict(WatchSpec("owner/project", 17, HEAD, KINDS))
    values.update(changes)
    return WatchSpec(**values)


def _update(**changes: object) -> GateUpdate:
    values = asdict(
        GateUpdate(
            1,
            "owner/project",
            17,
            HEAD,
            "ci.completed",
            "check:1",
            "attempt:1",
            "success",
            "https://github.com/owner/project/actions/runs/1",
        ),
    )
    values.update(changes)
    return GateUpdate(**values)


@pytest.mark.parametrize("state", CI_TERMINAL)
def test_ci_completion_wakes_once_including_nonpassing_results(state: str) -> None:
    update = _update(state=state)
    wakes = list(
        iter_wakeups(
            [
                _update(state="queued"),
                _update(state="in_progress"),
                update,
                update,
            ],
            _spec(),
        )
    )
    assert len(wakes) == 1
    assert wakes[0].state == state


def test_review_revisions_and_redelivery() -> None:
    first = _update(kind="review.posted", state="posted", source_id="review:3")
    second = replace(first, change_id="revision:2")
    updates = [replace(first, state="queued"), replace(first, state="in_progress"), first, second, first, second]
    assert [wake.change_id for wake in iter_wakeups(updates, _spec())] == [
        "attempt:1",
        "revision:2",
    ]


@pytest.mark.parametrize("state", ["merged", "failed", "cancelled", "removed"])
def test_queue_pending_and_terminal_results(state: str) -> None:
    result = _update(kind="queue.result", state=state)
    updates = [replace(result, state="queued"), replace(result, state="testing"), result, result]
    assert [wake.state for wake in iter_wakeups(updates, _spec())] == [state]


@pytest.mark.parametrize(
    "changes",
    [
        {"repo": "other/project"},
        {"pr": 18},
        {"head_sha": "b" * 40},
        {"kind": "review.posted", "state": "posted"},
    ],
)
def test_unwatched_updates_do_not_wake_or_consume_identity(changes: dict[str, object]) -> None:
    watcher = WakeWatcher(_spec(kinds=frozenset({"ci.completed"})))
    assert watcher.observe(_update(**changes)) is None
    assert watcher.observe(_update()) is not None


def test_distinct_sources_and_revisions_with_equal_conclusions_wake() -> None:
    first = _update()
    second = replace(first, change_id="attempt:2")
    third = replace(first, source_id="check:2")
    wakes = list(iter_wakeups([first, second, first, third, second], _spec()))
    assert [(wake.source_id, wake.change_id) for wake in wakes] == [
        ("check:1", "attempt:1"),
        ("check:1", "attempt:2"),
        ("check:2", "attempt:1"),
    ]
    assert len({wake.event_id for wake in wakes}) == 3


def test_conflicting_terminal_revision_fails() -> None:
    watcher = WakeWatcher(_spec())
    assert watcher.observe(_update()) is not None
    with pytest.raises(ValueError, match="conflicting state"):
        watcher.observe(_update(state="failure"))


def test_wake_identity_is_stable_complete_and_process_local() -> None:
    update = _update()
    first = WakeWatcher(_spec()).observe(update)
    second = WakeWatcher(_spec()).observe(update)
    assert first == second
    assert first is not None
    payload = asdict(first)
    assert payload.pop("event_id") == ("sha256:bbd95299d39ee3a31ed107c5732a405e532b647228bee46ef32af3b266d33a3e")
    assert payload == asdict(update)
    assert _spec(repo="OWNER/PROJECT", head_sha=HEAD.upper()) == _spec()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"kind": "comment"},
        {"state": "passed"},
        {"kind": "queue.result", "state": "success"},
        {"kind": "review.posted", "state": "success"},
        {"source_id": ""},
        {"change_id": " "},
        {"source_id": "id\nsecret"},
        {"repo": "project"},
        {"repo": "../project"},
        {"pr": 0},
        {"pr": True},
        {"pr": "17"},
        {"head_sha": "abc"},
        {"head_sha": "z" * 40},
        {"url": "relative"},
        {"url": "https://user:password@example.com/"},
        {"state": None},
    ],
)
def test_invalid_update_fails(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _update(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"repo": "invalid"},
        {"pr": -1},
        {"head_sha": "short"},
        {"kinds": frozenset()},
        {"kinds": frozenset({"unknown"})},
        {"kinds": "ci.completed"},
    ],
)
def test_invalid_watch_spec_fails(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _spec(**changes)


def _command(*args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "scripts.lane_wakeup",
        "--repo",
        "owner/project",
        "--pr",
        "17",
        "--head-sha",
        HEAD,
        "--kind",
        "ci.completed",
        "--kind",
        "review.posted",
        "--kind",
        "queue.result",
        *args,
    ]


def _line(update: GateUpdate) -> str:
    return json.dumps(asdict(update)) + "\n"


@pytest.mark.parametrize(
    "kind,state",
    [
        ("ci.completed", "failure"),
        ("review.posted", "posted"),
        ("queue.result", "merged"),
    ],
)
def test_real_cli_flushes_before_input_closes(kind: str, state: str) -> None:
    """Keep stdin open while a bounded reader awaits actual flushed output."""
    process = subprocess.Popen(
        _command("--once"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[2],
    )
    reader_started = Event()
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    stdout = process.stdout

    def read_wake() -> str:
        reader_started.set()
        return stdout.readline()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read_wake)
        try:
            assert reader_started.wait(timeout=10)
            pending = _update(kind=kind, state="queued")
            process.stdin.write(_line(pending))
            process.stdin.flush()
            terminal = replace(pending, state=state)
            process.stdin.write(_line(terminal))
            process.stdin.flush()
            wake = json.loads(future.result(timeout=10))
            assert wake == asdict(next(iter_wakeups([terminal], _spec())))
            assert not process.stdin.closed
            assert process.wait(timeout=10) == 0
            assert process.stdout.read() == ""
            assert process.stderr.read() == ""
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


@pytest.mark.parametrize(
    "args,data,expected",
    [
        ([], "", 0),
        (["--once"], "", 3),
        (["--once"], _line(_update(repo="other/project")), 3),
        ([], "{bad json}\n", 3),
        ([], "{}\n", 3),
        ([], "[]\n", 3),
        ([], "\n", 3),
        ([], '{"schema_version": 99}\n', 3),
        ([], json.dumps({**asdict(_update()), "extra": 1}) + "\n", 3),
        ([], json.dumps({**asdict(_update()), "source_id": ""}) + "\n", 3),
        ([], _line(_update()) + _line(_update(change_id="attempt:2")), 0),
        (["--pr", "0"], "", 2),
        (["--kind", "bogus"], "", 2),
        (["--head-sha", "short"], "", 2),
    ],
)
def test_cli_exit_contract(args: list[str], data: str, expected: int) -> None:
    result = subprocess.run(
        _command(*args), input=data, capture_output=True, text=True, encoding="utf-8", timeout=10, check=False
    )
    assert result.returncode == expected
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    if data.startswith(_line(_update())) and expected == 0:
        assert len(rows) == 2
    else:
        assert rows == []
    assert bool(result.stderr) == (expected != 0)


@pytest.mark.parametrize("boundary", ["input", "output"])
def test_broken_stream_is_explicit_failure(monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    class BrokenInput:
        def __iter__(self) -> Iterator[str]:
            raise OSError("broken input")

    class BrokenOutput:
        def write(self, text: str) -> int:
            raise BrokenPipeError("broken output")

        def close(self) -> None:
            raise BrokenPipeError("broken output flush")

    monkeypatch.setattr(sys, "stdin", BrokenInput() if boundary == "input" else io.StringIO(_line(_update())))
    monkeypatch.setattr(sys, "stdout", BrokenOutput() if boundary == "output" else io.StringIO())
    assert main(_command()[3:]) == 3


def test_real_cli_broken_output_preserves_exit_three() -> None:
    process = subprocess.Popen(
        _command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"
    )
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    try:
        process.stdout.close()
        process.stdin.write(_line(_update()))
        process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=10) == 3
        assert "lane-wakeup:" in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
