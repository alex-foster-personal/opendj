"""Pytest plugin: record which test modules list directories of the real repository tree.

Registered as a plugin only on request (`uv run --locked python -m pytest -p scripts.pytest_tree_read_census`);
a normal pytest run may import it (the census report does) but never registers its hooks.
`scripts/ci_tree_read_census.py` runs it and compares the result with `ci/pr-always-run.txt`.

A `sys.addaudithook` hook sees every listing Python code makes, however it is spelled: `os.scandir` and
`os.listdir` underlie `glob.glob`, `Path.glob`, `Path.rglob`, `Path.iterdir` and `os.walk`, and
`subprocess.Popen` carries any `git ls-files`. Each listing under the checkout is credited to the
test module being collected or run at the time. Listings made by the import system (its path
cache, and `importlib.metadata` searching every `sys.path` entry, which pytest extends with each
conftest directory) are not tree reads and are dropped here. Listings of untracked directories such
as tool caches are recorded; the report drops them when it keeps only tracked directories.

Each pytest process writes `<TREE_READ_CENSUS_DIR>/<worker>.json`: `gw<N>` per xdist worker, then
`main` for the run's own process, last, with its exit status, how each worker shut down and the
checkout's snapshot at the start and the end, so the report can refuse a run that was interrupted,
lost a worker, mixed with another or ran on different code. Fails loud:
loading the plugin without that directory set is an error, because a census that writes nowhere
would look like a census that found nothing, and so is a directory already holding census files.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import multiprocessing.util
import os
import posixpath
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import FrameType
from typing import Any, ClassVar

import pytest

REPO = Path(__file__).resolve().parents[1]
OUT_DIR_ENV = "TREE_READ_CENSUS_DIR"
CHILD_MODULE_ENV = "TREE_READ_CENSUS_MODULE"  # the running test module, or `<session>`: what Python children credit
CHILD_READS_ENV = "TREE_READ_CENSUS_CHILDREN"  # where each Python child appends its reads, one file per pid
CHILD_HOOK_DIR = ".child_hook"  # holds the sitecustomize that loads this hook into every Python child
CHILD_READS_DIR = "children"
MULTIPROCESSING_STARTS = {  # the `-c` code multiprocessing's `spawnv_passfds` runs, which raises no audit event
    "from multiprocessing.spawn import spawn_main": "spawned",  # a spawn-context Process: its test's own child
    "from multiprocessing.resource_tracker import main": "helper",  # each outlives the test that started it
    "from multiprocessing.forkserver import main": "helper",  # and serves later tests: its forks are unplaced
}
CENSUS_GIT_FILE = "census_git.txt"  # in CHILD_HOOK_DIR: the git the controller pinned, which children trust too
STARTUP_CODE_FILE = "startup_code.txt"  # in CHILD_HOOK_DIR, one per line; not .json, which reads as a census part
SESSION_READS = "<session>"  # listings by repository code while no test module is collected or run
PYTHON_SITE_OFF = frozenset("IES")  # -I, -E and -S each stop a Python child loading the census sitecustomize
PYTHON_OPTIONS_WITH_VALUES = frozenset({"-W", "-X", "--check-hash-based-pycs"})
SHORT_OPTION_CLUSTER = re.compile(r"-([^WX]*)([WX]?)(.*)", re.DOTALL)  # flags, then `-W`/`-X` and its value
PYTHON_LAUNCHERS = frozenset({"uv", "uvx", "pytest", "pip", "pipx", "py"})  # each starts a Python
NATIVE_LAUNCHERS = frozenset({"uv", "uvx"})  # compiled, so they read the project before their Python, unseen
EXECNET_BOOTSTRAP = "import sys;exec(eval(sys.stdin.readline()))"  # execnet's `popen_bootstrapline`
HOOK_DROPPING_ENV = frozenset({"-", "-i", "--ignore-environment", "-u", "--unset"})  # env options before a Python
HOOK_DROPPING_PREFIXES = ("--unset=", "PYTHONPATH=", "TREE_READ_CENSUS_")
SITECUSTOMIZE = """\
# Written by the tree-read census: loads its audit hook into each Python child of a measured test.
import os, sys
# An execnet gateway (an xdist worker) runs the census as its own pytest plugin; one a test starts is a counted
# launch that never checks in, so its module reads PARTIAL.
if os.environ.get({module_env!r}) and {bootstrap!r} not in sys.orig_argv:
    import importlib.util
    try:
        _spec = importlib.util.spec_from_file_location("tree_read_census_child", {plugin!r})
        _census = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_census)
        _census.install_child_hook()
    except Exception:  # a child without the hook (no pytest, an older Python) is recorded: its module reads PARTIAL
        with open(os.path.join(os.environ[{reads_env!r}], f"{{os.getpid()}}.unhooked"), "a", encoding="utf-8", newline="\\n") as _marker:
            _marker.write(os.environ[{module_env!r}] + "\\n")
import importlib.machinery, importlib.util, sys  # then run the sitecustomize this one shadows, as Python would:
_here, _after = os.path.dirname(os.path.abspath(__file__)), False  # the first after this one on sys.path, skipping
for _entry in sys.path:  # another census copy (a nested census), which would run this one again
    if not _after:
        _after = os.path.abspath(_entry or ".") == _here
        continue
    _shadowed = importlib.machinery.PathFinder.find_spec("sitecustomize", [_entry])
    if _shadowed is None or _shadowed.loader is None or _shadowed.origin is None:
        continue
    with open(_shadowed.origin, "rb") as _source:
        if _source.readline().startswith(b"# Written by the tree-read census"):
            continue
    sys.modules["sitecustomize"] = _module = importlib.util.module_from_spec(_shadowed)  # as an import leaves it
    _shadowed.loader.exec_module(_module)
    break
"""
STARTUP_PROBE = """\
import json, os, sys
seen = []
def _audit(event, args):
    if event in {"os.listdir", "os.scandir"}:
        where = args[0] if args and args[0] is not None else "."
        seen.append(["fd" if isinstance(where, int) else os.path.realpath(os.fsdecode(where)), event])
    elif event in spawns or event.startswith("os.fork"):
        seen.append(["spawn", event])
spawns = set(json.loads(sys.argv[2]))  # the census's own SPAWN_EVENTS, `os.startfile` too
out_path, start, directories = sys.argv[1], json.loads(sys.argv[3]), sys.argv[4:]
sys.argv, sys.orig_argv = start["argv"], start["orig_argv"]  # the start this replays, which startup code may read
sys.addaudithook(_audit)
import site  # under -S, importing it runs no `.pth` file (it reads `sys.flags`, so before they change below)
site.ENABLE_USER_SITE = start["user_site"]  # a normal start decides it before any `.pth` file runs
flags = sys.flags
class _Flags(tuple):  # a start without `-S`, which only lets this probe hook in before any startup code
    no_site = 0
    def __getattr__(self, name):
        return getattr(flags, name)
sys.flags = _Flags(0 if name == "no_site" else value for name, value in zip(flags.__match_args__, flags))
for directory in directories:
    site.addsitedir(directory)
with open(out_path, "w", encoding="utf-8", newline="\\n") as out:
    json.dump(seen, out)
