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

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from apps.shared import machine_pressure
from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.diagnostics.probe_process_family import (
    ProcessRow,
    associate_process_family,
)
from scripts.diagnostics.process_family_rule import (
    ANALYSIS_LANE_ROLE,
    ELECTRON_GPU_ROLE,
    ELECTRON_RENDERER_ROLE,
    ELECTRON_UTILITY_ROLE,
    FFMPEG_ROLE,
    NAMED_CHILD_ROLES,
    PROCESS_ROLES,
    associate_family,
    named_child_role,
    opendj_process_name,
    process_role,
)
from scripts.perf.resolve_role import PROFILABLE_ROLES

_APP = "/Applications/Open DJ.app"
_ELECTRON_RENDERER_CMD = (
    f"{_APP}/Contents/Frameworks/Open DJ Helper (Renderer).app/Contents/MacOS/Open DJ Helper (Renderer) --type=renderer"
)
_ELECTRON_GPU_CMD = (
    f"{_APP}/Contents/Frameworks/Open DJ Helper (GPU).app/Contents/MacOS/Open DJ Helper (GPU) --type=gpu-process"
)
_ELECTRON_PLUGIN_CMD = (
    f"{_APP}/Contents/Frameworks/Open DJ Helper (Plugin).app/Contents/MacOS/Open DJ Helper (Plugin) --type=ppapi"
)
_ELECTRON_UTILITY_CMD = (
    f"{_APP}/Contents/Frameworks/Open DJ Helper.app/Contents/MacOS/"
    "Open DJ Helper --type=utility --utility-sub-type=network.mojom.NetworkService"
)
_LANE_BUILD_GPU_CMD = (
    "/Applications/Open DJ (B).app/Contents/Frameworks/Open DJ (B) Helper (GPU).app/Contents/MacOS/"
    "Open DJ (B) Helper (GPU) --type=gpu-process"
)
_CLAUDE_RENDERER = (
    "/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/"
    "Claude Helper (Renderer) --type=renderer"
)
_FFMPEG_HOMEWRAPPED = (
    "/opt/homebrew/bin/ffmpeg -nostdin -hide_banner -v error -i /Music/Agent Orange.mp3 -map 0:a:0 -f f32le -"
)
_FFMPEG_TASKPOLICY = (
    "/usr/sbin/taskpolicy -b /Applications/Open DJ.app/Contents/Resources/payload/bin/ffmpeg -nostdin -i x -"
)
_FFMPEG_NICE = "/usr/bin/nice -n 10 /Applications/Open DJ.app/Contents/Resources/payload/bin/ffmpeg -nostdin -i x -"
_FFMPEG_BUNDLED = "/Applications/Open DJ.app/Contents/Resources/payload/bin/ffmpeg -nostdin -i x -"
_LANE_QUEUE_CLI = (
    "/Applications/Open DJ.app/Contents/Resources/payload/runtime/bin/python3 "
    "-m apps.analysis.queue_cli --db /d/state.db --json run --batch-id 7 --backend essentia"
)
_LANE_RUN = "python3 -u -m apps.analysis.run --backend b --workers 4"
_LANE_BACKFILL = "python3 -m apps.webui.server.backfill_drain_job --db /d/state.db"

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

    family, association = associate_process_family([shell, webcontent, gpu], shell, native)

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

    family, association = associate_process_family([shell, unrelated_webcontent, own_webcontent], shell, native)

    assert association["webkit_cluster_found"] is False
    assert association["webkit_cluster_ambiguous"] is True
    assert {row.pid for row, _role in family} == {100}


