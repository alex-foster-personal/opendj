"""WebKit cluster attribution when two Open DJ shells are live.

`associate_process_family`'s WebKit rule is a TIMING heuristic (first
WebContent within 45s of the shell), not ancestry: a WebContent helper's
`ppid` is launchd and its command is WebKit.framework's shared binary, with
no field tying it back to a requesting shell. With one shell running that
heuristic is unambiguous. With two shells running whose WebKit association
windows overlap, the SAME helper can fall inside both windows, and picking
"the first WebContent in-window" for whichever shell asks can hand it the
OTHER shell's cluster (Codex P1/BLOCKING, #705, resolve_role.py:98).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.diagnostics.probe_process_family import ProcessRow, associate_process_family

SHELL_CMD = "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"
WEBCONTENT_CMD = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"
)
GPU_CMD = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.GPU.xpc/Contents/MacOS/com.apple.WebKit.GPU"
)


class _FakeNative:
    """Duck-types `DarwinProcessMetrics` with start times handed in as plain
    seconds, so `ticks_to_seconds` is the identity function and tests never
    need real Darwin ctypes calls (this suite runs off-macOS too)."""

    def __init__(self, start_seconds: dict[int, float]) -> None:
        self._start_seconds = start_seconds

    def read(self, pid: int) -> SimpleNamespace:
        if pid not in self._start_seconds:
            raise ProcessLookupError(3, "No such process", pid)
        return SimpleNamespace(proc_start_abstime=self._start_seconds[pid])

    def ticks_to_seconds(self, ticks: float) -> float:
        return float(ticks)


def _shell(pid: int) -> ProcessRow:
    return ProcessRow(pid=pid, ppid=1, pgid=pid, command=SHELL_CMD)


def _webcontent(pid: int) -> ProcessRow:
    return ProcessRow(pid=pid, ppid=1, pgid=pid, command=WEBCONTENT_CMD)


def _gpu(pid: int) -> ProcessRow:
    return ProcessRow(pid=pid, ppid=1, pgid=pid, command=GPU_CMD)


def _native(start_seconds: dict[int, float]) -> DarwinProcessMetrics:
    """`_FakeNative` duck-types `DarwinProcessMetrics`; the cast tells mypy
    what every test here already relies on, since the real class can only be
    constructed on Darwin."""

    return cast(DarwinProcessMetrics, _FakeNative(start_seconds))


def test_one_shell_still_finds_its_own_webkit_cluster() -> None:
    """If broken: refusing contested clusters also breaks the ordinary,
    overwhelmingly common single-build case that has no contest to refuse.
    """
    shell = _shell(100)
    webcontent = _webcontent(300)
    gpu = _gpu(301)
    native = _native({100: 0.0, 300: 3.0, 301: 4.0})

    family, association = associate_process_family(
        [shell, webcontent, gpu], shell, native
    )

    assert association["webkit_cluster_found"] is True
    assert association["webkit_cluster_ambiguous"] is False
    pids = {row.pid for row, _role in family}
    assert {300, 301} <= pids


def test_a_webcontent_claimable_by_two_live_shells_is_refused_not_guessed() -> None:
    """The exact shape Codex reported: shell B starts (t=35) before shell A's
    own WebContent appears (t=40), shell B's own WebContent is slower still
    (t=45), so "first WebContent in shell B's 45s window" resolves to pid 300
    -- shell A's cluster, plausible and wrong. Resolving shell A hits the same
    trap in reverse, because 300 also sits inside shell B's window. Neither
    resolution has enough information to pick correctly, so both must refuse.
    """
    shell_a = _shell(100)
    shell_b = _shell(200)
    webcontent_a = _webcontent(300)
    webcontent_b = _webcontent(400)
    native = _native({100: 0.0, 200: 35.0, 300: 40.0, 400: 45.0})
    rows = [shell_a, shell_b, webcontent_a, webcontent_b]

    family_b, association_b = associate_process_family(rows, shell_b, native)
    assert association_b["webkit_cluster_found"] is False
    assert association_b["webkit_cluster_ambiguous"] is True
    assert {row.pid for row, _role in family_b} == {200}

    family_a, association_a = associate_process_family(rows, shell_a, native)
    assert association_a["webkit_cluster_found"] is False
    assert association_a["webkit_cluster_ambiguous"] is True
    assert {row.pid for row, _role in family_a} == {100}


def test_a_cluster_member_contested_only_via_the_cluster_window_is_refused() -> None:
    """The exact shape Codex reported on the earlier fix: shell A starts at
    t=0 and its own WebContent at t=3 (within A's 45s window, so `cluster_start`
    for A is 3 -- not itself contested by shell B, which starts at t=4). Shell
    B's own WebContent and GPU start at t=5, inside the 3s CLUSTER window of
    A's cluster_start=3, so the old check (only testing `cluster_start` for a
    contest) pulled B's helpers into A's cluster and reported it unambiguous.
    B's helpers are themselves contested (they fall inside A's 45s window
    too), so testing every member, not just `cluster_start`, must refuse.
    """
    shell_a = _shell(100)
    shell_b = _shell(200)
    webcontent_a = _webcontent(300)
    webcontent_b = _webcontent(400)
    gpu_b = _gpu(401)
    native = _native({100: 0.0, 200: 4.0, 300: 3.0, 400: 5.0, 401: 5.0})
    rows = [shell_a, shell_b, webcontent_a, webcontent_b, gpu_b]

    family_a, association_a = associate_process_family(rows, shell_a, native)
    assert association_a["webkit_cluster_found"] is False
    assert association_a["webkit_cluster_ambiguous"] is True
    assert {row.pid for row, _role in family_a} == {100}


def test_two_shells_far_enough_apart_each_still_resolve_their_own_cluster() -> None:
    """If broken: refusing on ANY second live shell (rather than only a
    genuinely contested candidate) would blind every two-build session, not
    just the overlapping-launch one Codex found.
    """
    shell_a = _shell(100)
    shell_b = _shell(200)
    webcontent_a = _webcontent(300)
    webcontent_b = _webcontent(400)
    native = _native({100: 0.0, 200: 100.0, 300: 3.0, 400: 103.0})
    rows = [shell_a, shell_b, webcontent_a, webcontent_b]

    family_a, association_a = associate_process_family(rows, shell_a, native)
    assert association_a["webkit_cluster_found"] is True
    assert association_a["webkit_cluster_ambiguous"] is False
    assert {row.pid for row, _role in family_a} == {100, 300}

    family_b, association_b = associate_process_family(rows, shell_b, native)
    assert association_b["webkit_cluster_found"] is True
    assert association_b["webkit_cluster_ambiguous"] is False
    assert {row.pid for row, _role in family_b} == {200, 400}


def test_a_second_unrelated_webkit_cluster_in_window_is_refused() -> None:
    """The shape Codex reported: a single Open DJ shell, no second shell in
    sight, but an UNRELATED WKWebView (Safari, another Electron app) starts
    its own WebContent at t=1 -- earlier than Open DJ's own WebContent at
    t=10 -- inside the same 45s window. Nothing ties a WebKit helper to the
    app that launched it, so "first WebContent in window" would silently
    profile the other app's process. Two distinct clusters (more than
    `WEBKIT_CLUSTER_WINDOW_SECONDS` apart) in one window must refuse, even
    with only one live shell (Codex P1/BLOCKING, #705, resolve_role.py:105).
    """
    shell = _shell(100)
    unrelated_webcontent = _webcontent(300)
    own_webcontent = _webcontent(301)
    native = _native({100: 0.0, 300: 1.0, 301: 10.0})

    family, association = associate_process_family(
        [shell, unrelated_webcontent, own_webcontent], shell, native
    )

    assert association["webkit_cluster_found"] is False
    assert association["webkit_cluster_ambiguous"] is True
    assert {row.pid for row, _role in family} == {100}


def test_two_webcontents_inside_the_cluster_window_are_kept_not_refused() -> None:
    """Fresh evidence against an earlier fix here: the locked production
    capture at tests/fixtures/perf/MANIFEST.json ("probe-samples.jsonl") shows
    one real Open DJ family legitimately holding two `webkit-webcontent` PIDs
    at once, both inside the 3s cluster window. A prior guard treated a second
    in-window WebContent as proof of an unrelated app -- "one WKWebView" does
    not mean "one WebContent process" -- and cleared this exact, real family as
    ambiguous, making the probe and role profilers omit all WebKit processes
    during a normal layout (Codex P1/BLOCKING, #705, probe_process_family.py:334).
    """
    shell = _shell(100)
    first_webcontent = _webcontent(300)
    second_webcontent = _webcontent(301)
    native = _native({100: 0.0, 300: 10.0, 301: 12.0})

    family, association = associate_process_family(
        [shell, first_webcontent, second_webcontent], shell, native
    )

    assert association["webkit_cluster_found"] is True
    assert association["webkit_cluster_ambiguous"] is False
    assert {row.pid for row, _role in family} == {100, 300, 301}


def test_no_webcontent_in_window_reports_no_cluster_not_an_error() -> None:
    """If broken: a shell with no WebView up yet (still launching) crashes
    resolution instead of reporting an ordinary, expected absence.
    """
    shell = _shell(100)
    native = _native({100: 0.0})

    family, association = associate_process_family([shell], shell, native)

    assert association["webkit_cluster_found"] is False
    assert association["webkit_cluster_ambiguous"] is False
    assert {row.pid for row, _role in family} == {100}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
