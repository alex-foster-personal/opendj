"""scripts/ratchet_scorecard.py: gate keys, classes, fix kinds, scoring and verdicts.

The quality line and the pytest summary line are copied from real ci.yml job logs
(quality job 116355559982 and a pytest fast lane shard, Thu 15 Oct 2026).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

import pytest

from scripts import ratchet_scorecard as rs
from tests.scripts.ratchet_scorecard_helpers import (
    CMDK_FIX,
    CMDK_RED,
    FIXTURES,
    MAIN_QUALITY,
    T0,
    _cmdk_api,
    _complete,
    _head,
    _job,
)


def test_a_queue_draft_is_authenticated_not_named() -> None:
    """ci.yml treats a run as a Trunk queue draft only when trunk-io[bot] opened a trunk-merge/
    head from this repository; a contributor branch with that prefix is an ordinary PR."""

    def run(branch: str, login: str, repo: str = rs.REPO) -> dict:
        return {"head_branch": branch, "actor": {"login": login}, "head_repository": {"full_name": repo}}

    assert rs._is_queue_draft(run("trunk-merge/pr-1/x", rs.TRUNK_BOT))
    assert not rs._is_queue_draft(run("trunk-merge/pr-1/x", "someone"))
    assert not rs._is_queue_draft(run("trunk-merge/pr-1/x", rs.TRUNK_BOT, "fork/music-dj-tools"))
    assert not rs._is_queue_draft(run("af--x", rs.TRUNK_BOT))
    # A spoofed branch's reds are PR evidence: it counts as a branch, not a queue draft.
    spoof = rs.Head("ci.yml", "trunk-merge/evil", "s1", T0, frozenset({"quality:x"}), True)
    gate = rs.score(_complete([spoof]), {}, lambda red, fix: None, T0 + dt.timedelta(hours=5))["quality:x"]
    assert (gate.branches, gate.queue_draft_reds) == ({"trunk-merge/evil"}, 0)


def test_a_window_holding_only_a_queue_draft_is_unmeasured() -> None:
    draft = _complete([_head("trunk-merge/pr-1/x", "q1", 1)])
    data = rs.Collected(draft, Counter(), 2, 0, 0)
    assert rs.owned_shas(draft, None) == []
    assert rs.unmeasured_window(data) == "no PR head in the window has a measured verdict from every required workflow"
    # Control: an ordinary PR head with a full verdict is measured.
    assert rs.unmeasured_window(rs.Collected(_complete([_head("a", "s1", 1)]), Counter(), 2, 0, 0)) is None


def test_a_main_report_counts_regressions_never_unknowns() -> None:
    """A metric main could not measure is no debt on it: only REGRESSION lines count."""
    job = {**_job("quality ratchet (lint debt)", "success", 9), "steps": []}
    log = (FIXTURES / MAIN_QUALITY / "log-116950591706.txt").read_text()
    unknown = "2026-10-16T11:51:23.3251132Z [quality] UNKNOWN           vulture.dead_code: no report\n"
    assert rs._main_report_keys([job], {9: log + unknown}) == frozenset({"quality:mypy.errors_tests"})
    # Control: a failed quality job's keys come from judge_jobs, never from here.
    assert rs._main_report_keys([{**job, "conclusion": "failure"}], {9: log}) == frozenset()


def test_a_partial_fix_horizon_head_makes_the_fix_unreadable() -> None:
    """A consulted head whose red-judging workflow passed but whose companion timed out has
    no full verdict, yet may be where the red cleared, so the fix is unreadable."""
    key = "quality:ruff.style"
    timed_out = rs.Head("e2e.yml", "a", "s2", T0 + dt.timedelta(hours=2), frozenset(), False)
    heads = [*_complete([_head("a", "s1", 1, key)]), _head("a", "s2", 2), timed_out]
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
    assert (gate.unresolved, dict(gate.fixes)) == (0, {"unreadable": 1})
    # Controls: a superseded head whose ci.yml run was cancelled said nothing, and one that
    # judged the key red again did not clear it; both leave the later fix readable.
    for s2 in (_head("a", "s2", 2, measured=False, cancelled=True), _head("a", "s2", 2, key)):
        heads = [*_complete([_head("a", "s1", 1, key), _head("a", "s3", 3)]), s2, timed_out]
        gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
        assert (gate.unresolved, dict(gate.fixes)) == (0, {"code": 1})


def test_held_time_runs_past_a_partial_head() -> None:
    """A head with no full verdict (a companion timed out) gave no verdict, so the hold ends
    at the next full verdict, not at the partial head."""
    key = "quality:ruff.style"
    timed_out = rs.Head("e2e.yml", "a", "s2", T0 + dt.timedelta(hours=2), frozenset(), False)
    heads = [*_complete([_head("a", "s1", 1, key), _head("a", "s3", 4)]), _head("a", "s2", 2), timed_out]
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
    assert gate.held_hours == 3.0
    # Control: a full verdict at s2 ends the hold there.
    heads = _complete([_head("a", "s1", 1, key), _head("a", "s2", 2), _head("a", "s3", 4)])
    assert rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key].held_hours == 1.0


def test_a_partial_head_before_a_later_red_stays_the_earlier_reds_blind_spot() -> None:
    """s1 red, s2 partial, s3 red again, s4 green: s3's red proves only its own state, so s2
    may be where s1 cleared and regressed, and s1's fix is unreadable; s3 owns s4's fix."""
    key = "quality:ruff.style"
    timed_out = rs.Head("e2e.yml", "a", "s2", T0 + dt.timedelta(hours=2), frozenset(), False)
    reds = [_head("a", "s1", 1, key), _head("a", "s3", 3, key), _head("a", "s4", 4)]
    heads = [*_complete(reds), _head("a", "s2", 2), timed_out]
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
    assert (gate.unresolved, dict(gate.fixes)) == (0, {"unreadable": 1, "code": 1})
    # The later red itself is no clearing candidate, so an unread log on it blinds nothing.
    import dataclasses

    unread = [reds[0], dataclasses.replace(reds[1], log_jobs=1, unread_logs=1), reds[2]]
    gate = rs.score(
        [*_complete(unread), _head("a", "s2", 2), timed_out],
        {},
        lambda a, b: ["apps/x.py"],
        T0 + dt.timedelta(hours=10),
    )[key]
    assert (gate.unresolved, dict(gate.fixes)) == (0, {"unreadable": 1, "code": 1})
    # Control: with s2's runs all cancelled (superseded, it said nothing), s1 books nothing.
    cancelled = [
        _head("a", "s2", 2, measured=False, cancelled=True),
        rs.Head("e2e.yml", "a", "s2", T0 + dt.timedelta(hours=2), frozenset(), False, cancelled=True),
    ]
    heads = [*_complete(reds), *cancelled]
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
    assert (gate.unresolved, dict(gate.fixes)) == (0, {"code": 1})
    # Control: with no later red, the partial head is s1's own blind spot.
    heads = [*_complete([reds[0], reds[2]]), _head("a", "s2", 2), timed_out]
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
    assert (gate.unresolved, dict(gate.fixes)) == (0, {"unreadable": 1})
    # A later red that never clears: s2 is still s1's blind spot, and s3 is unresolved.
    heads = [*_complete(reds[:2]), _head("a", "s2", 2), timed_out]
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10))[key]
    assert (gate.unresolved, dict(gate.fixes)) == (1, {"unreadable": 1})
    # s1 red, s2 red, s3 partial, scoring s1's window alone: s2's red proves s1 did not clear
    # by then, and s3 is s2's to judge, so s1 is unresolved, never blinded by s3.
    late = rs.Head("e2e.yml", "a", "p3", T0 + dt.timedelta(hours=3), frozenset(), False)
    heads = [*_complete([_head("a", "s1", 1, key), _head("a", "s2", 2, key)]), _head("a", "p3", 3), late]
    window = (T0, T0 + dt.timedelta(hours=1, minutes=30))
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10), window=window)[key]
    assert (gate.unresolved, dict(gate.fixes)) == (1, {})
    # Control: in s2's own window, p3 is s2's blind spot.
    window = (T0 + dt.timedelta(hours=1, minutes=30), T0 + dt.timedelta(hours=10))
    gate = rs.score(heads, {}, lambda a, b: ["apps/x.py"], T0 + dt.timedelta(hours=10), window=window)[key]
    assert (gate.unresolved, dict(gate.fixes)) == (0, {"unreadable": 1})


def test_a_transient_log_failure_is_retried_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5xx on the artifacts host does heal, so the fetch is retried rather than voiding
    the window; the waits between attempts are the backoff, measured, not assumed."""
    waits: list[float] = []
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    calls: list[str] = []

    def api(path: str) -> str:
        calls.append(path)
        if len(calls) < 3:
            raise RuntimeError("gh: HTTP 502 (Bad Gateway)")
        return "2026-10-17T01:00:00Z [quality] OK\n"

    job = {"id": 42, "name": "quality ratchet", "conclusion": "failure"}
    assert rs._job_log(api, job) == (42, "2026-10-17T01:00:00Z [quality] OK\n")
    assert len(calls) == 3
    assert waits == [0.5, 1.5]