"""
LISTING_EVENTS = frozenset({"os.scandir", "os.listdir"})
SPAWN_EVENTS = frozenset(  # `os.startfile` (Windows) opens an associated application: never read, so unmeasured
    {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.startfile", "os.startfile/2"}
    | {"_winapi.CreateProcess"}  # multiprocessing's Windows start: no environment in its audit, so unread
)
SHELLS = frozenset(  # POSIX and other Unix shells, busybox (its `sh` applet), and the Windows shells
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "ash",
        "ksh",
        "mksh",
        "yash",
        "fish",
        "csh",
        "tcsh",
        "busybox",
        "cmd",
        "powershell",
        "pwsh",
    }
)
SpawnCwd = str | bytes | os.PathLike[str] | os.PathLike[bytes] | None  # what `subprocess.Popen` accepts
TREE_SCANNERS = frozenset({"find", "rg", "fd", "fdfind", "tree", "du", "ls"})  # programs that list directories
ALWAYS_PYTHON = frozenset({"py", "pytest", "pip", "pipx"})  # programs that are a Python themselves
COMMAND_WRAPPERS = frozenset(
    {
        "env",
        "nice",
        "timeout",
        "xargs",
        "time",
        "nohup",
        "stdbuf",
        "ionice",
        "sudo",
        "exec",
        "command",
        "setsid",
        "flock",
    }
)  # each runs the command its later words name; not GNU `parallel`, which runs each line through `$SHELL -c`
WALKERS = frozenset({"grep", "egrep", "fgrep", "rgrep", "git"})  # may walk the tree in a way not modelled
PLACED_PROGRAMS = TREE_SCANNERS | SHELLS | COMMAND_WRAPPERS | {"git"}  # programs the census reads argv for
NOT_TRAVERSING = frozenset({"--version", "--help"})  # a scanner so started is not opaque, but still unmeasured
GIT_LISTS_NOTHING = frozenset(  # git commands that read refs or config, never a tree listing or a filter program
    {"rev-parse", "config", "rev-list", "for-each-ref", "show-ref", "symbolic-ref", "var", "version"}
)  # not `hash-object` (runs clean filters) or `init` (copies a template directory)
REV_LIST_OPTIONS = frozenset(  # `git rev-list` options that walk commits only: never `--alternate-refs`, which may
    {  # run `core.alternateRefsCommand`, nor `--objects`, which names every path of every tree
        "--parents", "--count", "--first-parent", "--no-merges", "--merges", "--reverse", "--left-right",
        "--max-count", "--max-parents", "--min-parents", "--topo-order", "--date-order", "--format", "--pretty", "-n",
    }
)  # fmt: skip
CAT_FILE_ANSWERS = frozenset({"-e", "-t", "-s"})  # whether, which type, what size; `-p` prints a tree, `--filters` runs
GIT_REPOSITORY_MOVES = ("--git-dir", "--work-tree")  # git options that list another tree than -C names
GIT_OPTIONS_WITH_VALUES = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
GIT_LISTERS = frozenset({"ls-files", "ls-tree"})  # argv git commands the census places where they run
LS_TREE_FLAG_LETTERS = "rdtlz"  # `git ls-tree -r`, `-d`, `-t`, `-l` and `-z`
LS_TREE_LONG_OPTIONS = frozenset(
    {"--long", "--name-only", "--name-status", "--object-only", "--full-name", "--full-tree", "--abbrev"}
)
LS_FILES_FLAGS = frozenset(  # `git ls-files` options that take no value and widen no pathspec
    {"-z", "-c", "--cached", "-s", "--stage", "-t", "-v", "-f", "--full-name", "--error-unmatch", "--deduplicate"}
)
PATHSPEC_SPECIALS = frozenset("*?[\\")  # a glob, or an escape, may match at any depth
PATHSPEC_FLAGS = frozenset({"--icase-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--literal-pathspecs"})
PATHSPEC_ENV = frozenset({"GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS", "GIT_LITERAL_PATHSPECS"})
PRETTY_FORMATS = frozenset(  # git's own `--pretty` names; any other bare name is a `pretty.<name>` from config
    {"oneline", "short", "medium", "full", "fuller", "reference", "email", "mboxrd", "raw"}
)
GIT_FALSE = frozenset({"false", "no", "off", "0", ""})  # every spelling git reads as a false boolean
GIT_CONFIG_REFUSED = (  # moves the tree it lists, or runs a hook or pager program
    "core.worktree",
    "include.path",
    "includeif.",
    "core.fsmonitor",
    "core.pager",
    "pager.",
    "filter.",  # a clean filter `ls-files -m` may run
)
LS_FILES_RUNNERS = ("--modified", "--recurse-submodules")  # may run a clean filter, or each submodule's git
GIT_FLAGS = frozenset(  # git's global options that take no value and leave where ls-files lists alone; not `-p`
    # or `--paginate`, which run a pager program on a terminal
    {
        "-P",
        "--no-pager",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-advice",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    }
)
NO_PAGER = frozenset({"-P", "--no-pager"})  # git never starts a pager with either
BATCH_SUFFIXES = (".cmd", ".bat")  # Windows runs these through cmd.exe
GIT_LOCATION_ENV = (  # the same, set in the child's environment; the common dir holds its config, the index its list
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")  # a shell or `env` NAME=value prefix
ENV_FLAGS = frozenset({"-", "-i", "--ignore-environment", "--"})
ENV_UNSET = frozenset({"-u", "--unset"})  # each takes the NAME as its next word
ENV_VALUE_LETTERS = "uCPS"  # env's short options that take the rest of a cluster, or the next word, as a value
IMPORT_MACHINERY = ("importlib._bootstrap", "importlib/metadata/", "importlib_metadata/")  # frozen, stdlib, backport
METADATA_SEARCH = ("importlib/metadata/", "importlib_metadata/")  # lists sys.path entries, unless given a `path=`


class _State:
    module: ClassVar[str | None] = None  # repository-relative test module being collected or run
    reads: ClassVar[dict[str, set[str]]] = {}
    measured: ClassVar[set[str]] = set()
    incomplete: ClassVar[set[str]] = set()  # some test phase did not pass, so a body may have stopped early
    collected: ClassVar[dict[str, set[str]]] = {}  # module -> ids of the tests collected to run
    yielded: ClassVar[dict[str, set[str]]] = {}  # module -> ids collection yielded, before any hook filters them
    ran: ClassVar[dict[str, set[str]]] = {}  # module -> ids of the tests whose body ran and passed
    workers: ClassVar[dict[str, str | None]] = {}  # controller only: xdist worker id -> its shutdown error
    snapshot: ClassVar[str | None] = None  # controller only: the checkout when the run started
    exitstatus: ClassVar[int | None] = None  # the session's, kept for the write after every unconfigure
    session: ClassVar[pytest.Session | None] = None  # whose final status the write reads
    child_reads: ClassVar[Path | None] = None  # in a Python child only: the file its reads append to
    git: ClassVar[str | None] = None  # the git the census runs and trusts, pinned before any test runs
    carrier: ClassVar[tuple[str, str] | None] = None  # the hook directory and the children's reads directory
    older_threads: ClassVar[set[threading.Thread]] = set()  # alive before this module phase, besides its own
    fork: ClassVar[threading.local] = threading.local()  # `.unplaced`, set by each forking thread for its child
    lineage_unknown: ClassVar[bool] = False  # an older thread lives: a thread born in this phase may be its descendant
    own_spawn: ClassVar[threading.local] = threading.local()  # `.active` while the census runs its own git


# -----------------------------------------------------------------------------
def git_output(*args: str, root: Path = REPO) -> bytes:
    """`git <args>` run in `root` by the git that PATH resolves, named absolutely; no git is an error."""
    return _git_with(list(args), root, os.environ)


def census_git() -> str:
    """The git the census runs and trusts, resolved from PATH once: by the controller and each worker before any
    conftest loads, and in a hooked child from the controller's file, so a test that later puts another `git`
    first on PATH (its own or a child's) changes neither. No git is an error."""
    if _State.git is None:
        _State.git = shutil.which("git")
        if _State.git is None:
            raise RuntimeError("the tree-read census needs git on PATH")
    return _State.git


def _git_with(args: list[str], root: Path | str, env: Mapping[str, str]) -> bytes:
    """`git <args>` in `root` with `env`, less its `GIT_*` names (no GIT_DIR, GIT_CONFIG_*), never a test's child."""
    git = census_git()
    clean = {k: v for k, v in env.items() if not k.upper().startswith("GIT_")}
    _State.own_spawn.active = True  # the hook may run this mid-audit (`_git_commands`): not a test's child
    try:
        return subprocess.run([git, *args], cwd=root, env=clean, capture_output=True, check=True).stdout
    finally:
        _State.own_spawn.active = False


def checkout_snapshot(root: Path = REPO) -> str:
    """The checkout's identity: HEAD, every change to tracked files and each untracked, unignored file's
    bytes. Census runs with equal snapshots ran the same test code."""

    entries = git_output("ls-files", "-v", "-z", root=root).split(b"\0")
    if hidden := [e[2:].decode() for e in entries if e[:1].islower() or e[:1] == b"S"]:  # `git diff` skips them
        raise RuntimeError(
            f"the tree-read census cannot snapshot assume-unchanged or skip-worktree paths: {hidden[:5]}"
        )
    digest = hashlib.sha256(
        git_output("rev-parse", "HEAD", root=root)
        + git_output("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv", root=root)
    )
    untracked = git_output("ls-files", "--others", "--exclude-standard", "-z", root=root)
    for name in sorted(filter(None, untracked.split(b"\0"))):
        path = root / os.fsdecode(name)
        if path.is_dir() and not path.is_symlink():  # git lists an untracked nested repository as `sub/`
            raise RuntimeError(f"the tree-read census cannot snapshot the untracked repository {path}: remove it")
        stored = b"link:" + os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()  # never followed
        stored += b"mode:%o" % path.lstat().st_mode  # an executable bit changes what a test can run
        content = hashlib.sha256(stored).digest()  # fixed width, so framed
        digest.update(name + b"\0" + content)
    return digest.hexdigest()


def tracked_directories(root: Path = REPO) -> frozenset[str]:
    """The root and every directory holding a git-tracked file at any depth. A listing of anything else
    (a runtime cache, a build output) is not a read of the tree."""
    listing = git_output("ls-files", "-z", root=root).decode()
    directories = {"."}
    for path in filter(None, listing.split("\0")):
        parents = PurePosixPath(path).parts[:-1]
        directories.update("/".join(parents[: depth + 1]) for depth in range(len(parents)))
    return frozenset(directories)


@functools.cache
def _tracked_directory_identities() -> dict[tuple[int, int], str]:
    """Each tracked directory by its (device, inode). Built before the audit hook is installed."""
    identities: dict[tuple[int, int], str] = {}
    for directory in tracked_directories():
        if (REPO / directory).is_dir():
            stat = os.stat(REPO / directory)
            identities[(stat.st_dev, stat.st_ino)] = directory
    return identities


def _descriptor_directory(descriptor: int) -> str | None:
    """The tracked directory an open descriptor is, matched by device and inode on every platform (no
    `/proc`, no `fcntl`); None for any other directory, which is not a tree read."""
    stat = os.fstat(descriptor)
    return _tracked_directory_identities().get((stat.st_dev, stat.st_ino))


def tree_directory(target: object) -> str | None:
    """`target` as a repository-relative POSIX directory, or None when it is not in the tree."""
    if isinstance(target, int):
        return _descriptor_directory(target)
    raw = os.fsdecode(target) if isinstance(target, str | bytes | os.PathLike) else "."
    resolved = Path(os.path.realpath(raw))  # a link, from outside or inside the checkout, lists its target
    if not resolved.is_relative_to(REPO):
        return None
    return resolved.relative_to(REPO).as_posix()  # the report keeps only tracked directories


def _listed_by_the_import_system() -> bool:
    """Whether the import system itself made the listing: the frame that called the listing function is
    import machinery (the path cache, or an `importlib.metadata` search). Code that merely runs inside an
    import (a test module's top-level scan, an installed package's) made its own listing, a read. A metadata
    search is the import system's only when it walks `sys.path` itself, not a `path=` a caller named."""
    frame = sys._getframe(2)  # above this judge and `_audit`: the caller of `os.listdir` and its kin
    filename = frame.f_code.co_filename.replace("\\", "/")
    if _is_repository_code(frame.f_code.co_filename):  # first: `tests/importlib/metadata/` is the repository's own
        return False
    if any(part in filename for part in IMPORT_MACHINERY):
        return not any(part in filename for part in METADATA_SEARCH) or _walks_sys_path(frame)
    return False


def _walks_sys_path(frame: FrameType | None) -> bool:
    """Whether the `importlib.metadata` search under way walks the `sys.path` list itself, its default, rather
    than a `path=` a caller named (`distributions(path=[REPO])`, even with REPO on `sys.path`): the generator
    in `_search_paths` iterates `map(FastPath, paths)`, whose list iterator still holds that very list."""
    while frame is not None:
        mapped = frame.f_locals.get(".0") if frame.f_code.co_name == "<genexpr>" else None
        if isinstance(mapped, map):
            iterator = mapped.__reduce__()[1][1]
            return type(iterator).__name__ == "list_iterator" and iterator.__reduce__()[1][0] is sys.path
        frame = frame.f_back
    return False


def _is_repository_code(filename: str) -> bool:
    """Whether a frame's file is in the checkout and not in a Python environment there (`.venv`)."""
    environments = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix)}
    real = os.path.realpath(filename) if os.path.isabs(filename) else ""
    return (
        bool(real)
        and tree_directory(os.path.dirname(real)) is not None
        and not any(real.startswith(env + os.sep) for env in environments)
    )


def _runs_one_of(command: list[str], programs: frozenset[str]) -> bool:
    """Whether a command runs one of `programs`: as its program past `NAME=value` words, or anywhere behind
    a command wrapper (`nice rg`, `env -C .. rg`) or a launcher (`uv run rg`), where its grammar decides the
    program. `uv python find` runs uv, not find."""
    words = [word for i, word in enumerate(command) if not all(ASSIGNMENT.match(w) for w in command[: i + 1])]
    if not words:
        return False
    if _program_name(words[0]) in COMMAND_WRAPPERS or _launches_a_program(words):
        return any(_program_name(word) in programs for word in words[1:])
    return _program_name(words[0]) in programs


def _launches_a_program(words: list[str]) -> bool:
    """`uv run X`, `uv tool run X`, `pipx run X` and `uvx X` start X as a wrapper does; `uv python find`
    runs no program."""
    name = _program_name(words[0])
    return name == "uvx" or (name in {"uv", "pipx"} and "run" in words)


def _program_name(word: str) -> str:
    """A program's name however it is spelled: `git`, `/usr/bin/git`, `C:\\Program Files\\Git\\cmd\\git.exe`."""
    return PureWindowsPath(word).name.lower().removesuffix(".exe")


def _command_words(command: str | bytes | Iterable[object] | None) -> list[str]:
    """The argv a Popen audit carries. On POSIX it is a list (CPython wraps a lone string, and prepends
    `/bin/sh -c` for `shell=True`). On Windows it is the one command line Popen built, split back into
    words here (a `cmd /c` line is then a shell, so not measured)."""
    if not isinstance(command, str | bytes):
        return [os.fsdecode(word) for word in command or [] if isinstance(word, str | bytes | os.PathLike)]
    line = os.fsdecode(command)
    return [word.strip('"') for word in shlex.split(line, posix=False)]


def _shell_start(argv: list[str]) -> int | None:
    """Where a shell starts in an argv: as its program past `NAME=value` words, or behind a wrapper or a
    launcher (`nice sh -c`, `uv run bash -c`, `uvx bash`, `pipx run sh`)."""
    program = next((i for i, word in enumerate(argv) if not ASSIGNMENT.match(word)), None)
    if program is None or _program_name(argv[program]) in SHELLS:
        return program
    if _program_name(argv[program]) in COMMAND_WRAPPERS or _launches_a_program(argv[program:]):
        return next((i for i in range(program + 1, len(argv)) if _program_name(argv[i]) in SHELLS), None)
    return None


def _splits_a_string(commands: list[list[str]]) -> bool:
    """Whether a command runs `env -S '<words>'`, whose program hides inside one argument: also after
    `NAME=value`, behind a wrapper (`nice env -S`) or past env's other options (`env -u X -S`)."""
    return any(
        _env_splits_a_string(command[i + 1 :])
        for command in commands
        for i, word in enumerate(command)
        if _program_name(word) == "env"
    )


