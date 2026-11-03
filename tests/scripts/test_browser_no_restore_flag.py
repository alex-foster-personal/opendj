"""Every tracked launcher that gives a Chromium browser a user-data dir must pass --hide-crash-restore-bubble.

Why: a browser killed mid-run leaves its profile marked "Crashed", and the next launch on that profile
shows Chrome's "Restore pages?" prompt, which waits for a human click and hangs agent test loops. The
state is sticky: a later clean shutdown keeps "Crashed" while the prompt is unanswered.

Scope: this keeps a future reused profile from hanging. Today's launchers use a temporary profile per
process, so the flag there only covers a relaunch inside one run.

Invariant (not a list): every launch site in a tracked source file (the suffixes in SOURCE_SUFFIXES;
docs and extensionless files are out of scope) that names `--user-data-dir=` or `launchPersistentContext(`
carries the flag in that same call. Electron launchers are excused by name: the restore bubble is Chrome
UI that Electron does not ship.

  - [if] a launch site lacks the flag [then] this module fails with its path and line, [else stop].
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCH = re.compile(r"--user-data-dir=|launchPersistentContext\(")
FLAG = "--hide-crash-restore-bubble"
SOURCE_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".svelte", ".py", ".sh"}
THIS_FILE = "tests/scripts/test_browser_no_restore_flag.py"
KNOWN_LAUNCHERS = {
    "apps/webui/frontend/scripts/probe-device-id-salting.mjs",
    "apps/webui/frontend/tests/e2e/agent-orders-hidden-tab.spec.ts",
}
ELECTRON_LAUNCHERS = {
    "apps/desktop/mcp/cdp-driver.real.test.ts",
    "apps/desktop/mcp/smoke.ts",
    "scripts/electron_dev.py",
    "tests/scripts/test_electron_dev.py",
}


def _launch_call_text(source: str, start: int) -> str:
    """The launch expression from `start` to the bracket that closes it, comment lines dropped."""
    depth = 0
    end = len(source)
    for index in range(start, len(source)):
        if source[index] in "([{":
            depth += 1
        elif source[index] in ")]}":
            depth -= 1
            if depth < 0 or (depth == 0 and source[index] == ")" and source[start] == "l"):
                end = index
                break
    return "\n".join(line for line in source[start:end].splitlines() if not line.lstrip().startswith(("//", "#")))


def _lines_missing_flag(source: str) -> list[int]:
    """One-based line of each launch site whose call does not carry the flag."""
    return [
        source.count("\n", 0, match.start()) + 1
        for match in LAUNCH.finditer(source)
        if FLAG not in _launch_call_text(source, match.start())
    ]


def _tracked_launchers() -> list[str]:
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in files:
        path = REPO / rel
        if path.suffix not in SOURCE_SUFFIXES or "node_modules" in rel or rel == THIS_FILE or rel in ELECTRON_LAUNCHERS:
            continue
        if path.is_file() and LAUNCH.search(path.read_text(encoding="utf-8")):
            hits.append(rel)
    return hits


def test_launchers_exist_so_the_check_is_measuring_something() -> None:
    launchers = set(_tracked_launchers())
    print(f"if a known launcher is not found then the scan is broken: found {sorted(launchers)}")
    assert KNOWN_LAUNCHERS <= launchers, "scan lost a known launcher; the regex or git ls-files is broken"


def test_every_persistent_profile_launcher_passes_the_no_restore_flag() -> None:
    missing = [
        f"{rel}:{line}"
        for rel in _tracked_launchers()
        for line in _lines_missing_flag((REPO / rel).read_text(encoding="utf-8"))
    ]
    print(f"if a launcher lacks {FLAG} then broken: {missing or 'none'}")
    assert not missing, f"add {FLAG} to: {missing}"


def test_only_the_same_call_can_satisfy_the_check() -> None:
    """Negative control: a neighbouring call or a comment does not excuse a bare launcher."""
    guarded = f"spawn(bin, ['--user-data-dir=/tmp/a', '{FLAG}']);"
    bare = "spawn(bin, ['--user-data-dir=/tmp/b']);"
    commented = f"spawn(bin, [\n'--user-data-dir=/tmp/c',\n// '{FLAG}'\n]);"
    assert _lines_missing_flag(guarded) == []
    assert _lines_missing_flag(f"launchPersistentContext(p, {{ args: ['{FLAG}'] }});") == []
    assert _lines_missing_flag(f"{bare}\n{guarded}") == [1]
    assert _lines_missing_flag(commented) == [2]