def test_a_transient_jobs_listing_failure_is_retried_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """One 502 on a jobs listing used to abort the whole collection inside ThreadPoolExecutor.map,
    before `unmeasured_window` ever ran: no exit code and no cause counts, which reads on the
    board as "produced nothing". The listing is an ordinary read, so it retries like a log fetch."""
    waits: list[float] = []
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    calls: list[str] = []
    inner = _cmdk_api(calls)
    failed_once: set[str] = set()

    def api(path: str) -> str:
        if "/jobs?" in path and path not in failed_once:
            failed_once.add(path)
            calls.append(path)
            raise RuntimeError("gh: HTTP 502 (Bad Gateway)")
        return inner(path)

    since, until = dt.datetime(2026, 10, 14, 20, 40, tzinfo=dt.UTC), dt.datetime(2026, 10, 14, 22, 35, tzinfo=dt.UTC)
    data = rs.collect(api, since, until, workers=1)

    assert rs.unmeasured_window(data) is None
    assert sorted(set(h.sha[:9] for h in data.heads if not h.cancelled)) == [CMDK_FIX, CMDK_RED]
    assert len(failed_once) == 4  # every failed run's listing, each retried exactly once
    assert waits == [0.5] * len(failed_once)


def test_a_permanent_jobs_listing_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gone run answers 404 forever: retrying it would spend three calls on a token every
    other thread shares, so the first answer stands and the caller sees the real cause."""
    waits: list[float] = []
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    calls: list[str] = []

    def api(path: str) -> str:
        calls.append(path)
        raise RuntimeError("gh: HTTP 404 Not Found (https://api.github.com)")

    with pytest.raises(RuntimeError, match="404"):
        rs._api_json(api, f"repos/{rs.REPO}/actions/runs/1/attempts/1/jobs?per_page=100")
    assert (len(calls), waits) == (1, [])


def test_a_control_bearing_pytest_name_is_not_a_gate_key() -> None:
    """`\\S` accepts ESC, so without a path-byte class `tests/foo<OSC>.py` parsed as a module
    and carried the control bytes into the scorecard row."""
    assert rs.pytest_keys(["FAILED tests/foo\x1b]0;x\x07.py::test_bar"]) == []
    assert rs.pytest_keys(["FAILED tests/foo.py::test_bar"]) == ["pytest:tests/foo.py"]


def test_an_expired_log_is_read_once_and_stays_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    """404/410 is the expired-log answer the issue counts. It cannot change on a retry, so
    a retry there would spend API calls for nothing."""
    waits: list[float] = []
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    calls: list[str] = []

    def api(path: str) -> str:
        calls.append(path)
        raise RuntimeError("gh: HTTP 410 (Gone)")

    assert rs._job_log(api, {"id": 7}) == (7, None)
    assert (len(calls), waits) == (1, [])


def test_an_unrecognised_log_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-attempt read is the contract for anything the classifier does not know:
    an unknown error multiplied by three is three unknown errors, not evidence."""
    waits: list[float] = []
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    calls: list[str] = []

    def api(path: str) -> str:
        calls.append(path)
        raise RuntimeError("unreadable")

    assert rs._job_log(api, {"id": 9}) == (9, None)
    assert (len(calls), waits) == (1, [])


