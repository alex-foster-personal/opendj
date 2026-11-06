"""The local pre-push selection reads CI's own ignore list and never runs empty for an unread diff.

Regression lines:
  - [if] the LANE_IGNORES block in ci.yml is not what the local runner reads [then] local and CI selections drift, so fail, [else stop].
  - [if] a workflow without the block parses as no ignores [then] the local run silently widens or narrows, so fail, [else stop].
  - [if] an empty diff yields an empty selection [then] an unread diff reads green, so fail, [else stop].
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from scripts import ci_local_affected as ci_local
from scripts.affected_tests import REPO
from scripts.ci_local_affected import Unmeasured, _freeze_tree, changed_paths, lane_ignores, main


def _host_git() -> str:
    """git by resolved path (AGENTS.md: launch programs by resolved path); none is a failure, not a skip."""
    git = shutil.which("git")
    assert git is not None, "these tests build real git repositories and need git on PATH"
    return git


def test_git_exe_missing_from_path_is_unmeasured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] git is not on PATH [then] diff selection raises Unmeasured with the exact message, [else stop]."""
    ci_local._git_exe.cache_clear()
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        with pytest.raises(Unmeasured, match=r"^git is not on PATH: the diff cannot be read$"):
            ci_local._git_exe()
    finally:
        ci_local._git_exe.cache_clear()


def test_reads_every_ignore_the_shard_lane_passes() -> None:
    text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    ignores = lane_ignores(text)
    block = text[text.index("LANE_IGNORES=(") : text.index(")", text.index("LANE_IGNORES=("))]
    assert ignores == re.findall(r"--ignore=\S+", block)
    assert "--ignore=tests/analysis" in ignores and len(ignores) >= 5


def test_a_workflow_without_the_block_is_unmeasured_not_empty() -> None:
    with pytest.raises(Unmeasured):
        lane_ignores("jobs:\n  test:\n    steps: []\n")


