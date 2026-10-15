"""The tree-read census places each child program a test starts: shells, tree scanners and git commands.

Split from `test_ci_tree_read_census.py`, which covers the plugin's hooks and the report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import pytest_tree_read_census as plugin
from scripts.affected_tests import REPO
from scripts.ci_tree_read_census import (
    _within,
    load_census,
)
from scripts.pytest_tree_read_census import OUT_DIR_ENV, directories_listed
from tests.scripts.tree_read_census_runs import run_with_the_plugin


@pytest.mark.parametrize(
    ("argv", "opaque"),
    [
        (["find", "apps", "-name", "*.py"], True),
        (["rg", "--pre", "./reader", "x", "apps"], True),  # runs a helper per input
        (["rg", "--hostname-bin=./reader", "x", "apps"], True),  # runs a helper with no arguments
        (["rg", "x", "apps"], True),  # RIPGREP_CONFIG_PATH may add either
        (["fd", "-x", "wc", "x"], True),
        (["nice", "tree", "apps"], True),  # behind a wrapper
        (["uv", "run", "rg", "x"], True),  # behind a launcher
        (["FOO=1", "du", "apps"], True),
        (["ls", "apps"], True),
        (["sh", "-c", "git ls-files"], True),  # any shell script, never parsed
        (["bash", "scan.sh"], True),
        (["/bin/sh", "-c", "echo hi"], True),
        (["rg", "--version"], False),  # prints and walks nothing
        (["find", "--help"], False),
        (["git", "ls-files"], False),  # placed where it runs
        (["echo", "apps"], False),
        (["uv", "tree"], False),  # scanner names that are not the program
        (["pytest", "-k", "tree"], False),
        (["python", "tool.py", "find"], False),
    ],
)
def test_a_shell_or_tree_scanner_child_is_opaque(argv: list[str], opaque: bool) -> None:
    """[if] a shell or a tree scanner child is parsed for its paths [then] a helper or config it runs hides a walk, so fail, [else stop]."""
    assert plugin._runs_what_the_census_does_not_model(argv, str(REPO), None) is opaque, argv
    if opaque:  # marked unmeasured by the spawn hook, and never parsed for paths
        assert directories_listed("subprocess.Popen", (argv[0], argv, str(REPO), None)) == []


@pytest.mark.parametrize(
    "script",
    [
        'files="$(git ls-files | wc -l)"',  # a command substitution
        "echo `git ls-files`",
        'bash -c "find ../apps"',  # a nested shell
        'eval "git ls-files"',
        "f() { rg PATTERN ../apps; }; f",  # a function
        'env -S "git ls-files"',
        ". ./lib.sh; find apps",  # `.` is POSIX source
        "diff <(find apps) <(find tests)",  # a process substitution
        "export GIT_DIR=/other/.git; git ls-files",  # git's location set by an earlier command
        "GIT_WORK_TREE=/other; git -C ci ls-files",
        "echo a#b; git ls-files",  # shlex ends the line at a mid-word `#`, bash does not
        "n=${#x}; find apps",
        'SCANNER=rg; "$SCANNER" TODO .',  # an expanded program may be any scanner
        "git ls-files -z",  # the shape CPython audits for shell=True
        "true&&git -C apps ls-files | wc -l",
        "git ls-files -z > /dev/null 2>&1 < /dev/null",
        "git -C .github ls-files\ngit -C apps ls-files",
        "git -C $TARGET ls-files",
        "cd ../apps; git ls-files",
        "FLAG=1 git ls-files; env -i FLAG=1 -u HOME git -C apps ls-files",
    ],
)
def test_a_reader_in_any_shell_script_is_opaque_and_never_raises(script: str) -> None:
    """[if] a reader in a substitution, nested shell, eval, function or `env -S` is parsed, or breaks the test [then] fail, [else stop]."""
    argv = ["/bin/sh", "-c", script]
    assert plugin._runs_what_the_census_does_not_model(argv, str(REPO / "ci"), None), "marked unmeasured"
    assert directories_listed("subprocess.Popen", ("/bin/sh", argv, str(REPO / "ci"), None)) == []


def test_a_dollar_in_a_plain_argv_is_not_a_shell_expansion() -> None:
    """[if] `["git", "ls-files", "~x"]`, which no shell rewrites, is refused [then] its module can never read whole, so fail, [else stop]."""
    assert directories_listed("subprocess.Popen", ("git", ["git", "ls-files", "~x"], str(REPO), None)) == ["./**"]
    script = ["/bin/sh", "-c", 'git ls-files "$X"']  # the same words in a script are rewritten first: opaque
    assert directories_listed("subprocess.Popen", ("/bin/sh", script, str(REPO), None)) == []


@pytest.mark.skipif(os.name == "nt", reason="runs `sh -c` scripts and #! entry points, which need a POSIX shell")
def test_a_shell_child_leaves_its_module_unmeasured_end_to_end(tmp_path: Path) -> None:
    """[if] any `sh -c` script, even one that edits nothing, a Python whose #! skips site or a compiled program the
    census cannot read reads as measured, or a hooked Python or a program that lists nothing reads as unmeasured
    [then] a hidden listing or a needless PARTIAL, so fail, [else stop]."""
    cases = {
        ("/bin/sh", "-c", "FLAG=1; python x.py"): True,
        ("/bin/sh", "-c", "python -c pass"): True,
        ("/bin/sh", "-c", "unset PYTHONPATH; tool"): True,
        ("quiet",): True,  # its #! line's -S skips site: launched, it never checks in
        ("tool",): False,  # a console entry point whose #! names python: hooked, it checks in
        ("python", "-c", "pass"): False,
        ("plain",): True,  # a compiled program that is neither a Python nor a shell: it may list the tree unseen
        ("plain", "--version"): True,  # what a program does with a lone --version is its own
        ("git", "rev-parse", "--git-dir"): False,  # a git command that reads no directory listing
        ("git", "rev-list", "--parents", "-n", "1", "HEAD"): False,
        ("git", "rev-list", "--alternate-refs", "HEAD"): True,  # may run `core.alternateRefsCommand`
    }
    tools = tmp_path / "tools"  # the child PATH: these, then the venv's own bin, so `python` keeps its venv
    tools.mkdir()
    for name, text in {"tool": "#!/usr/bin/env python3\n", "quiet": f"#!{sys.executable} -S\n"}.items():
        (tools / name).write_text(text, encoding="utf-8")
        (tools / name).chmod(0o755)
    for name, target in {"git": shutil.which("git"), "plain": shutil.which("true")}.items():  # the hook runs git
        assert target is not None, name
        (tools / name).symlink_to(target)  # a real binary: a `#!/bin/sh` wrapper is itself an unread shell script
    child_path = os.pathsep.join([str(tools), str(Path(sys.executable).parent)])
    probes = REPO / ".tmp" / f"census_shell_child_{os.getpid()}_{tmp_path.name}"  # gitignored, inside the checkout
    probes.mkdir(parents=True)
    body = (
        "import os, subprocess\ndef test_it():\n    subprocess.run({argv!r}, env={{**os.environ, 'PATH': {tools!r}}})\n"
    )
    try:
        for i, argv in enumerate(cases):
            (probes / f"test_case_{i}.py").write_text(body.format(argv=list(argv), tools=child_path), encoding="utf-8")
        result = run_with_the_plugin([str(probes)], {**os.environ, OUT_DIR_ENV: str(tmp_path / "out")})
        assert result.returncode == 0, result.stdout[-2000:]
        census = load_census(tmp_path / "out")
    finally:
        shutil.rmtree(probes)
    prefix = probes.relative_to(REPO).as_posix()
    for i, (argv, dropped) in enumerate(cases.items()):
        assert (f"{prefix}/test_case_{i}.py" in census.incomplete) is dropped, argv


@pytest.mark.skipif(os.name == "nt", reason="preexec_fn and `env -u PATH` are POSIX only")
def test_a_stand_in_named_git_is_unmeasured_end_to_end(tmp_path: Path) -> None:
    """[if] a program named `git` that is not the git the census runs (a copy first on the child's PATH, given as
    `executable=`, behind `env PATH=`, put first on PATH by the test itself or by a hooked child's) reads as measured
    [then] what it lists stays unseen, so fail; or the real git reads as unmeasured [then] a needless PARTIAL, so
    fail, [else stop]."""
    fake = tmp_path / "fake"
    fake.mkdir()
    real = shutil.which("git")
    assert real is not None
    shutil.copy2(real, fake / "git")  # a real git, but another file than the one the census runs: it reads unseen
    path = str(fake)
    child = "import subprocess; subprocess.run(['git', 'rev-parse', '--git-dir'], capture_output=True)"
    cases = {
        f"subprocess.run(['git', 'rev-parse', '--git-dir'], env={{**os.environ, 'PATH': {path!r}}})": True,
        f"subprocess.run(['git', 'ls-files', 'apps'], env={{**os.environ, 'PATH': {path!r}}})": True,
        f"subprocess.run(['git', 'rev-parse', '--git-dir'], executable={str(fake / 'git')!r})": True,
        f"subprocess.run(['env', 'PATH={path}', 'git', 'rev-parse', '--git-dir'])": True,
        "subprocess.run(['env', '-u', 'PATH', 'git', 'rev-parse', '--git-dir'])": True,  # env's own lookup
        "subprocess.run(['git', 'rev-parse', '--git-dir'], capture_output=True)": False,
        "subprocess.run(['git', 'rev-parse', '--git-dir'], capture_output=True, preexec_fn=os.getpid)": True,
        # a test that puts the stand-in first on its own PATH, then runs git with that environment
        f"old = os.environ['PATH']; os.environ['PATH'] = {path!r} + os.pathsep + old; "
        "subprocess.run(['git', 'rev-parse', '--git-dir'], capture_output=True); os.environ['PATH'] = old": True,
        # a hooked Python child started with it first on PATH, which then runs git
        f"subprocess.run([sys.executable, '-c', {child!r}], env={{**os.environ, 'PATH': {path!r} + os.pathsep "
        "+ os.environ['PATH']}, check=True)": True,
    }
    probes = REPO / ".tmp" / f"census_stand_in_{os.getpid()}_{tmp_path.name}"  # gitignored, inside the checkout
    probes.mkdir(parents=True)
    try:
        for i, call in enumerate(cases):
            (probes / f"test_case_{i}.py").write_text(
                f"import os, subprocess, sys\ndef test_it():\n    {call}\n", "utf-8"
            )
        result = run_with_the_plugin([str(probes)], {**os.environ, OUT_DIR_ENV: str(tmp_path / "out")})
        assert result.returncode == 0, result.stdout[-2000:]
        census = load_census(tmp_path / "out")
    finally:
        shutil.rmtree(probes)
    prefix = probes.relative_to(REPO).as_posix()
    for i, (call, unmeasured) in enumerate(cases.items()):
        assert (f"{prefix}/test_case_{i}.py" in census.incomplete) is unmeasured, call


@pytest.mark.skipif(os.name == "nt", reason="os.openpty, the terminal a child writes to here, is POSIX only")
def test_a_git_command_writing_to_a_terminal_is_unmeasured_end_to_end(tmp_path: Path) -> None:
    """[if] a git command whose stdout is a terminal reads as measured [then] a pager its config names (`pager.<cmd>`,
    `git config --list`'s default) lists unseen, so fail; or one writing to a pipe, or given `-P`, reads as
    unmeasured [then] a needless PARTIAL, so fail, [else stop]."""
    cases = {
        ("['git', 'rev-parse', '--git-dir']", "os.openpty()[1]"): True,
        ("['git', '-P', 'rev-parse', '--git-dir']", "os.openpty()[1]"): False,
        ("['git', 'rev-parse', '--git-dir']", "subprocess.PIPE"): False,
        ("['git', 'config', '--get-all', 'core.bare']", "subprocess.DEVNULL"): False,
    }
    probes = REPO / ".tmp" / f"census_terminal_{os.getpid()}_{tmp_path.name}"  # gitignored, inside the checkout
    probes.mkdir(parents=True)
    body = "import os, subprocess\ndef test_it():\n    subprocess.run({argv}, stdout={stdout})\n"
    try:
        for i, (argv, stdout) in enumerate(cases):
            (probes / f"test_case_{i}.py").write_text(body.format(argv=argv, stdout=stdout), encoding="utf-8")
        result = run_with_the_plugin([str(probes)], {**os.environ, OUT_DIR_ENV: str(tmp_path / "out")})
        assert result.returncode == 0, result.stdout[-2000:]
        census = load_census(tmp_path / "out")
    finally:
        shutil.rmtree(probes)
    prefix = probes.relative_to(REPO).as_posix()
    for i, (case, unmeasured) in enumerate(cases.items()):
        assert (f"{prefix}/test_case_{i}.py" in census.incomplete) is unmeasured, case


@pytest.mark.parametrize(
    ("argv", "lists_nothing"),
    [
        (["git", "rev-parse", "--git-dir"], True),
        (["git", "-C", "apps", "rev-list", "--parents", "-n", "1", "HEAD"], True),
        (["git", "-P", "-c", "user.name=t", "rev-parse", "HEAD"], True),
        (["git", "-p", "rev-parse", "HEAD"], False),  # runs a pager program on a terminal
        (["git", "--paginate", "config", "--get", "user.name"], False),
        (["git", "-c", "pager.rev-parse=./scan", "rev-parse", "HEAD"], False),
        (["git", "--config-env=core.pager=SCAN", "rev-parse", "HEAD"], False),
        (["git", "--exec-path=/x", "rev-parse", "HEAD"], False),  # an option the census does not model
        (["git", "rev-parse", "--help"], False),  # runs `git help`, whose viewer is configurable
        (["git", "rev-list", "--format=%H", "-n", "1", "HEAD"], True),
        (["git", "rev-list", "--format=%G?", "HEAD"], False),  # verifies a signature through `gpg.program`
        (["git", "for-each-ref", "--format=%(signature)"], False),
        (["git", "rev-list", "--pretty=signed", "HEAD"], False),  # `pretty.signed` in config may hold `%G?`
        (["git", "rev-list", "--format=signed", "HEAD"], False),
        (["git", "rev-list", "--pretty=oneline", "HEAD"], True),  # git's own
        (["git", "rev-list", "--pretty=format:%H", "HEAD"], True),
        (["git", "cat-file", "-e", "HEAD^{commit}"], True),  # whether an object exists
        (["git", "cat-file", "-t", "HEAD"], True),
        (["git", "cat-file", "-p", "HEAD^{tree}"], False),  # prints a tree: a listing
        (["git", "cat-file", "--batch"], False),  # a tree on stdin prints too
        (["git", "cat-file", "--filters", "HEAD:README.md"], False),  # runs a smudge filter
        (["git", "cat-file", "--textc", "HEAD:README.md"], False),  # git expands an abbreviated option
        (["git", "cat-file", "-e", "--path=x"], False),
        (["git", "rev-list", "--objects", "HEAD"], False),  # names every path of every tree
        (["git", "rev-list", "--alternate-refs", "HEAD"], False),  # may run `core.alternateRefsCommand`
        (["git", "rev-list", "--alt", "HEAD"], False),  # git expands an abbreviated option
        (["git", "rev-list", "--indexed-objects", "HEAD"], False),  # an option the census does not model
        (["git", "rev-list", "--count", "--first-parent", "--no-merges", "a..b"], True),
        (["git", "rev-list", "--left-right", "--count", "a...HEAD"], True),
        (["git", "rev-list", "--max-parents=0", "-n40", "--reverse", "HEAD", "^x"], True),
        (["git", "hash-object", "README.md"], False),  # runs the clean filter
        (["git", "init", "-q"], False),  # copies a template directory
        (["git", "--help"], False),
        (["git", "version"], True),
        (["git", "--version"], False),  # an option, not an allowlisted command
        (["git", "config", "--get", "user.name"], True),
        (["git", "config", "--edit"], False),  # runs an editor
        (["git", "config", "--ed"], False),  # git expands an abbreviated option
        (["git", "config", "-le"], False),  # a short cluster
        (["git", "config", "edit"], False),  # the subcommand form
        (["plain", "--version"], False),  # what a program does with its own flags is its own
        (["rg", "--help"], False),
    ],
)
def test_only_a_git_command_that_reads_no_tree_needs_no_hook(argv: list[str], lists_nothing: bool) -> None:
    """[if] a git command that prints a tree or runs a filter, or any program's lone `--help`, needs no hook [then]
    a listing or a filter-run program hides unmarked, so fail, [else stop]."""
    assert plugin._argv_shows_what_it_lists(argv) is lists_nothing, argv


@pytest.mark.skipif(
    os.name == "nt", reason="a #! line picks the interpreter only on POSIX; Windows entry points are .exe"
)
def test_a_script_whose_shebang_names_python_counts_as_a_python(tmp_path: Path) -> None:
    """[if] a console entry point under a scrubbed env reads as non-Python [then] its listings vanish unmarked, so fail, [else stop]."""
    for name, first_line, expected in (("tool", "#!/usr/bin/env python3", True), ("plain", "#!/bin/sh", False)):
        (tmp_path / name).write_text(f"{first_line}\n", encoding="utf-8")
        (tmp_path / name).chmod(0o755)
        assert plugin._runs_a_python_script([name], None, {"PATH": str(tmp_path)}) is expected, name


def test_a_class_collection_listing_is_credited_to_its_module(tmp_path: Path) -> None:
    """[if] a listing made while a test class is collected (`pytest_generate_tests`) is dropped [then] fail, [else stop]."""
    probes = REPO / ".tmp" / f"census_class_{os.getpid()}_{tmp_path.name}"  # gitignored, inside the checkout
    probes.mkdir(parents=True)
    body = [
        "import os",
        "def pytest_generate_tests(metafunc):",
        '    os.listdir("apps")',
        '    metafunc.parametrize("x", [1])',
    ]
    body += ["class TestIt:", "    def test_it(self, x):", "        assert x == 1", ""]
    try:
        (probes / "test_in_a_class.py").write_text("\n".join(body), encoding="utf-8")
        result = run_with_the_plugin([str(probes)], {**os.environ, OUT_DIR_ENV: str(tmp_path / "out")})
        assert result.returncode == 0, result.stdout[-2000:]
        census = load_census(tmp_path / "out")
    finally:
        shutil.rmtree(probes)
    assert "apps" in census.reads.get(f"{probes.relative_to(REPO).as_posix()}/test_in_a_class.py", ())


def test_a_renamed_program_fails_loud(tmp_path: Path) -> None:
    """[if] `executable=` runs a scanner, git or a shell script under another argv[0], read by argv
    [then] its listing vanishes, so fail, [else stop]."""
    (tmp_path / "scan.sh").write_text("#!/bin/sh\nfind apps\n", encoding="utf-8")
    for executable, argv in (
        ("./scan.sh", ["alias"]),
        (str(tmp_path / "scan.sh"), ["scan.sh", "apps"]),  # its own basename: a PATH lookup would find nothing
        ("./scan.sh", ["./scan.sh"]),  # POSIX audits `run(["./scan.sh"])` with its own path as the executable
        (r"tools\SCAN.BAT", ["alias"]),
    ):  # judged by the file actually run: not measured, never refused
        assert directories_listed("subprocess.Popen", (executable, argv, str(tmp_path), None)) == []
        assert plugin._runs_what_the_census_does_not_model(plugin._argv_as_run(executable, argv), str(tmp_path), None)
    renamed = (
        ("/usr/bin/find", ["not-scanner", "apps"]),
        ("git", ["placeholder", "ls-files"]),
        (sys.executable, ["alias", "-S", "-c", "pass"]),  # a Python that skips the hook, named as something else
    )
    for executable, argv in renamed:
        with pytest.raises(RuntimeError, match="argv names"):
            directories_listed("subprocess.Popen", (executable, argv, str(REPO), None))
    assert directories_listed("subprocess.Popen", (sys.executable, ["python", "-c", "pass"], str(REPO), None)) == []


def test_unmeasured_markers_are_appended_so_a_reused_pid_keeps_both(tmp_path: Path) -> None:
    """[if] a later unmeasured marker from the same pid overwrites an earlier one [then] a module can read whole, so fail, [else stop]."""
    probes = REPO / ".tmp" / f"census_append_{os.getpid()}_{tmp_path.name}"  # gitignored, inside the checkout
    probes.mkdir(parents=True)
    grandchildren = "import subprocess, sys\nfor _ in range(2): subprocess.run([sys.executable, '-I', '-c', 'pass'])"
    call = f"subprocess.run([sys.executable, '-c', {grandchildren!r}], check=True)"  # a hooked child, two unhooked
    try:
        (probes / "test_two_unhooked.py").write_text(
            "\n".join(["import subprocess, sys", "def test_it():", f"    {call}", ""]), encoding="utf-8"
        )
        result = run_with_the_plugin([str(probes)], {**os.environ, OUT_DIR_ENV: str(tmp_path / "out")})
        assert result.returncode == 0, result.stdout[-2000:]
    finally:
        shutil.rmtree(probes)
    module = f"{probes.relative_to(REPO).as_posix()}/test_two_unhooked.py"
    markers = [f.read_text(encoding="utf-8").splitlines() for f in (tmp_path / "out" / "children").glob("*.unhooked")]
    assert markers == [[module, module]], "the child's second marker line was appended, not written over the first"


def test_a_scanner_or_shell_is_opaque_and_a_walk_it_does_not_model_fails_loud() -> None:
    """[if] a shell, a scanner (also behind a wrapper or launcher, with `-x` or `--pre`) is parsed instead of opaque, or
    `grep -r` or `git grep` reads as no walk [then] a read hides or is guessed, so fail, [else stop]."""
    opaque = (
        ["/bin/sh", "-c", "cd ..; rg PATTERN"],
        ["/bin/sh", "-c", "env -C ../apps rg PATTERN"],
        ["nice", "-n", "5", "rg", "PATTERN", "apps"],
        ["du", "--files0-from=roots.txt"],
        ["fd", "--base-directory", "apps", "x"],
        ["find", "apps", "-maxdepth", "1", "-maxdepth", "4"],
        ["tree", "-L", "2", "apps"],
        ["fd", "x", "apps", "-x", "git", "ls-files"],
        ["rg", "--pre=cat", "x"],
        ["uv", "run", "rg", "PATTERN", "apps"],
        ["uvx", "fd", "x", "apps"],
        ["pipx", "run", "tree", "apps"],
        ["bash", "-o", "pipefail", "-c", "cd ../apps; git ls-files"],
        ["cmd", "/c", "dir", "/s"],  # a built-in recursive listing: no child to audit
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-Command", "Get-ChildItem apps"],
        ["pwsh", "-c", "Get-ChildItem -Recurse"],
        ["ksh", "-c", "find apps"],
        ["fish", "-c", "find apps"],
        ["busybox", "sh", "-c", "find apps"],
    )
    for argv in opaque:
        assert plugin._runs_what_the_census_does_not_model(argv, str(REPO / "ci"), None), argv
        assert directories_listed("subprocess.Popen", (argv[0], argv, str(REPO / "ci"), None)) == [], argv
    walks = (
        ["grep", "-rn", "x", "apps"],
        ["git", "grep", "x"],  # `rgrep` is a `#!/bin/sh` script on Debian, so opaque there and refused elsewhere
        ["nice", "grep", "-rn", "x", "apps"],  # behind a wrapper
        ["timeout", "5", "git", "grep", "x"],
        ["grep", "-d", "recurse", "x", "apps"],
        ["grep", "--directories", "recurse", "x", "apps"],
        ["grep", "--recurs", "x", "apps"],  # getopt takes an unambiguous prefix
        ["grep", "--direc=recurse", "x", "apps"],
        ["grep", "--dir", "rec", "x", "apps"],
    )
    for argv in walks:
        with pytest.raises(RuntimeError, match="a walk it does not model"):
            directories_listed("subprocess.Popen", (argv[0], argv, str(REPO), None))
    assert directories_listed("subprocess.Popen", ("grep", ["grep", "-n", "x", "f.txt"], str(REPO), None)) == []


def test_a_recursive_read_needs_a_subtree_approval() -> None:
    """[if] an exact `dir` approval covers a `find dir` or `git ls-files` walk [then] a new whole-tree guard hides, so fail, [else stop]."""
    assert not _within("apps/**", ("apps",)), "one listing of apps does not approve walking it"
    assert not _within("./**", (".",))
    assert _within("apps/**", ("apps/**",)) and _within("apps/webui/**", ("apps/**",)) and _within("apps/**", ("./**",))
    assert _within("apps", ("apps",)) and _within("apps", ("apps/**",)) and _within("apps/webui", ("apps/**",))
    assert not _within("apps/**", ("apps/webui/**",)), "a deeper subtree does not cover its parent"


@pytest.mark.parametrize(
    ("command", "unhooked"),
    [
        (["python3", "-I", "-c", "pass"], True),
        (["python3", "-Werror::ImportWarning", "x.py"], False),  # an attached `-W` value is not `-I`/`-E`/`-S`
        (["python3", "-Xdev", "-IWerror", "x.py"], True),  # the flags before `-W` are flags
        (["python3", "-sW", "error", "-I", "x.py"], True),  # `-W`'s value is the next word, then `-I`
        (["python3", "--check-hash-based-pycs", "always", "-I", "parent.py"], True),  # past a long option's value
        (["python3", "--check-hash-based-pycs=never", "-S", "x.py"], True),
        (["python3", "--check-hash-based-pycs", "always", "x.py", "-I"], False),  # the script's own argument
        (["env", "-i", "/usr/bin/python3", "-c", "pass"], True),  # an empty environment drops the hook
        (["env", "-u", "PYTHONPATH", "python3", "x.py"], True),
        (["PYTHONPATH=elsewhere", "python3", "x.py"], True),
        (["python3", "-u", "x.py"], False),  # python's own -u is unbuffered output, not env's unset
        (["nice", "python3", "x.py"], False),
        (["env", "FLAG=1", "python3", "x.py"], False),
        (["env", "-i", "pytest", "-q"], True),  # a launcher starts a Python in the emptied environment
        (["env", "-uPYTHONPATH", "uv", "run", "x.py"], True),  # an attached -u
        (["env", "-vi", "python3", "x.py"], True),  # a short cluster
        (["env", "--ignore-env", "python3", "x.py"], True),  # a prefix env accepts
        (["env", "--un", "PYTHONPATH", "python3", "x.py"], True),
        (["env", "-v", "python3", "x.py"], False),  # verbose only
        (["pytest", "-q", "-I"], False),  # a launcher's own options are not Python's
        (["uv", "run", "python", "-I", "-c", "pass"], True),  # but the Python it runs reads its own
        (["uv", "run", "--isolated", "python3", "x.py"], False),
        (["py", "-I", "x.py"], True),  # the Windows launcher forwards Python's options
        (["py", "-3.12", "x.py"], False),
    ],
)
def test_a_python_child_that_loses_the_hook_is_recognized(command: list[str], unhooked: bool) -> None:
    """[if] a Python child whose environment drops the hook counts as measured [then] its listings vanish unseen, so fail, [else stop]."""
    assert plugin._python_without_the_hook(command) is unhooked


@pytest.mark.parametrize(
    ("event", "args", "expected"),
    [
        ("os.system", ("git -C apps ls-files",), []),
        ("os.posix_spawn", ("/usr/bin/git", ["git", "-C", "apps", "ls-files"], {}), ["apps/**"]),
        ("os.spawn", (0, "/usr/bin/git", ["git", "-C", "apps", "ls-files"], None), ["apps/**"]),
        ("os.exec", ("/usr/bin/git", ["git", "-C", "apps", "ls-files"], None), ["apps/**"]),
        ("os.posix_spawn", ("/usr/bin/find", ["find", "apps"], {}), []),
        ("subprocess.Popen", ("/bin/bash", ["/bin/bash", "-ec", "git -C apps ls-files"], None, None), []),
        ("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "--", "find apps"], None, None), []),
    ],
)
def test_every_way_of_starting_a_child_is_placed(event: str, args: tuple[object, ...], expected: list[str]) -> None:
    """[if] posix_spawn, spawn* or exec* escapes placement, or os.system, a shell script or a scanner is parsed instead of
    marking its module unmeasured [then] a read vanishes or is guessed, so fail, [else stop]."""
    assert os.getcwd() == str(REPO), "these children run where the test process runs"
    assert directories_listed(event, args) == expected


def test_ls_files_counts_only_as_the_git_subcommand() -> None:
    """[if] `ls-files` as an argument (`git log -- ls-files`) reads as a listing [then] a bogus reader and GAP appear, so fail, [else stop]."""
    for argv in (["git", "log", "--", "ls-files"], ["git", "show", "ls-files"], ["git", "-c", "x=ls-files", "status"]):
        assert directories_listed("subprocess.Popen", ("git", argv, str(REPO), None)) == [], argv
    git_c = ["git", "-c", "core.quotepath=off", "-C", "apps", "ls-files"]
    assert directories_listed("subprocess.Popen", ("git", git_c, str(REPO), None)) == ["apps/**"]


def test_real_git_ls_tree_from_a_subdirectory_lists_only_that_subdirectory() -> None:
    """[if] real git's pathless `ls-tree` run in `apps` lists anything outside `apps` [then] the census's `apps`
    placement of it under-reports the read, so fail, [else stop]."""
    from_apps = plugin.git_output("ls-tree", "-r", "--full-name", "--name-only", "-z", "HEAD", root=REPO / "apps")
    under_apps = plugin.git_output("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "apps", root=REPO)
    names = set(from_apps.decode().strip("\0").split("\0"))
    assert names and all(name.startswith("apps/") for name in names)
    assert names == set(under_apps.decode().strip("\0").split("\0"))


def test_git_ls_tree_is_placed_where_its_tree_lies(tmp_path: Path) -> None:
    """[if] an argv `git ls-tree` is refused [then] every test that lists a commit's tree fails under the census, so
    fail; or it records less than it lists (a `-r` walk as one directory, a `rev:path` tree-ish, `--full-tree` or a
    climbing path, or a tree object that is not a commit's, as where it runs) [then] a reader hides, so fail; or one
    run outside the checkout counts [then] a temporary repository's tree reads as the checkout, so fail, [else stop]."""
    git = shutil.which("git")

    def listed(argv: list[str], cwd: str = str(REPO)) -> list[str]:
        return directories_listed("subprocess.Popen", (git, argv, cwd, None))

    assert listed(["git", "-C", str(REPO), "ls-tree", "-r", "-z", "HEAD"]) == ["./**"]
    assert listed(["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "(private path)/"]) == ["(private path)/**"]
    assert listed(["git", "-C", "apps", "ls-tree", "HEAD"]) == ["apps"]  # one level of it
    assert listed(["git", "-C", "apps", "ls-tree", "-rt", "HEAD"]) == ["apps/**"]
    subtree = plugin.git_output("rev-parse", "HEAD:apps").decode().strip()  # lists `apps`, wherever git runs
    for whole in (
        ["git", "ls-tree", subtree],
        ["git", "-C", "apps", "ls-tree", "-r", "HEAD^{tree}"],
        ["git", "-C", "apps", "ls-tree", "-r", "HEAD:docs"],
        ["git", "-C", "apps", "ls-tree", "--full-tree", "-r", "HEAD"],
        ["git", "-C", "apps", "ls-tree", "-r", "HEAD", "../docs"],
    ):
        assert listed(whole) == ["./**"], whole
    assert listed(["git", "ls-tree", "preview", "--", "file.txt"], cwd=str(tmp_path)) == []  # another repository
    # the subcommand is the word after git's own options; a later `ls-files` is only an operand
    assert listed(["git", "-C", "apps", "ls-tree", "HEAD:docs", "ls-files"]) == ["./**"]
    assert "./**" in listed(["git", "ls-tree", "HEAD", "--", "ls-files", "apps/foo"])
    icase = ["git", "-C", "apps", "ls-tree", "-r", "HEAD", "--", "audio-engine"]  # may match `Audio-Engine` too
    assert listed(["git", "--icase-pathspecs", *icase[1:]]) == ["apps/**"]
    for env in ({**os.environ, "GIT_ICASE_PATHSPECS": "1"}, {**os.environ, "git_glob_pathspecs": "1"}):
        assert directories_listed("subprocess.Popen", (git, icase, str(REPO), env)) == ["apps/**"], env
    assert directories_listed(
        "subprocess.Popen", (None, ["env", "GIT_ICASE_PATHSPECS=1", *icase], str(REPO), None)
    ) == ["apps/**"]
    assert plugin._fsmonitor_hooks(b"pager.ls-tree=less\npager.log=less\n") == [
        "pager.ls-tree=less"
    ]  # a terminal pager
    with pytest.raises(RuntimeError, match="an ls-tree option it does not model"):
        listed(["git", "ls-tree", "--no-such-option", "HEAD"])
    with pytest.raises(RuntimeError, match="behind a wrapper"):
        directories_listed("subprocess.Popen", (None, ["xargs", "git", "ls-tree", "-r", "HEAD"], str(REPO), None))


def test_git_ls_files_counts_where_it_runs(tmp_path: Path) -> None:
    """[if] `git -C <tmp>`, an argv `git ls-files` where it runs or a non-git `ls-files` is misread [then] reads hide, so fail, [else stop]."""
    git = "/usr/bin/git"
    aliased_env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.files", "GIT_CONFIG_VALUE_0": "ls-files"}
    for aliased, env in (
        (["git", "-c", "alias.files=ls-files", "files"], None),
        (["git", "--config-env=Alias.f=VAR", "f"], None),
        (["git", "files"], {**os.environ, **aliased_env}),  # or from a config file: git's own commands only
        (["git", "--attr-source", "HEAD", "grep", "TODO"], None),  # an unmodelled option swallows the subcommand
    ):
        with pytest.raises(RuntimeError, match="is no git command"):
            directories_listed("subprocess.Popen", (git, aliased, str(REPO), env))
    for from_file in ("--pathspec-from-file=specs", "--pathspec-from-f=specs"):  # git takes a unique prefix
        with pytest.raises(RuntimeError, match="pathspecs from a file"):
            directories_listed("subprocess.Popen", (git, ["git", "ls-files", from_file], str(REPO), None))
    moved_by_config = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree", "GIT_CONFIG_VALUE_0": str(REPO)}
    for env in ({"git_work_tree": "/elsewhere"}, moved_by_config, {"GIT_CONFIG_GLOBAL": "/tmp/moved.gitconfig"}):
        with pytest.raises(RuntimeError, match="location is in its env"):  # names ignore case, as on Windows
            directories_listed("subprocess.Popen", (git, ["git", "ls-files"], str(REPO / "ci"), env))
    assert directories_listed("subprocess.Popen", (git, ["git", "-C", "alias.d", "status"], str(REPO), None)) == []
    for prefixed in (
        ["env", "git", "-C", "apps", "ls-files"],
        ["env", "--unset=HOME", "git", "-C", "apps", "ls-files"],
    ):
        assert directories_listed("subprocess.Popen", (None, prefixed, str(REPO), None)) == ["apps/**"], prefixed
    with pytest.raises(RuntimeError, match="behind a wrapper"):
        directories_listed("subprocess.Popen", (None, ["nice", "git", "-C", "apps", "ls-files"], str(REPO), None))
    assert directories_listed("subprocess.Popen", (git, ["git", "ls-files"], str(REPO), None)) == ["./**"]
    assert directories_listed("subprocess.Popen", (git, ["git", "-C", str(tmp_path), "ls-files"], None, None)) == []
    assert directories_listed("subprocess.Popen", (git, ["git", "-C", "apps", "ls-files"], str(REPO), None)) == [
        "apps/**"
    ]
    ci = str(REPO / "ci")
    assert directories_listed("subprocess.Popen", (git, ["git", "ls-files", "../apps"], ci, None)) == ["./**"], (
        "escapes"
    )
    assert directories_listed("subprocess.Popen", (git, ["git", "ls-files", ":(top)apps"], ci, None)) == ["./**"]
    windows_git = r"C:\Program Files\Git\cmd\git.exe"
    line = f'"{windows_git}" -C apps ls-files -z'  # Windows audits the command line Popen built
    assert directories_listed("subprocess.Popen", (windows_git, line, str(REPO), None)) == ["apps/**"]
    cmd = r"C:\Windows\system32\cmd.exe"  # a Windows shell line: not measured, never parsed
    assert directories_listed("subprocess.Popen", (cmd, f'{cmd} /c "git ls-files"', str(REPO), None)) == []
    attached = ["git", "-C..", "ls-files"]  # git also takes `-C<dir>` attached
    assert directories_listed("subprocess.Popen", (git, attached, str(REPO / "ci"), None)) == ["./**"]
    assert directories_listed("subprocess.Popen", (git, ["git", "-Capps", "ls-files"], str(REPO), None)) == ["apps/**"]
    inside = ["git", "ls-files", "--", "workflows", "-z"]  # after `--`, `-z` is a pathspec too: no such file
    assert directories_listed("subprocess.Popen", (git, inside, str(REPO / ".github"), None)) == [
        ".github/**",
        ".github/workflows/**",
    ]
    folded = ["git", "-C", ".github", "-C", "../apps", "ls-files"]
    assert directories_listed("subprocess.Popen", (git, folded, str(REPO), None)) == ["apps/**"], "-C folds as git does"
    for paged in (["git", "-p", "ls-files"], ["git", "--paginate", "ls-files"]):  # a pager program on a terminal
        with pytest.raises(RuntimeError, match="git option -"):
            directories_listed("subprocess.Popen", (git, paged, str(REPO), None))
    for hidden in (["git", "--super-prefix", "sub", "ls-files"], ["git", "--attr-source", "HEAD", "ls-files"]):
        with pytest.raises(RuntimeError, match=r"git option --|is no git command"):  # may hide the subcommand
            directories_listed("subprocess.Popen", (git, hidden, str(REPO), None))
    for worktree in (
        ["git", "-c", "core.worktree=/other", "ls-files"],
        ["git", "-cCore.Worktree=/x", "ls-files"],
        ["git", "-c", "include.path=/tmp/move.cfg", "ls-files"],  # loads config that may move it
        ["git", "-c", "includeIf.gitdir:/x/.path=/tmp/move.cfg", "ls-files"],
        ["git", "-C", "apps", "-c", "core.fsmonitor=/tmp/helper", "ls-files"],  # a hook that may list anything
        ["git", "-ccore.fsMonitor=/tmp/helper", "ls-files"],
        ["git", "--config-env=core.fsmonitor=HELPER", "ls-files"],
        ["git", "-c", "pager.ls-files=/tmp/helper", "ls-files"],  # a pager program on a terminal
        ["git", "--config-env=pager.ls-files=HELPER", "ls-files"],
        ["git", "--config-env", "core.pager=HELPER", "ls-files"],
    ):
        with pytest.raises(RuntimeError, match="moves it"):  # config that moves the tree it lists, or runs a program
            directories_listed("subprocess.Popen", (git, worktree, str(REPO), None))
    for moved in (
        ["git", "--git-dir=x/.git", "ls-files"],
        ["env", "GIT_WORK_TREE=x", "git", "ls-files"],
        ["env", "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=core.worktree", "GIT_CONFIG_VALUE_0=/x", "git", "ls-files"],
        ["GIT_CONFIG_GLOBAL=/tmp/moved.gitconfig", "git", "ls-files"],  # a prefix, as `env=` names are
        ["env", "GIT_COMMON_DIR=/x", "git", "ls-files"],  # git reads its config there
        ["GIT_INDEX_FILE=/tmp/other.index", "git", "ls-files"],  # lists another index
    ):
        with pytest.raises(RuntimeError, match="moves the repository"):
            directories_listed("subprocess.Popen", (moved[0], moved, str(REPO), None))
    globbed = ["git", "ls-files", "*.lock", "**/uv.lock"]  # git, not a shell, matches these pathspecs
    assert directories_listed("subprocess.Popen", (git, globbed, str(REPO), None)) == ["./**"]
    assert directories_listed("subprocess.Popen", (git, ["git", "status"], str(REPO), None)) == []
    wrapped = ["env", "--unset=HOME", "FLAG=1", "git", "ls-files"]
    assert directories_listed("subprocess.Popen", ("/usr/bin/env", wrapped, str(REPO), None)) == ["./**"]
    relocated = ["env", "-C", "apps", "git", "ls-files"]  # env moves it: not measured, never parsed
    assert plugin._runs_what_the_census_does_not_model(relocated, str(REPO), None)
    assert directories_listed("subprocess.Popen", ("/usr/bin/env", relocated, str(REPO), None)) == []
    assert directories_listed("subprocess.Popen", ("/bin/echo", ["echo", "ls-files"], str(REPO), None)) == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="shell=True runs `/bin/sh -c <script>` on POSIX; Windows runs `cmd /c`, which the census refuses",
)
def test_a_real_shell_git_ls_files_leaves_its_module_unmeasured() -> None:
    """[if] `subprocess.run("git ls-files", shell=True)` escapes the real audit hook or is parsed into reads [then] a
    shell reader hides or is guessed, so fail, [else stop]."""
    probe = (
        "import subprocess, sys\n"
        "from scripts import pytest_tree_read_census as p\n"
        "p._State.module = 'tests/probe.py'\n"
        "p._State.carrier = ('<no hook>', '<no reads>')  # no plugin run, so no child hook\n"
        "sys.addaudithook(p._audit)\n"
        "subprocess.run('git ls-files -z > /dev/null', shell=True, cwd=p.REPO / 'apps', check=True)\n"
        "shell = 'tests/probe.py' in p._State.incomplete\n"
        "p._State.incomplete.clear()\n"
        "subprocess.run(['git', 'ls-files', '-z'], cwd=p.REPO / 'scripts', check=True, capture_output=True)\n"
        "print(sorted(p._State.reads.get('tests/probe.py', ())), shell, 'tests/probe.py' in p._State.incomplete)\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "['scripts/**'] True False", (
        "the shell alone leaves the module unmeasured; the argv git counts where it runs and leaves it measured"
    )