def test_a_persistently_transient_log_failure_stops_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry is bounded: a log that keeps answering 503 stays unread after three
    attempts instead of holding the run open."""
    waits: list[float] = []
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    calls: list[str] = []

    def api(path: str) -> str:
        calls.append(path)
        raise RuntimeError("gh: HTTP 503 (Service Unavailable)")

    assert rs._job_log(api, {"id": 11}) == (11, None)
    assert (len(calls), waits) == (3, [0.5, 1.5])


def test_a_log_fetch_asks_gh_to_keep_escapes_and_strips_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh api` refuses a log body that carries terminal escape sequences unless
    `--allow-escape-sequences` is passed, which would count a coloured pytest log as
    unreadable. The flag must be on the argv, and the escapes must not reach the parsers."""
    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = b""

        def __init__(self, stdout: str | bytes) -> None:
            self.stdout = stdout

    def fake_run(argv: list[str], **_: object) -> Done:
        seen.append(argv)
        if "--version" in argv:  # the escape-flag gate measures the local gh once
            return Done("gh version 2.97.0 (2026-09-24)\n")
        return Done(b"\x1b[31mFAILED tests/x.py::a\x1b[0m plain\n")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    rs._job_log_escape_flags.cache_clear()

    body = rs._gh_api("repos/o/r/actions/jobs/7/logs")

    api_argv = next(argv for argv in seen if "--version" not in argv)
    assert "--allow-escape-sequences" in api_argv
    assert body == "FAILED tests/x.py::a plain\n"