def test_an_empty_diff_is_unmeasured(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run([_host_git(), *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8", newline="\n")
    git("add", "a.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
    with pytest.raises(Unmeasured):
        changed_paths(tmp_path, "main")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8", newline="\n")
    assert changed_paths(tmp_path, "main") == ["b.txt"]
    # [if] git C-quotes a non-ASCII path ("caf\303\251.py") [then] it no longer ends in .py and its tests drop out.
    (tmp_path / "caf\u00e9.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
    git("add", "a.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b", "--allow-empty")
    assert changed_paths(tmp_path, "main") == ["b.txt", "caf\u00e9.py"]
    git("add", "caf\u00e9.py")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    assert changed_paths(tmp_path, "HEAD~1") == ["b.txt", "caf\u00e9.py"]


def test_a_base_ahead_of_head_is_unmeasured(tmp_path: Path) -> None:
    """[if] the base has commits HEAD lacks [then] CI selects on a merged tree this one is not, so fail, [else stop]."""

    def git(*args: str) -> None:
        subprocess.run([_host_git(), *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8", newline="\n")
        git("add", name)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
    git("branch", "feature", "HEAD~1")
    git("checkout", "-q", "feature")
    (tmp_path / "c.txt").write_text("c", encoding="utf-8", newline="\n")
    with pytest.raises(Unmeasured, match="merge it first"):
        changed_paths(tmp_path, "main")
    git("merge", "-q", "main")
    assert changed_paths(tmp_path, "main") == ["c.txt"]


def test_unknown_base_exits_3_not_0() -> None:
    assert main(["--base", "no-such-ref-xyz", "--list"]) == 3


def _tiny_repo(root: Path, test_body: str, conftest: str = "") -> Path:
    """A real git repository the real selector can read: one app module, one test that imports it,
    the shard lane's ignore block and an empty always-run list; then an uncommitted change to the app."""
    files = {
        ".github/workflows/ci.yml": (
            "jobs:\n  t:\n    steps:\n      - run: |\n"
            "          LANE_IGNORES=(\n            --ignore=tests/skipped\n          )\n"
        ),
        "ci/pr-always-run.txt": "# none\n",
        "apps/__init__.py": "",
        "apps/a.py": "X = 1\n",
        "tests/__init__.py": "",
        "tests/conftest.py": conftest,
        "tests/test_a.py": test_body,
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")

    def git(*args: str) -> None:
        subprocess.run([_host_git(), *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (root / "apps/a.py").write_text("X = 2\n", encoding="utf-8", newline="\n")
    return root


def test_a_selector_that_cannot_parse_the_tree_exits_3(tmp_path: Path) -> None:
    """[if] a SyntaxError from the selector escapes [then] Python exits 1 and reads as a test failure, so fail, [else stop]."""
    root = _tiny_repo(tmp_path, "from apps.a import X\n\n\ndef test_x():\n    assert X\n")
    (root / "tests/conftest.py").write_text("def broken(:\n", encoding="utf-8", newline="\n")
    assert main(["--root", str(root), "--base", "main", "--list"]) == 3


def test_the_real_selection_reaches_the_importing_test(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the tiny repo's change selects nothing [then] the two tests below prove nothing, so fail, [else stop]."""
    root = _tiny_repo(tmp_path, "from apps.a import X\n\n\ndef test_x():\n    assert X\n")
    assert main(["--root", str(root), "--base", "main", "--list"]) == 0
    assert capsys.readouterr().out.startswith("tests/test_a.py\t")


# pytest itself exits with this code: a sessionfinish hook runs on the xdist controller too,
# where a raised Exit in a test module is only a collection error that xdist reports as 1.
_EXIT_AS = "def pytest_sessionfinish(session):\n    session.exitstatus = {}\n"


@pytest.mark.parametrize(
    ("test_body", "conftest", "ours"),
    [
        ("from apps.a import X\n\n\ndef test_x():\n    assert X == 2\n", "", 0),
        ("from apps.a import X\n\n\ndef test_x():\n    assert X == 1\n", "", 1),
        ("from apps.a import X\n\n\ndef test_x():\n    assert X == 2\n", _EXIT_AS.format(2), 1),
        ("from apps.a import X\n\n\ndef test_x():\n    assert X == 2\n", _EXIT_AS.format(3), 1),
        ("from apps.a import X\n", "", 1),
        ("import pytest\n\nfrom apps.a import X\n\npytest.skip('n/a', allow_module_level=True)\n", "", 0),
    ],
    ids=["pass", "fail", "pytest-2", "pytest-3", "pytest-5-no-tests", "pytest-5-module-skipped"],
)
def test_a_real_pytest_exit_never_collides_with_the_runners_own(
    tmp_path: Path, test_body: str, conftest: str, ours: int
) -> None:
    """[if] pytest's own 2 or 3 is passed through [then] it reads as FullSuite or Unmeasured, so fail, [else stop]."""
    root = _tiny_repo(tmp_path, test_body, conftest)
    assert main(["--root", str(root), "--base", "main", "--", "-p", "no:cacheprovider"]) == ours


def test_a_run_that_hangs_in_collection_is_killed_at_the_deadline(tmp_path: Path) -> None:
    """[if] a module hangs on import [then] --timeout never arms (it bounds tests, not collection), so the run
    deadline must, and must take the xdist workers that hang with it, [else stop]."""
    pids = tmp_path / "pids"
    pids.mkdir()
    body = (
        "import os\nimport time\nfrom pathlib import Path\n\nfrom apps.a import X\n\n"
        f"Path({str(pids)!r}, str(os.getpid())).write_text('', encoding='utf-8', newline='\\n')\n"
        "time.sleep(60)\n\n\ndef test_x():\n    assert X == 2\n"
    )
    root = _tiny_repo(tmp_path / "repo", body)
    argv = ["--root", str(root), "--base", "main", "--deadline-s", "8", "--", "-p", "no:cacheprovider"]
    assert main(argv) == 1
    hung = [int(p.name) for p in pids.iterdir()]
    assert hung, "control: the hanging module never ran, so nothing was measured"
    survivors = [pid for pid in hung if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE]
    assert survivors == [], f"left running after the deadline: {survivors}"


def test_a_frozen_tree_cannot_spawn_a_worker_the_kill_would_miss(tmp_path: Path) -> None:
    """[if] the controller can spawn after the snapshot [then] that worker escapes the kill, so fail, [else stop]."""
    spawned = tmp_path / "spawned"
    spawned.mkdir()
    spawner = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "while True:\n"
        "    kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"    Path({str(spawned)!r}, str(kid.pid)).write_text('', encoding='utf-8', newline='\\n')\n"
        "    time.sleep(0.05)\n"
    )
    controller = subprocess.Popen([sys.executable, "-c", spawner])
    try:
        time.sleep(1)
        assert list(spawned.iterdir()), "control: the spawner never spawned, so nothing was measured"
        tree = _freeze_tree(controller.pid)
        before = {p.name for p in spawned.iterdir()}
        time.sleep(0.5)
        assert {p.name for p in spawned.iterdir()} == before, "a frozen controller kept spawning"
        assert {int(name) for name in before} <= {p.pid for p in tree}
    finally:
        for pid in [controller.pid, *(int(p.name) for p in spawned.iterdir())]:
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(pid).kill()
        controller.wait(timeout=10)