def _env_splits_a_string(options: list[str]) -> bool:
    rest = iter(options)
    for option in rest:
        if _abbreviates("--split-string", option) or _short_cluster_holds(option, "S", ENV_VALUE_LETTERS):
            return True  # also clustered (`env -vS 'find apps'`) or abbreviated (`env --s 'find apps'`)
        if _env_option_takes_the_next_word(option):
            next(rest, None)
        elif not (option.startswith("-") or ASSIGNMENT.match(option)):
            return False  # env's program: its own options are not env's
    return False


def _env_finds_its_program_elsewhere(words: list[str]) -> bool:
    """Among an env's own options, one that finds its program somewhere else: `-P DIR`, `--path=DIR`, `-C DIR`,
    also clustered (`env -iP /tools scan`, `env -vC tools ./scan.sh`) or abbreviated (`env --ch=tools`)."""
    rest = iter(words)
    for word in rest:
        if _program_name(word) != "env":
            continue
        for option in rest:
            relocates = option.startswith("--path") or _abbreviates("--chdir", option)
            if (
                relocates
                or _short_cluster_holds(option, "P", ENV_VALUE_LETTERS)
                or _short_cluster_holds(option, "C", ENV_VALUE_LETTERS)
            ):
                return True
            if _env_option_takes_the_next_word(option):
                next(rest, None)
            elif not (option.startswith("-") or ASSIGNMENT.match(option)):
                break  # env's program: its own options are not env's
    return False


def _short_cluster_holds(option: str, letter: str, value_letters: str) -> bool:
    """Whether a short-option cluster holds `-<letter>` before an option that takes the rest as its value
    (env's `-vS` splits; `-uFOO_S` unsets `FOO_S`). A `NAME=value` word is not an option."""
    if not option.startswith("-") or option.startswith("--"):
        return False
    for held in option[1:]:
        if held == letter:
            return True
        if held in value_letters:
            return False
    return False


def _env_option_takes_the_next_word(option: str) -> bool:
    """`env -u NAME`, `env -C DIR`, or a long form getopt also takes by a unique prefix (`--un NAME`, `--ch DIR`)."""
    long_forms = ("--unset", "--chdir")
    return option in {"-u", "-C"} or ("=" not in option and any(_abbreviates(form, option) for form in long_forms))


def _program_words(command: list[str]) -> list[str]:
    """A simple command from its program on: past leading `NAME=value` words and an `env` wrapper with its
    `NAME=value`, `-i` and `-u NAME`. Any other `env` option can move or rewrite the command, so with an
    `ls-files` in it that fails loud: the test errors, and its module reads as PARTIAL."""
    words = list(command)
    while words and ASSIGNMENT.match(words[0]):
        words.pop(0)
    if not words or _program_name(words[0]) != "env":
        return words
    words.pop(0)
    while words and (words[0].startswith("-") or ASSIGNMENT.match(words[0])):
        option = words.pop(0)
        if option in ENV_UNSET:
            words = words[1:]
        elif option in ENV_FLAGS or option.startswith("--unset=") or ASSIGNMENT.match(option):
            continue
        elif any("ls-files" in word for word in command):
            raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: env option {option}")
    return words


def _refuse_a_reader_it_does_not_place(command: list[str]) -> None:
    """`grep -r`, `rgrep` and `git grep` walk the tree, but the census does not model where:
    rather than record nothing, it fails loud, so the module reads PARTIAL."""
    words = _program_words(command)
    if not words:
        return
    name = _program_name(words[0])
    if name in COMMAND_WRAPPERS or _launches_a_program(words):  # `nice grep -r`, `timeout 5 git grep`
        wrapped = next((i for i, w in enumerate(words) if i and _program_name(w) in WALKERS), None)
        if wrapped is not None:
            _refuse_a_reader_it_does_not_place(words[wrapped:])
        return
    recursive = {"-r", "-R", "--recursive", "--dereference-recursive", "--directories=recurse"}
    grep_walks = name == "rgrep" or (
        name in WALKERS - {"rgrep", "git"}
        and (
            any(w in recursive or (w[:1] == "-" and w[1:2] != "-" and set(w[1:]) & {"r", "R"}) for w in words[1:])
            or any(pair in {("-d", "recurse"), ("--directories", "recurse")} for pair in itertools.pairwise(words[1:]))
            or any(_abbreviates_a_recursive_grep_option(w, n) for w, n in itertools.pairwise([*words[1:], ""]))
        )
    )
    git_options = _git_global_options(words) if name == "git" else []
    subcommand = words[len(git_options) + 1 :][:1]
    git_walks = name == "git" and subcommand == ["grep"]
    if name == "git" and subcommand and subcommand[0] not in _git_commands():  # an alias, or an unmodelled option
        raise RuntimeError(
            f"the tree-read census cannot place `{shlex.join(command)}`: {subcommand[0]} is no git command"
        )
    if grep_walks or git_walks:
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: a walk it does not model")


def _abbreviates(option: str, word: str) -> bool:
    """Whether `word` is `option` or a prefix git or getopt accepts for it (`--pathspec-from-f=x`)."""
    name = word.partition("=")[0]
    return len(name) > 2 and option.startswith(name)


def _abbreviates_a_recursive_grep_option(word: str, following: str) -> bool:
    """GNU getopt takes any unambiguous prefix of a long option: `--recurs`, `--direc=recurse`, `--dir rec`."""
    name, _, value = word.removeprefix("--").partition("=")
    if not word.startswith("--") or not name:
        return False
    if "recursive".startswith(name) or "dereference-recursive".startswith(name):
        return True
    operand = value or following
    return "directories".startswith(name) and bool(operand) and "recurse".startswith(operand)


@functools.cache
def _git_commands() -> frozenset[str]:
    """Git's own commands. An alias never hides one, so any other subcommand may be an alias, from argv, the
    environment or a config file, or an option the census does not model swallowed the real one."""
    return frozenset(git_output("--list-cmds=builtins,main,others").decode().split())