def test_a_json_fetch_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape strip is for log bodies only: a JSON answer is returned byte-for-byte,
    and gh keeps its own protection because the flag is a log-only concession."""
    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = b""
        stdout = b'{"a": "\\u001b[31m"}\n'

    def fake_run(argv: list[str], **_: object) -> Done:
        seen.append(argv)
        return Done()

    monkeypatch.setattr(rs.subprocess, "run", fake_run)

    assert rs._gh_api("repos/o/r/runs?per_page=1") == '{"a": "\\u001b[31m"}\n'
    assert "--allow-escape-sequences" not in seen[0]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("\x1b[31;1mFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x1b[38;5;196mFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x1b[38:2::255:0:0mFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x1b[?25lFAILED tests/a.py\x1b[?25h", "FAILED tests/a.py"),
        ("\x1b[>0cFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x1b]52;c;ZXZpbA==\x07FAILED tests/a.py", "FAILED tests/a.py"),
        ("\x1b]8;;https://evil.example\x1b\\FAILED tests/a.py\x1b]8;;\x1b\\", "FAILED tests/a.py"),
        ("\x1b]0;pwned\x07FAILED tests/a.py", "FAILED tests/a.py"),
        ("\x1bP1;2q\x1b\\FAILED tests/a.py", "FAILED tests/a.py"),
        ("\x9b31mFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x9d0;title\x07FAILED tests/a.py", "FAILED tests/a.py"),
        # C1 ST (`\x9c`) closes a string opened with a C1 introducer, as `ESC \` does: without
        # it the string ran to the newline and ate the FAILED line after it.
        ("\x9d0;title\x9cFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x90q\x9cFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x98q\x9cFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x9b31m\x9cFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x9eq\x9cFAILED tests/a.py", "FAILED tests/a.py"),
        ("\x9fq\x9cFAILED tests/a.py", "FAILED tests/a.py"),
        ("FAILED tests/a.py\x9c", "FAILED tests/a.py"),
        ("\x1b(BFAILED tests/a.py", "FAILED tests/a.py"),
        # ECMA-48's ESC final byte runs 0x30-0x7E, so the digit and punctuation finals are
        # two-byte sequences too: `ESC 7` DECSC, `ESC 8` DECRC, `ESC =` DECKPAM, `ESC >` DECKPNM.
        # Stopping the class at `@` dropped only the ESC and left the final byte glued to the
        # visible text, so this marker parsed as `7REGRESSION` and the key vanished.
        ("\x1b7[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b8[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b=[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b>[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("FAILED tests/a.py\x07", "FAILED tests/a.py"),
        ("FAILED tests/a.py\x1b", "FAILED tests/a.py"),
        ("FAILED tests/a.py\rcontinuation", "FAILED tests/a.py\ncontinuation"),
        # CRLF still collapses to ONE newline in ground text: the CR is a cursor move followed
        # by the line break, not a second line break.
        ("a\r\nb", "a\nb"),
        # A CR inside an OSC/DCS payload is a C0 that executes without ending the sequence, so
        # the hidden payload can never mint a gate key. The head rewrote CR to LF before the
        # scanner knew which state it was in, and the driver then read that LF as the string's
        # end: `ESC ] 0;x CR FAILED tests/a.py::t` stripped to `\nFAILED tests/a.py::t`. Both
        # P1/BLOCKING threads at a6784645d6 are this one defect (sol-review, line 881).
        ("\x1bPpayload\rFAILED tests/a.py::t\x1b\\", ""),
        ("\x1b]0;x\rFAILED tests/a.py::t\x1b\\", ""),
        ("\x1b]0;x\r[quality] REGRESSION: ruff.style\x1b\\", ""),
        # A CRLF pair inside a payload is payload too: the CR is dropped and the LF it is paired
        # with goes with it, so neither half ends the string. A LONE LF is still a line boundary
        # and still ends an unterminated sequence (the row below pins that, deliberately).
        ("\x1b]0;x\r\r\nFAILED tests/a.py::t\x1b\\", ""),
        ("\x1b]0;unterminated\nFAILED tests/a.py", "\nFAILED tests/a.py"),
        # A BS is a cursor step back, not a byte to drop: the next character REPLACES the one
        # the terminal overwrote, so the covered character must not survive as visible text.
        # P2/BLOCKING at 1d4ca14b77 (PRRT_kwDOSEvNd86uyhMp): keeping the `X` lost the marker.
        ("[quality] REGX\bRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        # ...and the same step in the direction that MINTS: a line the terminal shows as
        # PASSED must not keep a `FAILED tests/a.py::t` prefix to mint a module key from.
        ("FAILED tests/a.py::t\b\b\b\b\b\bPASSED", "FAILED tests/aPASSED"),
        # CUB is the same step with a count, in the spelling a coloured log actually uses.
        ("[quality] REGX\x1b[1DRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("[quality] REGXX\x1b[2DRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        # CHA is an absolute column, still on the active line: `JUNK` is overwritten in place.
        ("JUNK\x1b[1G[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        # EL blanks the cells it erases and does not move the cursor, so the erased `JUNK` is
        # blank rather than deleted and the marker still starts where the terminal put it.
        ("JUNK\x1b[2K[quality] REGRESSION: ruff.style", "    [quality] REGRESSION: ruff.style"),
        ("JUNK\x1b[1K[quality] REGRESSION: ruff.style", "    [quality] REGRESSION: ruff.style"),
        # Out of contract, pinned so it is not "improved" into a mint: IND (`ESC D`) and RI
        # (`ESC C`) are VERTICAL moves. Applying IND as a one-column backstep would join two
        # terminal lines into an `[quality] REGRESSION` marker no terminal displays.
        ("[quality] REGX\x1bDRESSION: ruff.style", "[quality] REGXRESSION: ruff.style"),
        # CUP/HVP and ED address the screen, not the line: dropped, exactly as before.
        ("\x1b[1;1H[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("\x1b[2J[quality] REGRESSION: ruff.style", "[quality] REGRESSION: ruff.style"),
        ("plain\n", "plain\n"),
        ("", ""),
    ],
)
def test_the_log_stripper_removes_every_terminal_control(raw: str, expected: str) -> None:
    """Numeric CSI is not the whole family: OSC/DCS strings (title, hyperlink, clipboard),
    private and colon-form CSI, C1 introducers and bare C0 survive a `[0-9;]*` strip, and
    any of them reaching a gate key reaches the generated markdown."""
    assert rs.strip_terminal_controls(raw) == expected


def test_a_backspace_decides_the_displayed_key_in_both_directions() -> None:
    """Codex P2/BLOCKING at 1d4ca14b77 (PRRT_kwDOSEvNd86uyhMp): BS was dropped as a control
    byte, so the character the terminal overwrote survived as text. Downwards it hides a red
    the terminal shows; upwards it MINTS a failure identity from a line the terminal shows as
    PASSED. A cursor step, applied, is the only reading that gets both directions right.
    """
    shown = rs.strip_terminal_controls("[quality] REGX\bRESSION: ruff.style")
    assert shown == "[quality] REGRESSION: ruff.style"
    assert rs.quality_keys([shown]) == ["quality:ruff.style"]

    passed = rs.strip_terminal_controls("FAILED tests/a.py::t\b\b\b\b\b\bPASSED")
    assert passed.endswith("PASSED")
    assert rs.pytest_keys(rs.failed_identities(passed)) == []


def test_a_forward_step_past_the_line_cannot_size_the_strip() -> None:
    """A CUF/CHA parameter is PR-controlled, so a forward step past the end of the line
    renders ONE blank, never `n` of them: `ESC[999999999C` must not make the strip
    allocate, while the blank it does leave keeps the separator a marker needs."""
    assert rs.strip_terminal_controls("\x1b[999999999C[quality] REGRESSION: ruff.style") == (
        " [quality] REGRESSION: ruff.style"
    )
    assert rs.strip_terminal_controls("\x1b[999999999G[quality] REGRESSION: ruff.style") == (
        " [quality] REGRESSION: ruff.style"
    )


@pytest.mark.parametrize("gap", [10, 999999999])
def test_a_backstep_within_a_forward_gap_keeps_the_visible_gate(gap: int) -> None:
    """Logical cursor positions survive gap compression until all overwrites are applied."""
    shown = rs.strip_terminal_controls(f"[quality] REGRESSION:\x1b[{gap}C\x1b[3Dmetric")
    assert rs.quality_keys([shown]) == ["quality:metric"]
    unchanged = rs.strip_terminal_controls(f"[quality] REGRESSION: metric\x1b[{gap}C\x1b[3DX")
    assert rs.quality_keys([unchanged]) == ["quality:metric"]


def test_a_backstep_across_a_gap_overwrites_the_original_column() -> None:
    shown = rs.strip_terminal_controls("[quality] REGX\x1b[999999999C\x1b[1000000000DRESSION: metric")
    assert shown == "[quality] REGRESSION: metric"
    assert rs.quality_keys([shown]) == ["quality:metric"]


def test_an_unbounded_cursor_parameter_reports_an_unreadable_log() -> None:
    raw = "\x1b[" + "9" * 5000 + "C[quality] REGRESSION: metric"
    with pytest.raises(rs.GhReadError, match="CSI cursor count longer than 64 digits"):
        rs.strip_terminal_controls(raw)
    # Leading zeros carry no magnitude and cannot trigger the integer-string limit either.
    assert rs.strip_terminal_controls("\x1b[" + "0" * 5000 + "1Cmarker") == " marker"


def test_a_cr_in_a_control_string_cannot_mint_a_gate_key() -> None:
    """Codex P1/BLOCKING at a6784645d6 (PRRT_kwDOSEvNd86uvMky, PRRT_kwDOSEvNd86uvouj):
    `strip_terminal_controls` rewrote every CR to LF before the scanner knew its state, so a
    CR inside an OSC/DCS payload became the LF that ENDS the string and the hidden payload
    reached the parsers. A terminal executes a CR in a sequence without ending it, so this is
    the P1 shape: text no terminal shows, deciding a gate key.
    """
    for raw in (
        "\x1bPpayload\rFAILED tests/a.py::t\x1b\\",  # DCS
        "\x1b]0;x\rFAILED tests/a.py::t\x1b\\",  # OSC
        "\x1b]0;x\r\r\nFAILED tests/a.py::t\x1b\\",  # CRLF pair inside the payload
    ):
        line = rs.strip_terminal_controls(raw)
        assert line == ""
        assert rs.pytest_keys([line]) == []
    quality = rs.strip_terminal_controls("\x1b]0;x\r[quality] REGRESSION: ruff.style\x1b\\")
    assert quality == ""
    assert rs.quality_keys([quality]) == []


def test_a_digit_final_escape_cannot_shift_a_quality_key() -> None:
    """Codex P2 at 95b53a88, `_ESC_SEQUENCE`: the final-byte class started at `@`, so
    `\\x1b7[quality] REGRESSION: ruff.style` stripped to `7[quality] ...` and `quality_keys`
    returned `[]` -- a regression the terminal shows and the ratchet scorecard never scored.
    The residue is the same class of PR-controlled byte deciding a gate key this PR closes."""
    for final in ("7", "8", "=", ">"):
        line = rs.strip_terminal_controls(f"\x1b{final}[quality] REGRESSION: ruff.style")
        assert line == "[quality] REGRESSION: ruff.style"
        assert rs.quality_keys([line]) == ["quality:ruff.style"]


def test_bel_terminates_only_an_osc_string() -> None:
    """Codex P2 at 8512cbf, `_CONTROL_STRING`: one `BEL|ST` terminator served all five
    introducers, so a BEL inside a DCS/SOS/PM/APC payload ended it early and its tail minted
    a gate key (`strip_terminal_controls("\\x1bPpayload\\x07FAILED tests/a.py::t\\x1b\\\\")
    -> 'FAILED tests/a.py::t'`). BEL ends only `ESC ]`; the other four need ST.
    """
    for intro, c1 in (("\x1bP", "\x90"), ("\x1bX", "\x98"), ("\x1b^", "\x9e"), ("\x1b_", "\x9f")):
        for raw in (f"{intro}payload\x07FAILED tests/a.py::t\x1b\\", f"{c1}payload\x07FAILED tests/a.py::t\x9c"):
            assert rs.strip_terminal_controls(raw) == ""
            assert rs.pytest_keys(rs.failed_identities(rs.strip_terminal_controls(raw))) == []
    # BEL still ends OSC, where it is legal, so the text after it is real: an honest key.
    for osc in ("\x1b]0;title\x07", "\x9d0;title\x07"):
        assert rs.strip_terminal_controls(f"{osc}FAILED tests/a.py::t") == "FAILED tests/a.py::t"
    # ST still ends both families, and an unterminated string still stops at the newline.
    assert rs.strip_terminal_controls("\x1bPpayload\x1b\\FAILED tests/a.py") == "FAILED tests/a.py"
    assert rs.strip_terminal_controls("\x1bPpayload\nFAILED tests/a.py") == "\nFAILED tests/a.py"


def test_a_log_with_a_clipboard_escape_yields_a_plain_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through `_gh_api` and the key extraction: PR-controlled log output cannot
    put a control byte into the markdown the scorecard renders."""

    class Done:
        returncode = 0
        stderr = b""

        def __init__(self, stdout: str | bytes) -> None:
            self.stdout = stdout

    def fake_run(argv: list[str], **_: object) -> Done:
        if "--version" in argv:  # the escape-flag gate measures the local gh once
            return Done("gh version 2.97.0 (2026-09-24)\n")
        return Done(b"\x1b[31m\x1b]52;c;ZXZpbA==\x07FAILED tests/foo.py::test_bar\x1b[?25l\x1b[0m\n")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    rs._job_log_escape_flags.cache_clear()

    log = rs._gh_api("repos/o/r/actions/jobs/7/logs")

    assert rs.pytest_keys(rs.failed_identities(log)) == ["pytest:tests/foo.py"]
    assert all(ord(ch) >= 0x20 or ch == "\n" for ch in log)
