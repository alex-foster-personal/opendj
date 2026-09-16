"""pytest plugin: record which repository files each test module actually touches.

SMARTEST-CI (specs/ci-fail-fast.md, part 3). A static import graph cannot see a test that
reads a data file, lists a directory, or globs the frontend tree, and measured Wed 16 Sep
2026 72 test files reference frontend paths while 16 of the last 80 merged pull requests
touched only the frontend. Test impact analysis tools (Bazel data dependencies, Launchable,
Datadog ITR) learn those edges by tracing; this plugin does it with CPython audit hooks.

Loaded explicitly, never by default:
    TEST_IMPACT_OUT=.tmp/impact pytest -p scripts.test_impact_trace tests/...

Records, per test module file (the unit selection runs at):
    files      repository files opened (the `open` audit event, which `io.open_code` fires
               too, so imports count)
    dirs       directories listed or scanned (`os.listdir`, `os.scandir`), so a test that
               walks a tree depends on every file later added under it
    globs      patterns passed to `glob.glob` / `glob.iglob`
    spawns     the programs it started as subprocesses (argv[0] basenames). A child's own
               reads are NOT traced, so a spawning module is selected for every change.
    fixtures   fixture names its tests used, so reads made while a shared fixture was set up
               are attributed to every module that uses that fixture
Reads made while a directory is collected (its conftest.py imports there) are recorded under
`dir:<path>` and apply to every module beneath it; reads before collection starts (the root
conftest, pytest's own config reads) under `dir:.`, which applies to every module.

Output: one JSON file per process (`<TEST_IMPACT_OUT>/impact-<worker>.json`), merged by
`scripts/test_impact_map.py`. A process that recorded nothing still writes its file, so an
absent file means the process did not finish, never that nothing was read.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OUT_ENV = "TEST_IMPACT_OUT"
IGNORED_PARTS = frozenset({".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"})
PATH_EVENTS = frozenset({"open", "os.listdir", "os.scandir"})
GLOB_EVENTS = frozenset({"glob.glob", "glob.glob/2"})
SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.system", "os.exec"})


def _source_of_cached_bytecode(relative: str) -> str:
    """`pkg/__pycache__/mod.cpython-311[-pytest-9.1.1].pyc` -> `pkg/mod.py`, unchanged otherwise.

    An import reads the SOURCE only when no valid cached bytecode exists. With a warm cache
    CPython opens the `.pyc` instead, and `__pycache__` is ignored here, so every import edge
    vanished: measured Wed 16 Sep 2026, a module whose first line imports the module under
    test recorded zero files warm and both files cold. Selection would then silently narrow,
    which is the unsafe direction, so the bytecode is credited to the source it was built from.
    """
    parent, sep, name = relative.rpartition(os.sep)
    if not sep or os.path.basename(parent) != "__pycache__" or not name.endswith(".pyc"):
        return relative
    return os.path.join(os.path.dirname(parent), name.split(".", 1)[0] + ".py")


def _program(event: str, args: tuple) -> str:
    """The spawned program's basename, or the event name when the args do not say."""
    candidate: object = None
    if event == "subprocess.Popen" and len(args) >= 2:
        argv = args[1]
        candidate = args[0] or (argv[0] if isinstance(argv, list | tuple) and argv else argv)
    elif event in ("os.posix_spawn", "os.exec") and args:
        candidate = args[0]
    elif event == "os.system" and args:
        candidate = os.fsdecode(args[0]).split(" ", 1)[0] if args[0] else None
    if isinstance(candidate, bytes | str | os.PathLike):
        return os.path.basename(os.fsdecode(candidate).split(" ", 1)[0]) or event
    return event


@dataclass
class Record:
    files: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)
    globs: set[str] = field(default_factory=set)
    fixtures: set[str] = field(default_factory=set)
    spawns: set[str] = field(default_factory=set)

    def as_json(self) -> dict:
        return {
            "files": sorted(self.files),
            "dirs": sorted(self.dirs),
            "globs": sorted(self.globs),
            "fixtures": sorted(self.fixtures),
            "spawns": sorted(self.spawns),
        }


class Tracer:
    """Attributes audit events to whatever is running: a test module, fixture or conftest."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.repo_prefix = str(repo) + os.sep
        self.records: dict[str, Record] = defaultdict(Record)
        self.current: list[str] = ["dir:."]
        self.active = True

    def _relative(self, raw: object) -> str | None:
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if not isinstance(raw, str | os.PathLike):
            return None
        path = os.path.abspath(os.fspath(raw))
        if not path.startswith(self.repo_prefix):
            return None
        relative = _source_of_cached_bytecode(path[len(self.repo_prefix) :])
        if IGNORED_PARTS & set(relative.split(os.sep)):
            return None
        return relative.replace(os.sep, "/")

    def audit(self, event: str, args: tuple) -> None:
        if not self.active or not self.current:
            return
        key = self.current[-1]
        if event in PATH_EVENTS:
            relative = self._relative(args[0] if args else None)
            if relative is None:
                return
            target = self.records[key].files if event == "open" else self.records[key].dirs
            target.add(relative)
        elif event in GLOB_EVENTS:
            pattern = args[0] if args else None
            if isinstance(pattern, str | bytes):
                self.records[key].globs.add(os.fsdecode(pattern))
        elif event in SPAWN_EVENTS:
            self.records[key].spawns.add(_program(event, args))


_TRACER = Tracer(REPO)
# Installed at import, not at pytest_configure: `-p` imports this before conftests load, so the
# root conftest's reads are attributed (to `dir:.`) instead of lost.
sys.addaudithook(_TRACER.audit)


def _worker_name(config: pytest.Config) -> str:
    worker = getattr(config, "workerinput", {}).get("workerid")
    return worker or "main"


def _module_key(nodeid_path: str) -> str:
    return "module:" + nodeid_path.split("::", 1)[0]


def pytest_configure() -> None:
    if not os.environ.get(OUT_ENV):
        raise pytest.UsageError(f"scripts.test_impact_trace needs {OUT_ENV} set to a directory")


def pytest_sessionstart() -> None:
    """Startup is over: pop `dir:.` so nothing later falls back to "every module"."""
    if _TRACER.current == ["dir:."]:
        _TRACER.current.pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    path = getattr(collector, "path", None)
    relative = _TRACER._relative(path) if path is not None else None
    if relative is None:
        yield
        return
    kind = "module:" if isinstance(collector, pytest.Module) else "dir:"
    _TRACER.current.append(
        kind
        + (relative if kind == "module:" or Path(path).is_dir() else str(Path(relative).parent))
    )
    try:
        yield
    finally:
        _TRACER.current.pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef):
    _TRACER.current.append("fixture:" + fixturedef.argname)
    try:
        yield
    finally:
        _TRACER.current.pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item):
    key = _module_key(item.nodeid)
    _TRACER.records[key].fixtures.update(getattr(item, "fixturenames", ()))
    _TRACER.current.append(key)
    try:
        yield
    finally:
        _TRACER.current.pop()


def pytest_sessionfinish(session: pytest.Session) -> None:
    _TRACER.active = False
    out = Path(os.environ[OUT_ENV])
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "rootdir": str(REPO),
        "worker": _worker_name(session.config),
        "records": {key: record.as_json() for key, record in sorted(_TRACER.records.items())},
    }
    target = out / f"impact-{_worker_name(session.config)}.json"
    target.write_text(json.dumps(payload, indent=1), encoding="utf-8")