def _ls_files_subdirectory(command: list[str]) -> str | None:
    """For `git [-C <dir>]... ls-files ...` or `... ls-tree ...`, where its `-C` options lead, folded in order as
    git does ("" without one); None for any other command."""
    words = _program_words(command)
    if not GIT_LISTERS & set(words):
        return None
    if not any(_program_name(word) == "git" for word in words):
        return None
    if _program_name(words[0]) != "git":  # `nice git ...`, `timeout 5 git ...`, `xargs git ...`
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: git behind a wrapper")
    options = _git_global_options(words)
    if (unmodelled := _unmodelled_git_option(options)) is not None:
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: {unmodelled}")
    if words[len(options) + 1 : len(options) + 2] not in ([lister] for lister in GIT_LISTERS):  # `git log -- ls-files`
        return None
    if any(_abbreviates("--pathspec-from-file", word) for word in words):  # may hold `:(top)` or `..`
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: pathspecs from a file")
    if _ls_files_may_run_a_program(words[len(options) + 2 :]):
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: it may run a program unseen")
    prefixes = [w.partition("=")[0] for w in command if ASSIGNMENT.match(w)]  # `env GIT_CONFIG_COUNT=1 git ...`
    if any(o.startswith(GIT_REPOSITORY_MOVES) for o in options) or any(map(_names_a_git_location, prefixes)):
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(command)}`: it moves the repository")
    return os.path.join("", *_git_directories(options))


def _ls_files_may_run_a_program(arguments: list[str]) -> bool:
    """Whether `git ls-files` options may run a program the census cannot read: `-m` re-hashes a racily clean
    file through the configured clean filter, and `--recurse-submodules` runs git under each submodule's config."""
    options = arguments[: arguments.index("--")] if "--" in arguments else arguments
    return any(
        (word.startswith("-") and not word.startswith("--") and "m" in word)  # also `-cm`, and `-x` values: refused
        or any(_abbreviates(option, word) for option in LS_FILES_RUNNERS)
        for word in options
    )


def _unmodelled_git_option(options: list[str]) -> str | None:
    """The first of git's global options the census does not model: it may hide the subcommand (`--super-prefix
    sub`, `--attr-source HEAD`), move the tree it lists (`-c core.worktree=...`) or run a pager (`-p`,
    `-c pager.ls-files=...`, `--config-env=pager.ls-files=VAR`)."""
    rest = iter(options)
    for option in rest:
        if option in GIT_OPTIONS_WITH_VALUES:
            value = next(rest, "")
        elif option[:2] in {"-c", "-C"} or option.startswith(tuple(f"{o}=" for o in GIT_OPTIONS_WITH_VALUES)):
            value = option
        elif option in GIT_FLAGS:
            continue
        else:
            return f"git option {option}"
        if option.startswith(("-c", "--config-env")) and any(k in value.lower() for k in GIT_CONFIG_REFUSED):
            return f"git option {option} moves it or runs a program"
    return None


def _git_directories(options: list[str]) -> list[str]:
    """Each `-C <dir>` and attached `-C<dir>` among git's global options, in order: git folds them all."""
    directories: list[str] = []
    rest = iter(options)
    for option in rest:
        if option in GIT_OPTIONS_WITH_VALUES:
            value = next(rest, "")
            if option == "-C":
                directories.append(value)
        elif option.startswith("-C"):
            directories.append(option[2:])
    return directories


def _git_global_options(words: list[str]) -> list[str]:
    """The options between `git` and its subcommand, with the values of `-C`, `-c` and their kin."""
    options: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        if not word.startswith("-"):
            break
        options.append(word)
        if word in GIT_OPTIONS_WITH_VALUES:
            options.append(next(rest, ""))
    return options


def _lister_at(argv: list[str]) -> int:
    """Where git's lister subcommand is in `argv`: the word after git's own options, where `_ls_files_subdirectory`
    found it, never a later operand that only spells one (`git ls-tree HEAD -- ls-files`)."""
    words = _program_words(argv)
    return len(argv) - len(words) + len(_git_global_options(words)) + 1


def _pathspec_leaves_its_directory(arguments: list[str]) -> bool:
    """Whether a `git ls-files` pathspec reaches outside where git runs: magic (`:(top)apps`, `:/`), absolute
    or climbing `..`. Git then lists more than that directory, so the census credits the root."""
    pathspecs = [word for word in arguments if not word.startswith("-")]
    return any(
        spec.startswith(":") or os.path.isabs(spec) or Path(os.path.normpath(spec)).parts[:1] == (os.pardir,)
        for spec in pathspecs
    )


def _listed_trees(argv: list[str], directory: str, env: Iterable[str | bytes]) -> list[str]:
    """What a `git ls-files` run in `directory` lists: from the root when a pathspec leaves it; each pathspec's
    own tree when every pathspec is a plain path and every option a known flag (`-- apps` lists `apps/**`, a
    file or a missing path its nearest tracked ancestor's); otherwise all of `directory`, since a glob, magic, an option's value
    or a pathspec setting in the environment may match anywhere below it."""
    at = _lister_at(argv)
    if argv[at] == "ls-tree":
        return _ls_tree_trees(argv, at, directory, env)
    if argv[at] != "ls-files":
        raise AssertionError(f"not a git lister: {shlex.join(argv)}")
    words = argv[at + 1 :]
    if _pathspec_leaves_its_directory(words):
        return ["./**"]
    split = words.index("--") if "--" in words else len(words)
    specs = [word for word in words[:split] if not word.startswith("-")] + words[split + 1 :]
    flags = {word for word in words[:split] if word.startswith("-")}
    if (
        not specs
        or not flags <= LS_FILES_FLAGS
        or any(set(spec) & PATHSPEC_SPECIALS or spec.startswith(":") for spec in specs)
        or _a_pathspec_setting_applies(argv, at, env)
    ):
        return [f"{directory}/**"]
    tracked = set(_tracked_directory_identities().values())
    paths = {os.path.normpath(os.path.join(directory, spec)).replace(os.sep, "/") for spec in specs}
    return sorted({f"{_nearest_tracked(path, tracked)}/**" for path in paths})


def _a_pathspec_setting_applies(argv: list[str], at: int, env: Iterable[str | bytes]) -> bool:
    """Whether `git --icase-pathspecs` (or a glob, noglob or literal setting) or its `GIT_*_PATHSPECS` name, in the
    environment or an argv assignment, may make a plain path match more than its own tree."""
    return (
        any(word in PATHSPEC_FLAGS for word in argv[:at])
        or any(os.fsdecode(name).upper() in PATHSPEC_ENV for name in env)
        or any(word.partition("=")[0].upper() in PATHSPEC_ENV for word in argv if ASSIGNMENT.match(word))
    )


def _ls_tree_trees(argv: list[str], at: int, directory: str, env: Iterable[str | bytes]) -> list[str]:
    """What a `git ls-tree [options] <tree-ish> [paths]` run in `directory` lists, at whatever commit it names:
    one level of `directory` without paths (all of it with `-r`), each plain path's tree as a `git ls-files`
    pathspec is placed, and the root when the tree-ish names a path (`HEAD:docs`) or is not a commit (a tree
    object lists from wherever it lies), `--full-tree` is given or a path leaves `directory`. A glob or a
    pathspec setting (`--icase-pathspecs`, `GIT_ICASE_PATHSPECS`) lists all of `directory`, as it does for `git ls-files`.
    An option the census does not model fails loud."""
    words = argv[at + 1 :]
    split = words.index("--") if "--" in words else len(words)
    options = [word for word in words[:split] if word.startswith("-")]
    operands = [word for word in words[:split] if not word.startswith("-")] + words[split + 1 :]
    if not operands or not all(map(_is_a_modelled_ls_tree_option, options)):
        raise RuntimeError(
            f"the tree-read census cannot place `{shlex.join(argv)}`: an ls-tree option it does not model"
        )
    tree_ish, paths = operands[0], operands[1:]
    if (
        ":" in tree_ish
        or "--full-tree" in options
        or any(map(_leaves_where_it_runs, paths))
        or not _names_a_commit(tree_ish)
    ):
        return ["./**"]
    if not paths:
        recursive = any(not option.startswith("--") and "r" in option for option in options)
        return [f"{directory}/**" if recursive else directory]
    if any(set(path) & PATHSPEC_SPECIALS for path in paths) or _a_pathspec_setting_applies(argv, at, env):
        return [f"{directory}/**"]
    tracked = set(_tracked_directory_identities().values())
    joined = {os.path.normpath(os.path.join(directory, path)).replace(os.sep, "/") for path in paths}
    return sorted({f"{_nearest_tracked(path, tracked)}/**" for path in joined})


def _names_a_commit(tree_ish: str) -> bool:
    """Whether `tree_ish` peels to a commit in the checkout, so `ls-tree` lists from the root tree; a tree object
    (`HEAD^{tree}`, a subtree's id) or a name git cannot resolve is not one."""
    try:
        _git_with(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{tree_ish}^{{commit}}"], REPO, os.environ)
    except subprocess.CalledProcessError:
        return False
    return True


def _is_a_modelled_ls_tree_option(word: str) -> bool:
    """A `git ls-tree` option that runs no program and takes no separate value: a cluster of its flags, or a long
    option (`--format` and `--abbrev` only with an attached `=` value)."""
    if word.startswith("--"):
        return word in LS_TREE_LONG_OPTIONS or word.startswith(("--format=", "--abbrev="))
    return len(word) > 1 and set(word[1:]) <= set(LS_TREE_FLAG_LETTERS)


def _leaves_where_it_runs(path: str) -> bool:
    return path.startswith(":") or os.path.isabs(path) or Path(os.path.normpath(path)).parts[:1] == (os.pardir,)


def _nearest_tracked(path: str, tracked: set[str]) -> str:
    """The path if it is a tracked directory, else its nearest tracked ancestor (`.` at the root): a pathspec
    naming untracked levels (`apps/new/deep/file.py`) matches what a later change adds below that ancestor."""
    while path not in tracked and path not in {".", ""}:
        path = posixpath.dirname(path) or "."
    return path


def _as_popen(event: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
    """Any other way of starting a program, as the (executable, argv, cwd, env) a Popen audit carries. Each
    runs where the test process runs; os.system runs its command through the shell. `os.startfile` and
    `_winapi.CreateProcess` (whose audit carries no environment) are None: a child the census cannot read."""
    if event == "os.system" and os.name == "nt":  # cmd.exe runs it: one command line, as a Windows Popen audits
        return (None, f"cmd.exe /c {os.fsdecode(args[0])}", None, None)
    if event == "os.system":
        return ("/bin/sh", ["/bin/sh", "-c", os.fsdecode(args[0])], None, None)
    if event in {"os.posix_spawn", "os.exec"}:
        return (args[0], list(args[1]), None, args[2])
    if event == "os.spawn":
        return (args[1], list(args[2]), None, args[3])
    return None


def _names_a_git_location(name: str) -> bool:
    """`GIT_DIR`, `GIT_WORK_TREE`, or config from the environment (`GIT_CONFIG_COUNT`/`_KEY_n`, `GIT_CONFIG_GLOBAL`),
    which can set `core.worktree`. Windows names ignore case."""
    return name.upper() in GIT_LOCATION_ENV or name.upper().startswith("GIT_CONFIG")


def _argv_as_run(executable: object, argv: list[str]) -> list[str]:
    """The argv with the file actually run in place of argv[0]: `Popen(["scan.sh"], executable="/x/scan.sh")`
    and `os.execv("/x/scan.sh", ["scan.sh"])` run /x/scan.sh, whatever a lookup of argv[0] would find."""
    return [os.fsdecode(executable), *argv[1:]] if executable is not None and argv else argv  # type: ignore[arg-type]


def _refuse_a_renamed_program(executable: object, argv: list[str]) -> None:
    """`Popen(executable=...)` and the spawn APIs run their executable whatever argv[0] names. When the two
    differ and either is a program the census places, or only one is a Python (`["alias", "-S", ...]` run by
    `sys.executable` skips the hook unseen), it cannot tell what really runs, so it fails loud."""
    if executable is None or not argv:
        return
    real, named = _program_name(os.fsdecode(executable)), _program_name(argv[0])  # type: ignore[arg-type]
    if real != named and (PLACED_PROGRAMS & {real, named} or _names_a_python([real]) != _names_a_python([named])):
        raise RuntimeError(
            f"the tree-read census cannot place `{shlex.join(argv)}`: it runs {real}, argv names {named}"
        )


def directories_listed(event: str, args: tuple[Any, ...]) -> list[str]:
    """The tree directories an audit event lists: a scandir or listdir target, or where each `git ls-files`
    runs (the Popen `cwd`, then any `git -C <dir>`). A shell, a tree scanner or a hidden program lists nothing
    here: its module reads as not measured instead. A pathspec reaching outside where git runs makes it the root."""
    if event in LISTING_EVENTS:
        directory = tree_directory(args[0] if args else ".")
        return [] if directory is None else [directory]
    if (spawned := _as_popen(event, args)) is not None:
        event, args = "subprocess.Popen", spawned
    if event != "subprocess.Popen":
        return []
    start = os.fsdecode(args[2]) if args[2] is not None else "."
    env = os.environ if args[3] is None else args[3]
    argv = _command_words(args[1])
    _refuse_a_renamed_program(args[0], argv)
    argv = _argv_as_run(args[0], argv)
    if _runs_what_the_census_does_not_model(argv, args[2], args[3]):  # marked unmeasured, never parsed
        return []
    _refuse_a_reader_it_does_not_place(argv)
    subdirectory = _ls_files_subdirectory(argv)
    if subdirectory is not None and any(_names_a_git_location(os.fsdecode(name)) for name in env):
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(argv)}`: git's location is in its env")
    directory = None if subdirectory is None else tree_directory(os.path.join(start, subdirectory))
    where = os.path.join(start, subdirectory or "")
    if subdirectory is not None and os.path.isdir(where):  # also another repository's: its hook may list this one
        _refuse_a_git_hook_in_its_config(argv, where, env)
    return [] if directory is None else _listed_trees(argv, directory, env)


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event not in LISTING_EVENTS and event not in SPAWN_EVENTS:
        return
    if event in {"os.posix_spawn", "_winapi.CreateProcess"} and _spawned_inside_popen():  # Popen's event counted it
        return
    pooled = _a_pool_thread_runs_this()
    if pooled and event in SPAWN_EVENTS:  # its child inherits the running module's name, so nothing it does is placed
        _record_reads(SESSION_READS, ["."])  # unplaced: the report says UNKNOWN
        return
    module = SESSION_READS if pooled else _State.module or (SESSION_READS if _repository_code_on_the_stack() else None)
    if module is None:
        return
    try:  # inline, not a helper: `_listed_by_the_import_system` counts the frames above this one
        if event in SPAWN_EVENTS:
            _record_a_spawn(event, args)
        directories = directories_listed(event, args)
        if not directories or _listed_by_the_import_system():
            return
        _record_reads(module, directories)
    except Exception:  # a refusal or a parse error (shlex's ValueError) a test may swallow still leaves it PARTIAL
        if module == SESSION_READS:  # repository code's or a pool thread's: no module to mark, so an unplaced read
            _record_reads(SESSION_READS, ["."])
        elif module != SESSION_READS:
            _record_an_unmeasured_child(module)
        raise


def _record_reads(module: str, directories: list[str]) -> None:
    """Credit listings, or an unplaced `.` mark, to a module. A child, spawned or forked, writes them now:
    its in-memory reads are lost when it exits, by `os._exit` or a signal too."""
    if _State.child_reads is not None:
        with _State.child_reads.open("a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps({"module": module, "dirs": directories}) + "\n")
    elif _State.child_reads is None:
        _State.reads.setdefault(module, set()).update(directories)


def _a_pool_thread_runs_this() -> bool:
    """A thread older than the running module's phase, any but the main thread while no module runs, or any but
    the main thread born while an older thread lives (`_enter_a_module_phase`): it may run
    another module's work, or work queued for after the last test, so what it does is never credited to the
    running module but held as unplaced `<session>` reads."""
    thread = threading.current_thread()
    return thread in _State.older_threads or (
        thread is not threading.main_thread() and (_State.module is None or _State.lineage_unknown)
    )


def _spawned_inside_popen() -> bool:
    """Whether this `os.posix_spawn` or `_winapi.CreateProcess` is Popen's own start (`_execute_child`), which
    fires both events for one child: the POSIX fast path (with `close_fds=False` before 3.13, by default from
    3.13), and every Windows Popen."""
    frame: FrameType | None = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == "_execute_child" and os.path.basename(frame.f_code.co_filename) == "subprocess.py":
            return True
        frame = frame.f_back
    return False


def _repository_code_on_the_stack() -> bool:
    """Whether repository code (a conftest's or a plugin's top level, or a hook or fixture it defines) made this
    call, outside any test module: pytest's own collection walks are not reads, but such a scan feeds every test."""
    frame: FrameType | None = sys._getframe(1)
    repo, census = REPO.as_posix() + "/", _real_posix_path(__file__)
    while frame is not None:
        path = _real_posix_path(frame.f_code.co_filename)  # pytest imports a conftest by the path it was given
        if path.startswith(repo) and path != census and "/.venv/" not in path and "/site-packages/" not in path:
            return True
        frame = frame.f_back
    return False


@functools.cache
def _real_posix_path(filename: str) -> str:
    """A frame's file with every symlink resolved, so a checkout reached through one still matches `REPO`; a
    name that is no path (`<frozen importlib._bootstrap>`, `<string>`) stays itself, never one under the cwd."""
    return (os.path.realpath(filename) if os.path.isabs(filename) else filename).replace("\\", "/")


def _record_a_spawn(event: str, args: tuple[Any, ...]) -> None:
    """Count a spawn's hooked Python launches, or mark what it starts that the census cannot see: unmeasured
    for a test module, an unplaced `<session>` read for repository code."""
    if getattr(_State.own_spawn, "active", False):
        return
    spawned = args if event == "subprocess.Popen" else _as_popen(event, args)
    if spawned is None:  # `os.startfile`: whatever application the file opens with
        _mark_an_unread_child()
        return
    if event == "subprocess.Popen" and (_popen_arguments() or {}).get("preexec_fn") is not None:
        _mark_an_unread_child()  # it runs in the child before exec, and may move it or list anything
        return
    argv = _argv_as_run(spawned[0], _command_words(spawned[1]))
    if _runs_what_the_census_does_not_model(argv, spawned[2], spawned[3]):
        _mark_an_unread_child()
        return
    commands = [argv]
    dropped = _python_without_the_hook(argv)
    if (
        dropped
        or _git_may_run_unseen(argv, event, spawned[2], spawned[3])
        or not (_starts_a_hooked_python(argv, spawned[2], spawned[3]) or _argv_shows_what_it_lists(argv))
    ):  # any other program may list the tree unseen; repository code's child has no module to hold PARTIAL
        _record_an_unmeasured_child(_State.module or SESSION_READS)  # so `<session>`, which the report unplaces
    elif _hook_reaches(spawned[3]):
        _record_python_launches(commands, spawned[2], spawned[3])


def _git_may_run_unseen(argv: list[str], event: str, cwd: SpawnCwd, env: object) -> bool:
    """Whether a git command may run a program the census cannot read: a stand-in named `git` (on the child's
    PATH, or `executable=`) rather than the git the census runs, or a pager. Git pages only on a terminal,
    where `pager.<cmd>` from any config, or `git config --list`'s own default, starts one; `-P` never does."""
    words = _program_words(argv)
    if not words or _program_name(words[0]) != "git":
        return False
    if not _runs_the_census_git(argv, cwd, env):
        return True
    return not NO_PAGER & set(_git_global_options(words)) and _stdout_may_be_a_terminal(event)


def _runs_the_census_git(argv: list[str], cwd: SpawnCwd, env: object) -> bool:
    """Whether the file a git command runs, found from the child's cwd or on its PATH, is the git the census runs
    itself. Behind `env -i` or `-u`, env's own lookup decides: it may not be (`env PATH=` is unmeasured already)."""
    words = _program_words(argv)
    if any(word.startswith("-") for word in argv[: len(argv) - len(words)]):
        return False
    found = _program_file(words[0], cwd, env)
    return found is not None and os.path.samefile(found, census_git())


def _stdout_may_be_a_terminal(event: str) -> bool:
    """Whether a spawned program's stdout may be a terminal: a Popen's own `stdout` (PIPE, DEVNULL or a file is
    not one), else the test process's fd 1, which `os.system`, `os.exec*` and `os.spawn*` children inherit. A bare
    `os.posix_spawn` may `dup2` anything onto it, so it may be one."""
    if event == "os.posix_spawn":
        return True
    if event != "subprocess.Popen":
        return os.isatty(1)
    if (arguments := _popen_arguments()) is None:
        return True  # no Popen frame: it may be one
    stdout = arguments["stdout"]
    if stdout in (subprocess.PIPE, subprocess.DEVNULL):
        return False
    if stdout is None:
        return os.isatty(1)
    return os.isatty(stdout if isinstance(stdout, int) else stdout.fileno())


def _popen_arguments() -> Mapping[str, Any] | None:
    """The arguments of the `Popen` whose audit event this is, which the event's tuple leaves out."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_name == "__init__" and isinstance(frame.f_locals.get("self"), subprocess.Popen):
            return frame.f_locals
        frame = frame.f_back
    return None


def _starts_a_hooked_python(argv: list[str], cwd: SpawnCwd, env: object) -> bool:
    """A Python the hook reaches, started directly or behind a wrapper. Behind `uv` or `uvx` it is not: the
    native launcher reads the project (workspace member globs) unhooked before its Python starts."""
    programs = [_program_name(word) for word in _wrapped_program(_program_words(argv))]
    return _hook_reaches(env) and not NATIVE_LAUNCHERS & set(programs) and _starts_a_python(argv, cwd, env)


def _argv_shows_what_it_lists(argv: list[str]) -> bool:
    """A child the census needs no hook in: an argv `git ls-files`, placed where it runs, or a git command that
    lists no directory of the checkout (`git rev-parse`). Every other program a test starts, other than a Python
    the hook reaches, may walk the tree unseen (its own `--help` too), so its module reads PARTIAL."""
    words = _program_words(argv)
    if not words or _ls_files_subdirectory(argv) is not None:
        return bool(words)
    if _program_name(words[0]) != "git":
        return False
    options = _git_global_options(words)
    command = words[len(options) + 1 :]
    if _unmodelled_git_option(options) is not None or "--help" in command:  # a pager, or `git help`'s viewer
        return False
    if command[:1] == ["config"]:  # never `config edit` or `-e`/`--edit` (also abbreviated): they run an editor
        return not any(
            word == "edit"
            or (word.startswith("-") and not word.startswith("--") and "e" in word)
            or (len(word) > 2 and "--edit".startswith(word.split("=")[0]))
            for word in command[1:]
        )
    if command[:1] == ["cat-file"]:  # only `git cat-file -e OBJECT` and its `-t`, `-s` siblings
        return len(command) == 3 and command[1] in CAT_FILE_ANSWERS and not command[2].startswith("-")
    if command[:1] == ["rev-list"] and not all(map(_is_a_modelled_rev_list_option, _options_of(command[1:]))):
        return False  # an unknown option, or one git expands from an abbreviation, may run a configured helper
    return (
        bool(command)
        and command[0] in GIT_LISTS_NOTHING
        and not any(w.startswith("--objects") or "%G" in w or "signature" in w for w in command)
        and not any(map(_names_a_configured_format, command))
    )  # `--format=%G?` and `%(signature)` verify a signature through the configured `gpg.program`


def _options_of(words: list[str]) -> list[str]:
    """The option words before a `--`; after it every word is a path."""
    return [word for word in words[: words.index("--") if "--" in words else len(words)] if word.startswith("-")]


def _is_a_modelled_rev_list_option(word: str) -> bool:
    """`--count`, `--max-parents=0`, `-n` or `-n40`: an exact option of `REV_LIST_OPTIONS`, never an abbreviation."""
    return word.partition("=")[0] in REV_LIST_OPTIONS or re.fullmatch(r"-n\d+", word) is not None


def _names_a_configured_format(word: str) -> bool:
    """`--pretty=<name>` or `--format=<name>` naming no format of git's own: git expands `pretty.<name>` from
    config, which may hold `%G?`. A format with a `%` in it is written out, so the census reads it."""
    option, _, value = word.partition("=")
    return option in {"--pretty", "--format"} and "%" not in value and value not in PRETTY_FORMATS | {""}


def _runs_what_the_census_does_not_model(argv: list[str], cwd: SpawnCwd, env: object) -> bool:
    """Whether a spawn runs a shell (a `-c` script, a script file or stdin, also behind a wrapper), a program
    whose `#!` line names a shell, or a tree scanner (`find`, `rg`, `fd`, `tree`, `du`, `ls`, also behind a
    wrapper or launcher). What these read depends on grammars, config files and helper commands the census
    does not model (`rg --pre`, `RIPGREP_CONFIG_PATH`, `--hostname-bin`), so their module reads as not
    measured and nothing is parsed. A lone `--version` or `--help` is not opaque, yet its module still reads
    PARTIAL unless `_argv_shows_what_it_lists`."""
    position = next((i for i, word in enumerate(argv) if not ASSIGNMENT.match(word)), None)
    if position is None:
        return False
    words = argv[position:]
    wrapped = _program_name(words[0]) in COMMAND_WRAPPERS or _launches_a_program(words)
    relocated = (
        wrapped
        and (
            any(  # `env -C tools ./scan.sh`, `env PATH=/tools scan`: found elsewhere than here
                w.startswith(("-C", "--chdir", "--directory", "-D", "PATH="))
                for w in itertools.takewhile(  # also attached: `env -Ctools`; stops at git, whose own `-C` it places
                    lambda w: _program_name(w) != "git", words[1:]
                )
            )
            or _env_finds_its_program_elsewhere(words)
        )
    )
    if (
        relocated
        or _splits_a_string([argv])
        or any(  # `flock LOCK -c 'find apps'` runs its string through the shell
            _program_name(word) == "flock"
            and any(w in {"-c", "--command"} or w.startswith("--command=") for w in words[i + 1 :])
            for i, word in enumerate(words)
        )
        or any(  # `env -S 'python -I ...'`: its program hides inside one argument
            _is_a_shell_script_file(word, cwd, env) or PureWindowsPath(word).suffix.lower() in BATCH_SUFFIXES
            for word in (words if wrapped else words[:1])
        )
    ):  # a script, also behind a wrapper (`nice ./scan.sh`), whose own `--help` may run anything
        return True
    if words[1:] and set(words[1:]) <= NOT_TRAVERSING:
        return False
    return _shell_start(argv) is not None or _runs_one_of(argv, TREE_SCANNERS)


def _is_a_shell_script_file(program: str, cwd: SpawnCwd, env: object) -> bool:
    """Whether a program is a file whose `#!` line names a shell (`#!/bin/sh`, `#!/usr/bin/env bash`) or passes
    `env` an option, whose operands the census does not parse (`#!/usr/bin/env -u NAME sh`)."""
    found = _program_file(program, cwd, env)
    if found is None or not os.path.isfile(found):
        return False
    with open(found, "rb") as handle:
        line = handle.readline(256)
    words = line[2:].decode(errors="replace").split() if line.startswith(b"#!") else []
    if words and _program_name(words[0]) == "env":  # past `env`'s NAME=value words
        words = [word for word in words[1:] if not ASSIGNMENT.match(word)]
        if words and words[0].startswith("-"):
            return True
    return bool(words) and _program_name(words[0]) in SHELLS


def _hook_reaches(env: object) -> bool:
    """Whether a child started with this `env=` (None inherits `os.environ`) loads the census hook for the
    running test module. A test's environment is its own input, so the census reads it and never edits it."""
    effective = os.environ if env is None else env
    if not isinstance(effective, Mapping):
        raise TypeError(f"the tree-read census cannot read a {type(effective).__name__} environment")
    if _State.carrier is None:  # a conftest's top level, before the hook is installed: no child can load it
        return False
    hook_dir, reads_dir = _State.carrier
    return (
        effective.get(CHILD_MODULE_ENV) == (_State.module or SESSION_READS)  # repository code's children: `<session>`
        and effective.get(CHILD_READS_ENV) == reads_dir
        and hook_dir in str(effective.get("PYTHONPATH", "")).split(os.pathsep)
    )


def _names_a_python(words: list[str]) -> bool:
    """Whether any word names a Python or a program that starts one (`pytest`, `uv`, `pip`)."""
    return any(_program_name(word).startswith("python") or _program_name(word) in PYTHON_LAUNCHERS for word in words)


def _program_file(program: str, cwd: SpawnCwd, env: object) -> str | None:
    return _program_file_on(os.name, program, cwd, env)


def _program_file_on(platform: str, program: str, cwd: SpawnCwd, env: object) -> str | None:
    """The file a child runs for `program`: a path resolves against the child's own cwd, a bare name on
    the child's PATH. On Windows, CreateProcess finds a relative or bare program its own way (the parent's
    directories and PATH, `.exe` appended), so only an absolute path is known: None, so the child is unmeasured."""
    if platform == "nt" and not os.path.isabs(program):
        return None
    if os.sep in program:
        found = os.path.join(os.fsdecode(cwd or "."), program)
        return found if os.path.isfile(found) else None
    effective = os.environ if env is None else env
    if not isinstance(effective, Mapping):
        return shutil.which(program)
    here = os.fsdecode(cwd or ".")  # a relative PATH entry resolves after the child's chdir
    search = os.pathsep.join(os.path.join(here, entry) for entry in os.get_exec_path(effective))
    return shutil.which(program, path=search)  # without PATH, subprocess searches os.defpath, not ours


def _runs_a_python_script(command: list[str], cwd: SpawnCwd, env: object) -> bool:
    """Whether the program, found from the child's cwd or on its PATH, is a script whose `#!` line names a
    Python (a console entry point, an executable `.py`), also behind a wrapper (`nice ./tool.py`). A compiled
    program that is not Python reads as False."""
    words = _program_words(command)
    if words and _program_name(words[0]) in {"uv", "uvx"}:
        return _launches_a_program(words) and _uv_runs_a_python(words, cwd, env)  # past `--with pytest` operands
    if words and _launches_a_program(words):  # `pipx run ./tool.py`: the launcher's grammar names it
        candidates = words
    elif not (words and _launches_a_program(words)):
        candidates = _wrapped_program(words)
    return any(_is_a_python_script_file(word, cwd, env) for word in candidates)


def _is_a_python_script_file(program: str, cwd: SpawnCwd, env: object) -> bool:
    found = _program_file(program, cwd, env)
    if found is None or not os.path.isfile(found):
        return False
    with open(found, "rb") as handle:
        first_line = handle.readline(256)
    return first_line.startswith(b"#!") and b"python" in first_line


def _drops_the_hook(word: str) -> bool:
    """An env word before a Python that empties or edits its environment: `-i` or `-u NAME`, also in a short
    cluster (`-vi`, `-uNAME`), `--ignore-environment` or `--unset` and any prefix env accepts (`--ignore-env`,
    `--un`), or `PYTHONPATH=`."""
    return (
        word in HOOK_DROPPING_ENV
        or word.startswith(HOOK_DROPPING_PREFIXES)
        or any(_short_cluster_holds(word, letter, ENV_VALUE_LETTERS) for letter in "iu")
        or any(_abbreviates(option, word) for option in ("--ignore-environment", "--unset"))
    )


def _remember_the_carrier() -> None:
    """Kept from the start, so a test that edits or clears `os.environ` cannot move what the hook checks."""
    reads_dir = os.environ[CHILD_READS_ENV]
    _State.carrier = (str(Path(reads_dir).parent / CHILD_HOOK_DIR), reads_dir)


def _record_a_fork_launch() -> None:
    """In the parent, before the child exists: a fork is a launch, as a spawned Python is, so a child
    scheduled only after the report loads leaves its module PARTIAL. A fork while no module runs (a conftest
    as it is imported, a session hook) is unplaced: its child, once out of repository code, has no module. So is
    one from a pool thread or a thread of unknown lineage, whose work may be another module's."""
    _State.fork.module = _State.module  # read once: another thread may move the phase before the fork happens
    _State.fork.unplaced = _State.fork.module is None or _a_pool_thread_runs_this()  # per thread: two may fork
    if _State.fork.unplaced:
        _record_reads(SESSION_READS, ["."])
    elif not _State.fork.unplaced:
        _write_launches(1, _State.fork.module)


def _mark_an_unread_child() -> None:
    """A child the census cannot read: its test module reads PARTIAL; repository code's is an unplaced read."""
    if _State.module is None:
        _record_reads(SESSION_READS, ["."])  # unplaced: the report says UNKNOWN
    elif _State.module is not None:
        _record_an_unmeasured_child(_State.module)


def _refuse_startup_code_that_reads_the_tree(site_directories: list[str]) -> None:
    """The environment's startup code (each `.pth` file's `import` lines) runs in every Python before any
    sitecustomize, so before the census hook in each child. Run it once, under a hook installed first, and
    refuse a census whose startup code lists a tracked directory or starts a program. A presite module
    (`PYTHON_PRESITE`, `-X presite`, debug builds) runs before even that probe's hook, so it is refused."""
    if presite := [entry for entry in _startup_code([]) if entry.startswith("presite:")]:
        raise pytest.UsageError(f"the tree-read census cannot see what a presite module reads: {presite}; unset it")
    if unseen := _what_startup_code_reads(site_directories, sys.argv, str(REPO), dict(os.environ)):
        raise pytest.UsageError(f"the tree-read census cannot see what startup code (`.pth` files) reads: {unseen[:5]}")


def _what_startup_code_reads(site_directories: list[str], argv: list[str], cwd: str, env: dict[str, str]) -> list[str]:
    """Replay a Python's start (its argv, cwd and environment, which startup code may branch on) in a
    `python -S` probe that installs a hook before running each `.pth` file: the tracked directories it lists,
    and `fd` or `spawn` for a listing or a start the census cannot place. The start replayed is this Python's
    own: the probe starts with its interpreter options and shows startup code its flags (without `-S`),
    `sys.orig_argv` and user-site decision, which `-S` would otherwise change."""
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "seen.json"
        _State.own_spawn.active = True  # the census's own probe, not a test's child
        try:
            probe = [sys.executable, *_interpreter_options(sys.orig_argv), "-S", "-c", STARTUP_PROBE, str(out)]
            probe += [json.dumps(sorted(SPAWN_EVENTS))]
            start = {"argv": argv, "orig_argv": sys.orig_argv, "user_site": site.ENABLE_USER_SITE}
            probe += [json.dumps(start), *site_directories]
            # `probe` is a fixed argv list (no shell), and `env` is this process's own os.environ, replayed so
            # the probe sees the same startup branches the census is about to measure. Filtering it would make
            # the probe start a different Python than the one under test. Logged in (private path).
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            subprocess.run(probe, cwd=cwd, env=env, capture_output=True, check=True)
        finally:
            _State.own_spawn.active = False
        seen = json.loads(out.read_text(encoding="utf-8"))
    tracked = tracked_directories()
    return [path for path, event in seen if path in {"fd", "spawn"} or tree_directory(path) in tracked]


def _refuse_a_git_hook_in_the_checkout_config() -> None:
    """A `core.fsmonitor` hook in the checkout's effective config runs a program at every `git ls-files`."""
    if hooks := _fsmonitor_hooks(git_output("config", "--list")):
        raise pytest.UsageError(f"the tree-read census cannot read a git fsmonitor hook: {hooks}; unset it")


def _fsmonitor_hooks(config_list: bytes) -> list[str]:
    """The settings in `git config --list` output that make `git ls-files` run another program: a
    `core.fsmonitor` hook, or `pager.ls-files` or `pager.ls-tree`, which page through `core.pager` on a terminal."""
    lines = config_list.decode(errors="replace").lower().splitlines()
    return [  # a key with no value (`[core] fsmonitor`) prints bare, and git reads it as true
        line
        for line in lines
        if line.partition("=")[0] in {"core.fsmonitor", "pager.ls-files", "pager.ls-tree"}
        and ("=" not in line or line.partition("=")[2] not in GIT_FALSE)
    ]


def _refuse_a_git_hook_in_its_config(argv: list[str], where: str, env: Mapping[Any, Any]) -> None:
    """A `git ls-files` whose own config (from its `HOME`, `XDG_CONFIG_HOME` or checkout, as its environment and
    `NAME=value` words set them) names a `core.fsmonitor` hook runs that program: fail loud."""
    given = {os.fsdecode(name): os.fsdecode(value) for name, value in env.items()}
    given |= dict(word.split("=", 1) for word in argv if ASSIGNMENT.match(word))
    if hooks := _fsmonitor_hooks(_git_with(["config", "--list"], where, given)):
        raise RuntimeError(f"the tree-read census cannot place `{shlex.join(argv)}`: its git config runs {hooks}")


def _write_reads_from_a_forked_child() -> None:
    """A forked child (`os.fork`, multiprocessing's fork start) keeps the hook but its own copy of the reads,
    lost when it exits, so it appends them to a children file as a spawned Python child does."""
    if _State.carrier is None:  # forked before the census configured: the parent marked it unplaced
        return
    _State.child_reads = Path(_State.carrier[1]) / f"{os.getpid()}-{uuid.uuid4().hex}.jsonl"
    _State.module = _State.fork.module  # the module the parent wrote the launch for, not the one running now
    if _State.module is None:  # perhaps forked between a phase clearing the module and resetting the environment
        os.environ[CHILD_MODULE_ENV] = SESSION_READS  # so its Python children never credit a finished test
    elif _State.fork.unplaced:  # decided in the parent; the child cannot tell
        _State.module = os.environ[CHILD_MODULE_ENV] = SESSION_READS  # and its own Python children's
    elif not _State.fork.unplaced:
        os.environ[CHILD_MODULE_ENV] = _State.module
        _mark_this_child_started()


def _mark_this_child_started() -> None:
    """A hooked child marks its start, and its normal exit, so the controller can tell which still run."""
    assert _State.child_reads is not None and _State.module is not None
    started = _State.child_reads.with_suffix(".started")
    publish_atomically(started, _State.module)  # the controller may check it at any moment
    sys.addaudithook(_marks_done_as_the_interpreter_clears(os.getpid(), str(started.with_suffix(".done"))))


def publish_atomically(path: Path, text: str) -> None:
    """Write `text` to `path` so no reader sees it empty or partly written: a sibling no glob of the census
    matches, then a rename over `path`."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8", newline="\n")
    os.replace(partial, path)


def _marks_done_as_the_interpreter_clears(pid: int, done: str) -> Callable[[str, tuple[Any, ...]], None]:
    """An audit hook marking the child done when its interpreter clears, after every exit callback: an
    `atexit` handler would run before those registered earlier (startup code's, or multiprocessing's
    finalizers), which may still list. It closes over what it calls, as module globals are cleared by then."""
    getpid, create, close, flags = os.getpid, os.open, os.close, os.O_CREAT | os.O_WRONLY

    def mark(event: str, _args: tuple[Any, ...]) -> None:
        if event == "cpython.PyInterpreterState_Clear" and getpid() == pid:  # a forked grandchild inherits it
            close(create(done, flags))

    return mark


def _children_still_running(started: list[Path]) -> set[str]:
    """The modules whose hooked child started, never exited normally and is still alive: its listings may
    still be coming after the run ends, so the module stays PARTIAL."""
    unfinished = (s for s in started if not s.with_suffix(".done").exists())
    return {marker.read_text(encoding="utf-8") for marker in unfinished if _is_alive(int(marker.stem.split("-")[0]))}


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        return True  # os.kill(pid, 0) sends CTRL_C_EVENT there and no stdlib probe exists, so it stays PARTIAL
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive under another user
        return True
    return True


def _python_without_the_hook(command: list[str]) -> bool:
    """Whether a command starts a Python that never imports the census sitecustomize: `-I`, `-E` or `-S`,
    or an environment that drops the hook before it (`env -i`, `env -u NAME`, `PYTHONPATH=...`)."""
    pythons = (i for i, word in enumerate(command) if not ASSIGNMENT.match(word) and _names_a_python([word]))
    position = next(pythons, None)
    if position is None:
        return False
    if any(_drops_the_hook(word) for word in command[:position]):
        return True
    if not _forwards_python_options(command[position]):  # a launcher's own options are its own,
        inner = (i for i in range(position + 1, len(command)) if _forwards_python_options(command[i]))
        position = next(inner, -1)  # but the Python it runs (`uv run python -I`) reads its own
        if position < 0:
            return False
    options = iter(command[position + 1 :])
    for word in options:  # through the script or `-c`/`-m`, past values: `--check-hash-based-pycs always -I`
        if word in PYTHON_OPTIONS_WITH_VALUES:
            next(options, None)
        elif word.startswith("--check-hash-based-pycs="):
            continue
        elif not word.startswith("-") or word.startswith(("-c", "-m", "--")) or word == "-":
            return False
        elif set(_short_option_cluster(word)[0]) & PYTHON_SITE_OFF:
            return True
        elif _short_option_cluster(word)[1:] in {("W", ""), ("X", "")}:  # `-sW error`: its value is the next word
            next(options, None)
    return False


def _interpreter_options(orig_argv: list[str]) -> list[str]:
    """The interpreter options a Python started with (`-O`, `-X dev`, `-W error`), up to its program (`-c`,
    `-m`, a script or `-`): they set `sys.flags` and `__debug__`, which startup code may branch on. `-i` is
    left out, so a probe never waits at a prompt."""
    options: list[str] = []
    words = iter(orig_argv[1:])
    for word in words:
        if word in PYTHON_OPTIONS_WITH_VALUES:
            options += [word, next(words)]
        elif word.startswith("--check-hash-based-pycs="):
            options.append(word)
        elif not word.startswith("-") or word.startswith("--") or word == "-":
            break
        elif word.startswith("-"):
            letters, value_option, value = _short_option_cluster(word)
            program = next((i for i, letter in enumerate(letters) if letter in "cm"), len(letters))
            if flags := letters[:program].replace("i", ""):
                options.append(f"-{flags}")
            if program < len(letters):
                break
            if value_option:
                options += [f"-{value_option}", value or next(words)]
    return options


def _short_option_cluster(word: str) -> tuple[str, str, str]:
    """A short-option cluster's flag letters, then `W` or `X` (or nothing), then that option's attached value:
    `-IWerror` is `-I` and `-W error`, and `-Werror::ImportWarning` holds no flag at all."""
    cluster = SHORT_OPTION_CLUSTER.fullmatch(word)
    assert cluster is not None, word  # every word here starts with `-`
    return cluster[1], cluster[2], cluster[3]


def _forwards_python_options(word: str) -> bool:
    """Whether a program reads Python's own options: a Python, or the Windows `py` launcher."""
    return _program_name(word) == "py" or _program_name(word).startswith("python")


def _record_python_launches(commands: list[list[str]], cwd: SpawnCwd, env: object) -> None:
    """Write down, before it starts, each hooked Python a spawn runs. The report holds the module PARTIAL
    until as many children have started the hook, so one still starting when the report loads, or one
    whose hook an earlier sitecustomize shadows, never reads as measured."""
    launches = sum(1 for command in commands if _starts_a_python(command, cwd, env))
    if launches:
        _write_launches(launches, _State.module or SESSION_READS)


def _write_launches(count: int, module: str) -> None:
    """A `.launched` marker: one line naming `module` per Python about to start. The caller decides it (the
    fork hook passes the module it read before the fork), so this never re-reads `_State.module`."""
    assert _State.carrier is not None
    marker = Path(_State.carrier[1]) / f"{os.getpid()}-{uuid.uuid4().hex}.launched"
    marker.write_text(f"{module}\n" * count, encoding="utf-8", newline="\n")


UV_OPTIONS_WITH_VALUES = frozenset(  # `uv run --with pytest ruff` runs ruff: pytest is an operand. Every option
    # `uv run --help` and `uv tool run --help` (uv 0.12.8) show with a `<VALUE>`
    {"--allow-insecure-host", "--build-constraints", "--cache-dir", "--color", "--config-file", "--config-setting"}
    | {"--config-settings-package", "--constraints", "--default-index", "--directory", "--env-file"}
    | {"--exclude-newer", "--exclude-newer-package", "--extra", "--extra-index-url", "--find-links"}
    | {"--fork-strategy", "--from", "--group", "--index", "--index-strategy", "--index-url", "--keyring-provider"}
    | {"--link-mode", "--no-binary-package", "--no-build-isolation-package", "--no-build-package"}
    | {"--no-editable-package", "--no-extra", "--no-group", "--no-sources-package", "--only-group", "--overrides"}
    | {"--package", "--prerelease", "--prerelease-package", "--project", "--python", "--python-platform"}
    | {"--refresh-package", "--reinstall-package", "--resolution", "--torch-backend", "--upgrade-group"}
    | {"--upgrade-package", "--with", "--with-editable", "--with-requirements", "-C", "-P", "-b", "-c", "-f", "-i"}
    | {"-p", "-w"}
)
UV_PYTHON_MODES = frozenset({"-m", "--module", "-s", "--script", "--gui-script"})  # what follows runs as Python
UV_VALUE_LETTERS = "CPbcfipw"  # uv's short options that take a value, so a cluster's later letters are that value


def _uv_runs_a_python(words: list[str], cwd: SpawnCwd, env: object) -> bool:
    """Whether `uv run ...` or `uvx ...` runs a Python: `-m`, or a command (past uv's options and their operands)
    that is a Python, a `.py` file, an always-Python program or a script whose `#!` line names one. `uv run --help`
    runs nothing and `uv run ruff` runs ruff, so neither is a hooked launch."""
    rest = iter(words[words.index("run") + 1 :] if "run" in words else words[1:])
    for word in rest:
        if word in UV_PYTHON_MODES or any(_short_cluster_holds(word, mode, UV_VALUE_LETTERS) for mode in "ms"):
            return True  # also inside a cluster: `uv run -qs scan.py`, `uv run -qm pkg`
        if word in UV_OPTIONS_WITH_VALUES:
            next(rest, None)
        elif not word.startswith("-"):
            name = _program_name(word)
            return (
                word.endswith(".py")
                or name.startswith("python")
                or name in ALWAYS_PYTHON
                or _is_a_python_script_file(word, cwd, env)
            )
    return False


def _wrapped_program(words: list[str]) -> list[str]:
    """The word naming the program a command runs: its first, or behind wrappers (`nice -n 5 timeout 10 python`)
    the first that is no wrapper, option, number or `NAME=value`. A word before it may be an option's value or
    the real program (`nice make python`, `nice ./native-helper ./tool.py`), so nothing behind it counts."""
    if not words or _program_name(words[0]) not in COMMAND_WRAPPERS:
        return words[:1]
    return next(
        (
            [word]
            for word in words[1:]
            if not (
                _program_name(word) in COMMAND_WRAPPERS
                or word.startswith("-")
                or word.isdigit()
                or ASSIGNMENT.match(word)
            )
        ),
        [],
    )


def _starts_a_python(command: list[str], cwd: SpawnCwd, env: object) -> bool:
    """Whether a command surely starts a Python: one named as its program (also behind a wrapper such as
    `nice`), a program that is always Python (`pytest`, `pip`, `py`), `uv run`, or a python `#!` script."""
    words = _program_words(command)
    names = [_program_name(word) for word in words]
    if not names:
        return False
    programs = [_program_name(word) for word in _wrapped_program(words)]
    return (
        any(name.startswith("python") or name in ALWAYS_PYTHON for name in programs)
        or (names[0] in {"uv", "uvx"} and ("run" in words or names[0] == "uvx") and _uv_runs_a_python(words, cwd, env))
        or _runs_a_python_script(command, cwd, env)
    )


def _record_an_unmeasured_child(module: str) -> None:
    """A child whose listings the census cannot see leaves its test module PARTIAL; repository code's, or a
    long-lived helper's, has no module to hold PARTIAL, so it is an unplaced read."""
    if module == SESSION_READS:
        _record_reads(SESSION_READS, ["."])
    elif _State.child_reads is None:
        _State.incomplete.add(module)
    elif _State.child_reads is not None:
        with (_State.child_reads.parent / f"{os.getpid()}.unhooked").open(
            "a", encoding="utf-8", newline="\n"
        ) as marker:
            marker.write(module + "\n")  # appended: a reused pid must not erase another module's marker


def install_child_hook() -> None:
    """In a Python child of a measured test, loaded by the census sitecustomize: credit its listings, and
    its own children's, to the test module that started it."""
    _State.module = os.environ[CHILD_MODULE_ENV]
    _State.child_reads = Path(os.environ[CHILD_READS_ENV]) / f"{os.getpid()}-{uuid.uuid4().hex}.jsonl"
    _remember_the_carrier()
    assert _State.carrier is not None
    _State.git = (Path(_State.carrier[0]) / CENSUS_GIT_FILE).read_text(encoding="utf-8")  # never this PATH's
    helper = _multiprocessing_start(sys.orig_argv) == "helper"
    if helper:  # long-lived: what it and its children list is no one test's, so unplaced
        _State.module = SESSION_READS
    elif not helper:  # no launch counted it, and it outlives the run: its children check in for themselves
        _mark_this_child_started()
    assert _State.carrier is not None
    known = (Path(_State.carrier[0]) / STARTUP_CODE_FILE).read_text(encoding="utf-8").splitlines()
    site_directories = _site_directories()
    if not set(_startup_code(site_directories)) <= set(known):  # it ran before the hook could listen
        _record_an_unmeasured_child(_State.module)
    elif _what_startup_code_reads(site_directories, sys.argv, os.getcwd(), dict(os.environ)):  # its own start
        _record_an_unmeasured_child(_State.module)  # the same lines, branching on this child's argv, cwd or env
    _tracked_directory_identities()
    sys.addaudithook(_audit)
    _track_forks()
    _watch_multiprocessing_starts()  # a forked grandchild (a daemon's double fork) launches and starts as one here does


def _multiprocessing_start(argv: list[str]) -> str | None:
    """Which multiprocessing start a Python command line is, judged by its `-c` code."""
    code = argv[argv.index("-c") + 1] if "-c" in argv[:-1] else ""
    return next((kind for start, kind in MULTIPROCESSING_STARTS.items() if code.startswith(start)), None)


def _site_directories() -> list[str]:
    """The directories whose `.pth` files this Python's `site` ran, in its order: the user site when enabled,
    then its prefixes' site-packages."""
    return [*([site.getusersitepackages()] if site.ENABLE_USER_SITE else []), *site.getsitepackages()]


def _startup_code(site_directories: list[str]) -> list[str]:
    """What a Python runs before any sitecustomize, so before the census hook listens: each executable line
    (`import ...`) of a `.pth` file in its site directories, and a `PYTHON_PRESITE` or `-X presite` module.
    A child is measured only if it ran nothing the controller's own Python did not."""
    code = [f"presite:{name}" for name in (os.environ.get("PYTHON_PRESITE"), sys._xoptions.get("presite")) if name]
    for directory in filter(os.path.isdir, site_directories):
        for pth in sorted(Path(directory).glob("*.pth")):
            lines = pth.read_text(encoding="utf-8", errors="replace").splitlines()
            code += [f"{pth}:{line}" for line in lines if line.startswith(("import ", "import\t"))]
    return code


def _watch_multiprocessing_starts() -> None:
    """multiprocessing starts spawn and forkserver children and its resource tracker through
    `util.spawnv_passfds`, which raises no audit event before Python 3.14: wrapped, so each start is counted
    as a Popen of the same command is, before the child runs."""
    start = multiprocessing.util.spawnv_passfds

    @functools.wraps(start)
    def counted(path: Any, args: Any, passfds: Any) -> int:
        _record_a_multiprocessing_start(path, args)
        return start(path, args, passfds)

    multiprocessing.util.spawnv_passfds = counted


def _record_a_multiprocessing_start(path: Any, args: Any) -> None:
    """A start that cannot load the hook (a test changed `os.environ` before `Process.start()`, or
    `set_executable` named another program) is a child the census cannot read. A hooked one is a launch its
    `.started` marker pairs, except a long-lived helper's (the resource tracker, the forkserver), which loads
    the hook under `<session>`. One started while no module runs, or on a pool thread, is unplaced even when
    hooked (a hooked Popen started while no module runs is measured under `<session>`): unplaced errs safe."""
    module = _State.module  # read once: another thread may move the phase between this check and the marker
    argv = _argv_as_run(path, [os.fsdecode(word) for word in args])
    if module is None or _a_pool_thread_runs_this():  # its child carries no module, or the wrong one
        _record_reads(SESSION_READS, ["."])  # unplaced: the report says UNKNOWN, hooked or not
    elif _python_without_the_hook(argv) or not _starts_a_hooked_python(argv, None, None):
        _mark_an_unread_child()
    elif _multiprocessing_start(argv) != "helper":
        _write_launches(1, module)


def _track_forks() -> None:
    """Count each fork as a launch and have the forked child write its own reads, from the moment the hook
    listens. POSIX only: Windows has no fork, so no forked child to lose."""
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(before=_record_a_fork_launch, after_in_child=_write_reads_from_a_forked_child)


def _install_the_child_hook_for(out_dir: Path) -> None:
    """Controller only, before any worker starts: every Python process from here on loads the census
    sitecustomize from PYTHONPATH; it stays inert unless CHILD_MODULE_ENV names a module."""
    hook_dir, children = out_dir / CHILD_HOOK_DIR, out_dir / CHILD_READS_DIR
    hook_dir.mkdir(parents=True)
    children.mkdir()
    source = SITECUSTOMIZE.format(
        module_env=CHILD_MODULE_ENV,
        reads_env=CHILD_READS_ENV,
        plugin=str(Path(__file__).resolve()),
        bootstrap=EXECNET_BOOTSTRAP,
    )
    compile(source, "sitecustomize.py", "exec")  # Python skips a broken sitecustomize silently; fail loud here
    (hook_dir / "sitecustomize.py").write_text(source, encoding="utf-8", newline="\n")
    startup = "\n".join(sorted(_startup_code(_site_directories())))
    (hook_dir / STARTUP_CODE_FILE).write_text(startup, encoding="utf-8", newline="\n")
    (hook_dir / CENSUS_GIT_FILE).write_text(census_git(), encoding="utf-8", newline="\n")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(hook_dir), os.environ.get("PYTHONPATH")]))
    os.environ[CHILD_READS_ENV] = str(children)
    os.environ[CHILD_MODULE_ENV] = SESSION_READS  # repository code's Python children: listings unplaced, never lost


def _repository_relative(path: Path) -> str:
    return Path(path).resolve().relative_to(REPO).as_posix()


def _module_of(nodeid: str) -> str:
    """The module a node id's file names (an inherited test's own module), resolved once, at collection: a fixture
    or test that patches `os.stat`, which `Path.resolve` calls, must not break the hooks around its phases. The
    xdist controller collects nothing and asks first from its reports, with nothing patched in its process."""
    return _module_of_node_file(nodeid.partition("::")[0])


@functools.cache
def _module_of_node_file(node_file: str) -> str:
    return _repository_relative(REPO / node_file)


# -----------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    if not os.environ.get(OUT_DIR_ENV):
        raise pytest.UsageError(f"{OUT_DIR_ENV} must name the census output directory when this plugin is loaded")
    if config.rootpath.resolve() != REPO:  # node ids are relative to it, and modules are keyed from the checkout
        raise pytest.UsageError(f"the tree-read census needs the checkout as pytest's rootdir, not {config.rootpath}")
    stops_early = (
        config.getoption("collectonly", False)
        or config.getoption("maxfail", 0)
        or config.getoption("stepwise", False)
        or config.getoption("lf", False)  # skips collecting every module without a cached failure
    )
    if stops_early or any("::" in arg for arg in config.args):  # a node id runs part of its module, unseen
        raise pytest.UsageError(
            "the tree-read census needs every collected test to run: drop --collect-only, -x, --maxfail, --sw, --lf and node ids"
        )
    out_dir = Path(os.environ[OUT_DIR_ENV]).resolve()  # absolute: children may start in another directory
    if not hasattr(config, "workerinput") and out_dir.is_dir() and any(out_dir.iterdir()):
        raise pytest.UsageError(f"{out_dir} already holds census files; use an empty directory so no stale run merges")
    if not hasattr(config, "workerinput"):
        _refuse_startup_code_that_reads_the_tree(_site_directories())
        _install_the_child_hook_for(out_dir)
    _remember_the_carrier()


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Listen from before pytest imports the initial conftests, so a scan at a conftest's top level counts.
    The census is written by the first cleanup registered, which runs last: after every `pytest_unconfigure`,
    hookwrapper teardowns included, and after every cleanup a conftest or plugin adds later. `add_cleanup`
    hides whether one of those raised, so the writer goes on the same stack as an exit callback, which sees.
    The checkout's git config is checked first, by `git config --list`, which reads no index: a hook there runs
    at the first git that does. Then the opening snapshot, before a conftest can change code and restore it.
    Each process takes one (an xdist worker is not known yet); only the controller's is written. Before all
    of it, the census pins the git it runs and trusts, while PATH is still the one pytest started with."""
    census_git()
    _refuse_a_git_hook_in_the_checkout_config()
    _State.snapshot = checkout_snapshot()
    _tracked_directory_identities()  # its git and stat calls must not reach the hook
    sys.addaudithook(_audit)
    _track_forks()
    _watch_multiprocessing_starts()
    early_config._cleanup_stack.push(functools.partial(_write_census_unless_a_cleanup_raised, early_config))


def _write_census_unless_a_cleanup_raised(
    config: pytest.Config, raised: type[BaseException] | None, *_: object
) -> None:
    """A cleanup that raised cut the run short after its exit status was taken: no census, so no finished run."""
    if raised is None:
        _write_census(config)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: str | None) -> None:
    """xdist, on the controller: a worker shut down, cleanly (error None) or not."""
    _State.workers[node.gateway.id] = error


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    module = collector if isinstance(collector, pytest.Module) else collector.getparent(pytest.Module)
    if module is None:  # a directory or package collector, outside any test module
        return (yield)
    _State.module = os.environ[CHILD_MODULE_ENV] = _repository_relative(module.path)  # a Class runs its module's code
    _State.measured.add(_State.module)
    threads_before = set(threading.enumerate())
    _enter_a_module_phase(threads_before)
    try:
        report = yield
        if report.failed or report.skipped:  # a module-level skip (`importorskip`) runs no test at all
            _State.incomplete.add(_State.module)
        return report
    finally:
        _mark_a_module_that_leaves_a_thread_running(threads_before)
        _State.older_threads, _State.lineage_unknown = set(), False
        _State.module = None
        os.environ[CHILD_MODULE_ENV] = SESSION_READS


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    _State.module = os.environ[CHILD_MODULE_ENV] = _module_of(item.nodeid)  # a module-scoped patch may be live
    threads_before = set(threading.enumerate())
    _enter_a_module_phase(threads_before)
    try:
        yield
    finally:
        _mark_a_module_that_leaves_a_thread_running(threads_before)
        _State.older_threads, _State.lineage_unknown = set(), False
        _State.module = None
        os.environ[CHILD_MODULE_ENV] = SESSION_READS


def _enter_a_module_phase(threads_before: set[threading.Thread]) -> None:
    """What threads older than this phase do is held as unplaced. While any lives (one a module left running, or
    one session code started), so is what any thread born in this phase does: nothing on Python 3.11 records which
    thread started another, so it may be that older one's descendant. A test runs on the main thread, under
    xdist too, so a run with no such thread credits each module its own threads."""
    _State.older_threads = threads_before - {threading.current_thread()}
    _State.lineage_unknown = bool(_State.older_threads)


def _mark_a_module_that_leaves_a_thread_running(threads_before: set[threading.Thread]) -> None:
    """A thread the current module started that outlives this test (or its collection) may list later,
    while another module runs or none does, so that listing cannot be credited to its owner: PARTIAL."""
    assert _State.module is not None
    if set(threading.enumerate()) - threads_before:  # enumerate holds only threads still alive
        _State.incomplete.add(_State.module)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Every test left to run after deselection. Under xdist each worker collects them all and the
    controller none, so the report checks the union against the union of `ran`."""
    for item in session.items:
        _State.collected.setdefault(_repository_relative(item.path), set()).add(item.nodeid)
    kept = {item.nodeid for item in session.items}  # a hook that drops items without `pytest_deselected`
    _State.incomplete.update(module for module, ids in _State.yielded.items() if ids - kept)


def pytest_itemcollected(item: pytest.Item) -> None:
    _State.yielded.setdefault(_repository_relative(item.path), set()).add(item.nodeid)
    _module_of(item.nodeid)  # cached before any test runs


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Only a phase that passed ran to its end. A skip never ran the body; a failure, an error or an
    xfail (a raise, `pytest.xfail()` or `run=False`) stopped it early, before any listing after that
    point. Any of them leaves the module not fully measured. A test with no passing call phase at all
    (xdist collected it and ran nothing) shows up in the report as collected but not `ran`."""
    module = _module_of(report.nodeid)
    if not report.passed:
        _State.incomplete.add(module)
    elif report.when == "call":
        _State.ran.setdefault(module, set()).add(report.nodeid)


def pytest_deselected(items: Iterable[pytest.Item]) -> None:
    """A deselected test (`-k`, `-m` or a plugin) never runs its body, so its module is not fully measured."""
    _State.incomplete.update(_repository_relative(item.path) for item in items)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session) -> Generator[None, None, None]:
    """Whether every `pytest_sessionfinish` hook inside this wrapper returned: one that raises (`pytest.exit` too)
    leaves no status, so the report refuses the run. A wrapper registered later (a conftest's) runs outside this
    one and may still change the status, so the write reads the session's final one."""
    yield
    _State.exitstatus, _State.session = int(session.exitstatus), session
    session.config.pluginmanager.register(_TeardownGuard())  # after every conftest: the outermost wrapper


class _TeardownGuard:
    """A run whose teardown raised exits nonzero, and a later teardown hook's listings may never happen, so
    it records no run: the report refuses it as never finished. Registered last, its wrapper surrounds
    every other `pytest_unconfigure`, a conftest's `tryfirst` wrapper too."""

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_unconfigure(self) -> Generator[None, None, None]:
        try:
            yield
        except BaseException:
            _State.exitstatus = None
            raise


def _write_census(config: pytest.Config) -> None:
    """Write this process's census after every other end-of-run hook, so a listing there still reaches
    `<session>`."""
    if _State.exitstatus is None:  # the session never finished, so there is no run to record
        return
    assert _State.session is not None
    _State.exitstatus = int(_State.session.exitstatus)  # what pytest exits with: no hook runs after this write
    # xdist's own record of a worker, not PYTEST_XDIST_WORKER: a pytest run a test spawns inherits that
    # variable, and would then write as a worker with no controller file beside it.
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    census: dict[str, object] = {
        "measured": sorted(_State.measured),
        "incomplete": sorted(_State.incomplete),
        "reads": {module: sorted(dirs) for module, dirs in sorted(_State.reads.items())},
        "collected": {module: sorted(ids) for module, ids in sorted(_State.collected.items())},
        "ran": {module: sorted(ids) for module, ids in sorted(_State.ran.items())},
        "exitstatus": _State.exitstatus,
        "workers": dict(sorted(_State.workers.items())),
    }
    assert _State.carrier is not None
    if worker == "main":  # after every worker: a hooked child still alive now may list after this file is read
        started = sorted(Path(_State.carrier[1]).glob("*.started"))  # the report judges any child not in it
        running = _children_still_running(started)
        census |= {"incomplete": sorted(_State.incomplete | running), "children_checked": [s.name for s in started]}
        census |= {"snapshot": _State.snapshot, "snapshot_at_end": checkout_snapshot(), "platform": sys.platform}
    out = Path(_State.carrier[1]).parent / f"{worker}.json"  # resolved at configure, not the raw env value
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(census, indent=1) + "\n", encoding="utf-8", newline="\n")