def test_two_webcontents_inside_the_cluster_window_are_kept_not_refused() -> None:
    """Fresh evidence against an earlier fix here: the locked production
    capture at (private path) ("probe-samples.jsonl") shows
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

    family, association = associate_process_family([shell, first_webcontent, second_webcontent], shell, native)

    assert association["webkit_cluster_found"] is True
    assert association["webkit_cluster_ambiguous"] is False
    assert {row.pid for row, _role in family} == {100, 300, 301}


@pytest.mark.requirement("PERFMODE-05")
def test_opendj_process_name_uses_the_setproctitle_token() -> None:
    """[if] a command line carries a setproctitle --name [then] name parser returns, [else stop]."""
    assert opendj_process_name("opendj-engine --name opendj-engine --port 8585 [/usr/bin/python3]") == "opendj-engine"
    assert opendj_process_name("opendj-worker --name opendj-worker [...]") == "opendj-worker"
    assert opendj_process_name("opendj-backend --name opendj-backend --port 8787 [...]") == "opendj-backend"


@pytest.mark.requirement("PERFMODE-05")
def test_opendj_desktop_shell_is_named_from_the_executable() -> None:
    """[if] the command is the Open DJ desktop [then] name parser returns, [else stop]."""
    assert opendj_process_name(SHELL_CMD) == "opendj-desktop"


@pytest.mark.requirement("PERFMODE-05")
def test_webkit_helper_is_unnamed() -> None:
    """[if] the command is a WebKit helper binary [then] name parser returns, [else stop]."""
    assert opendj_process_name(WEBCONTENT_CMD) == "unnamed"


@pytest.mark.requirement("PERFMODE-05")
def test_associate_still_includes_unnamed_webkit_members() -> None:
    """[if] a single shell owns a WebKit cluster [then] family resolver keeps, [else stop]."""
    shell = _shell(100)
    webcontent = _webcontent(300)
    gpu = _gpu(301)
    native = _native({100: 0.0, 300: 3.0, 301: 4.0})

    family, _association = associate_process_family([shell, webcontent, gpu], shell, native)

    pids = {row.pid for row, _role in family}
    assert {300, 301} <= pids
    commands = {row.command for row, _role in family if row.pid in {300, 301}}
    assert all(opendj_process_name(command) == "unnamed" for command in commands)


@pytest.mark.requirement("PERFMODE-05")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (_ELECTRON_RENDERER_CMD, ELECTRON_RENDERER_ROLE),
        (_ELECTRON_GPU_CMD, ELECTRON_GPU_ROLE),
        (_ELECTRON_PLUGIN_CMD, ELECTRON_UTILITY_ROLE),
        (_ELECTRON_UTILITY_CMD, ELECTRON_UTILITY_ROLE),
        (_LANE_BUILD_GPU_CMD, ELECTRON_GPU_ROLE),
    ],
)
def test_open_dj_electron_helpers_map_to_the_three_electron_roles(command: str, expected: str) -> None:
    """[if] an Open DJ helper path is a shell descendant [then] role maps, [else stop]."""
    row = ProcessRow(pid=201, ppid=100, pgid=100, command=command)
    assert process_role(row, 100, {201}) == expected


@pytest.mark.requirement("PERFMODE-05")
def test_another_apps_electron_helper_is_other() -> None:
    """[if] a non-Open-DJ helper is on the box [then] it is other, [else stop]."""
    row = ProcessRow(pid=202, ppid=1, pgid=202, command=_CLAUDE_RENDERER)
    assert process_role(row, 100, set()) == "other"
    assert named_child_role(_CLAUDE_RENDERER) is None


@pytest.mark.requirement("PERFMODE-05")
@pytest.mark.parametrize(
    "command",
    [_FFMPEG_HOMEWRAPPED, _FFMPEG_TASKPOLICY, _FFMPEG_NICE, _FFMPEG_BUNDLED],
)
def test_an_ffmpeg_child_of_the_engine_is_ffmpeg(command: str) -> None:
    """[if] ffmpeg decodes under the engine [then] role is ffmpeg, [else stop]."""
    row = ProcessRow(pid=203, ppid=100, pgid=100, command=command)
    assert process_role(row, 100, {203}) == FFMPEG_ROLE
    assert opendj_process_name(command) == FFMPEG_ROLE


@pytest.mark.requirement("PERFMODE-05")
@pytest.mark.parametrize("command", [_LANE_QUEUE_CLI, _LANE_RUN, _LANE_BACKFILL])
def test_analysis_lane_children_are_named(command: str) -> None:
    """[if] analysis lane argv matches [then] role is analysis-lane, [else stop]."""
    row = ProcessRow(pid=204, ppid=100, pgid=100, command=command)
    assert process_role(row, 100, {204}) == ANALYSIS_LANE_ROLE


@pytest.mark.requirement("PERFMODE-05")
@pytest.mark.parametrize(
    "command",
    [
        "python3 -m apps.x --input /music/ffmpeg -f",
        "/usr/bin/ffmpegthumbnailer -i x",
        "/usr/bin/ffprobe -i /a/ffmpeg -v",
        "python3 -m apps.analysis.runner",
        "python3 -m pytest -k apps.analysis.run",
    ],
)
def test_named_child_role_negative_controls(command: str) -> None:
    """[if] argv mimics ffmpeg or lane [then] no false named role, [else stop]."""
    assert named_child_role(command) is None


@pytest.mark.requirement("PERFMODE-05")
def test_setproctitle_worker_still_names_opendj_token() -> None:
    """[if] argv is opendj-worker title [then] name stays opendj-worker, [else stop]."""
    cmd = "opendj-worker --name opendj-worker [...]"
    assert named_child_role(cmd) is None
    assert opendj_process_name(cmd) == "opendj-worker"


@pytest.mark.requirement("PERFMODE-05")
def test_role_spellings_agree_across_probe_register_and_live_walk() -> None:
    """[if] role tuples match [then] probe, register and app agree, [else stop]."""
    assert NAMED_CHILD_ROLES == machine_pressure.NAMED_CHILD_ROLES
    assert set(NAMED_CHILD_ROLES) <= set(PROFILABLE_ROLES) <= set(PROCESS_ROLES)
    assert machine_pressure.ELECTRON_RENDERER_ROLE == ELECTRON_RENDERER_ROLE
    assert machine_pressure.ELECTRON_GPU_ROLE == ELECTRON_GPU_ROLE
    assert machine_pressure.ELECTRON_UTILITY_ROLE == ELECTRON_UTILITY_ROLE
    assert machine_pressure.FFMPEG_ROLE == FFMPEG_ROLE
    assert machine_pressure.ANALYSIS_LANE_ROLE == ANALYSIS_LANE_ROLE
    cmds = (
        _ELECTRON_RENDERER_CMD,
        _ELECTRON_GPU_CMD,
        _ELECTRON_PLUGIN_CMD,
        _ELECTRON_UTILITY_CMD,
        _LANE_BUILD_GPU_CMD,
        _CLAUDE_RENDERER,
        _FFMPEG_HOMEWRAPPED,
        _FFMPEG_TASKPOLICY,
        _FFMPEG_NICE,
        _FFMPEG_BUNDLED,
        _LANE_QUEUE_CLI,
        _LANE_RUN,
        _LANE_BACKFILL,
    )
    for cmd in cmds:
        assert named_child_role(cmd) == machine_pressure.named_child_role(cmd)
        assert opendj_process_name(cmd) == machine_pressure.opendj_process_name(cmd)


@pytest.mark.requirement("PERFMODE-05")
def test_every_process_role_result_is_a_declared_role() -> None:
    """[if] representative rows classify [then] each role is in PROCESS_ROLES, [else stop]."""
    shell_pid = 100
    desc = {101, 201, 202, 203, 204}
    rows = [
        ProcessRow(pid=100, ppid=1, pgid=100, command=SHELL_CMD),
        ProcessRow(pid=101, ppid=100, pgid=100, command="/usr/bin/python3 -m apps.engine_core serve"),
        ProcessRow(pid=201, ppid=100, pgid=100, command=_ELECTRON_RENDERER_CMD),
        ProcessRow(pid=202, ppid=100, pgid=100, command=_FFMPEG_HOMEWRAPPED),
        ProcessRow(pid=203, ppid=100, pgid=100, command=_LANE_QUEUE_CLI),
        ProcessRow(
            pid=204,
            ppid=100,
            pgid=100,
            command="opendj-worker --name opendj-worker --port 0 [python]",
        ),
        ProcessRow(pid=300, ppid=1, pgid=300, command=WEBCONTENT_CMD),
        ProcessRow(pid=301, ppid=1, pgid=301, command=_CLAUDE_RENDERER),
        ProcessRow(pid=302, ppid=1, pgid=302, command="unrelated --no-match"),
    ]
    for row in rows:
        role = process_role(row, shell_pid, desc)
        assert role in PROCESS_ROLES


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


def test_the_family_rule_is_one_file_at_both_import_paths() -> None:
    root = Path(__file__).resolve().parents[2]
    apps_copy = root / "apps" / "shared" / "process_family_rule.py"
    scripts_copy = root / "scripts" / "diagnostics" / "process_family_rule.py"
    assert apps_copy.read_bytes() == scripts_copy.read_bytes(), (
        "process_family_rule.py must be byte-identical: "
        "cp apps/shared/process_family_rule.py scripts/diagnostics/process_family_rule.py"
    )


def test_associate_family_matches_the_probe_wrapper() -> None:
    shell = _shell(100)
    webcontent = _webcontent(300)
    gpu = _gpu(301)
    start_seconds = {100: 0.0, 300: 3.0, 301: 4.0}
    native = _native(start_seconds)

    via_wrapper, assoc_wrapper = associate_process_family([shell, webcontent, gpu], shell, native)

    def start_of(pid: int) -> float | None:
        return start_seconds.get(pid)

    via_core, assoc_core = associate_family([shell, webcontent, gpu], shell, start_of)

    assert {r.pid for r, _ in via_wrapper} == {r.pid for r, _ in via_core}
    assert {r: role for r, role in via_wrapper} == {r: role for r, role in via_core}
    for key in (
        "webkit_cluster_found",
        "webkit_cluster_ambiguous",
        "webkit_refusal",
        "included_pids",
    ):
        assert assoc_wrapper[key] == assoc_core[key]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
