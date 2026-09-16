"""`scripts/impact_trace_plugin.py`: what the audit hook records and who it is attributed to.

SMARTEST-CI round 4. The plugin's failure mode is silence: a read it misses produces an
empty record, an empty record narrows selection, and a narrowed selection skips a test that
would have failed. So every recorder here is proven to FIRE on a real call, and the filters
are proven to reject only what they claim to reject.

  - [if] a repository file is opened under a key [then] that key records it, [else stop].
  - [if] a path outside the repository is opened [then] nothing is recorded, [else stop].
  - [if] cached bytecode is read [then] the SOURCE file is recorded, [else stop].
  - [if] `glob.glob` runs [then] the pattern is recorded, repo-relative, [else stop].
  - [if] the plugin runs a real session [then] a module key is written to disk, [else stop].
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

import pytest

from scripts.impact_trace_plugin import (
    IGNORED_PARTS,
    REPO,
    STARTUP_KEY,
    Tracer,
    _program,
    _source_of_cached_bytecode,
)

# `pytester` runs a real pytest session in a subprocess, which is the only thing that proves
# the hook SIGNATURES are ones pytest accepts. Without this the two end-to-end tests below
# ERROR on a missing fixture and read as coverage while testing nothing.
pytest_plugins = ["pytester"]


@pytest.fixture
def tracer(tmp_path) -> Tracer:
    traced = Tracer(tmp_path)
    traced.current.append("module:tests/test_a.py")
    return traced


def _open_event(path) -> tuple[str, tuple]:
    return "open", (str(path), "r", None)


# ----- what is in scope -----


def test_opening_a_repository_file_is_recorded_under_the_current_key(tracer, tmp_path):
    tracer.audit(*_open_event(tmp_path / "apps" / "engine.py"))
    assert tracer.records["module:tests/test_a.py"].files == {"apps/engine.py"}


def test_a_path_outside_the_repository_is_recorded_nowhere(tracer, tmp_path):
    tracer.audit(*_open_event(tmp_path.parent / "elsewhere" / "engine.py"))
    assert not tracer.records


def test_a_path_that_merely_starts_with_the_repository_name_is_not_inside_it(tracer, tmp_path):
    """`/repo-backup/x.py` must not be read as `/repo/...`; the separator is what decides."""
    tracer.audit(*_open_event(str(tmp_path) + "-backup/x.py"))
    assert not tracer.records


@pytest.mark.parametrize("ignored", sorted(IGNORED_PARTS - {"__pycache__"}))
def test_vendored_and_generated_directories_are_ignored(tracer, tmp_path, ignored):
    tracer.audit(*_open_event(tmp_path / ignored / "deep" / "thing.py"))
    assert not tracer.records


def test_nothing_is_recorded_once_the_session_has_finished(tracer, tmp_path):
    """`pytest_sessionfinish` freezes the record; later interpreter shutdown reads are noise."""
    tracer.active = False
    tracer.audit(*_open_event(tmp_path / "apps" / "engine.py"))
    assert not tracer.records


def test_nothing_is_recorded_when_no_key_is_current(tmp_path):
    """Between collection and the first test there is no owner, and a guess would be wrong."""
    traced = Tracer(tmp_path)
    traced.current.clear()
    traced.audit(*_open_event(tmp_path / "apps" / "engine.py"))
    assert not traced.records


# ----- cached bytecode maps back to its source -----


@pytest.mark.parametrize(
    ("cached", "source"),
    [
        (os.path.join("scripts", "__pycache__", "affected_tests.cpython-311.pyc"),
         os.path.join("scripts", "affected_tests.py")),
        (os.path.join("tests", "__pycache__", "conftest.cpython-311-pytest-9.1.1.pyc"),
         os.path.join("tests", "conftest.py")),
        (os.path.join("__pycache__", "top.cpython-311.pyc"), "top.py"),
    ],
)
def test_cached_bytecode_is_credited_to_its_source(cached, source):
    assert _source_of_cached_bytecode(cached) == source


@pytest.mark.parametrize(
    "unchanged",
    [
        os.path.join("scripts", "affected_tests.py"),
        os.path.join("scripts", "__pycache__", "notes.txt"),
        os.path.join("scripts", "cache", "affected_tests.cpython-311.pyc"),
        "bare.pyc",
    ],
)
def test_other_paths_are_left_alone(unchanged):
    assert _source_of_cached_bytecode(unchanged) == unchanged


def test_importing_with_a_warm_cache_records_the_source_not_the_bytecode(tracer, tmp_path):
    """Warm and cold caches must agree. Measured Wed 16 Sep 2026 before this mapping existed:
    a module whose first statement imports the module under test recorded ZERO files warm and
    both files cold, so selection silently narrowed wherever a cache survived."""
    tracer.audit(*_open_event(tmp_path / "scripts" / "__pycache__" / "engine.cpython-311.pyc"))
    assert tracer.records["module:tests/test_a.py"].files == {"scripts/engine.py"}


# ----- directories, globs and spawns -----


@pytest.mark.parametrize("event", ["os.listdir", "os.scandir"])
def test_listing_a_directory_is_recorded_as_a_directory(tracer, tmp_path, event):
    tracer.audit(event, (str(tmp_path / "apps" / "webui"),))
    record = tracer.records["module:tests/test_a.py"]
    assert record.dirs == {"apps/webui"}
    assert not record.files


def test_a_directory_listed_during_startup_is_not_recorded(tmp_path):
    """Nothing is collected yet at startup, so a listing there is the import machinery or
    pytest walking the tree. Recorded, one scan of `apps/` made a change to any file in it
    select every module in the map: measured Wed 16 Sep 2026, 209 of 209."""
    traced = Tracer(tmp_path)
    assert traced.current == [STARTUP_KEY]
    traced.audit("os.scandir", (str(tmp_path / "apps"),))
    assert not traced.records


def test_a_file_read_during_startup_is_still_recorded(tmp_path):
    """The control: the root conftest's own imports genuinely do reach every test, and
    dropping them with the listings would delete the record that keeps selection safe."""
    traced = Tracer(tmp_path)
    traced.audit(*_open_event(tmp_path / "apps" / "shared" / "state.py"))
    assert traced.records[STARTUP_KEY].files == {"apps/shared/state.py"}


def test_a_directory_listed_after_startup_is_recorded(tmp_path):
    """The other control: the listing recorder must still fire once a key names a module."""
    traced = Tracer(tmp_path)
    traced.current.append("module:tests/test_a.py")
    traced.audit("os.scandir", (str(tmp_path / "data"),))
    assert traced.records["module:tests/test_a.py"].dirs == {"data"}


def test_a_real_glob_call_is_recorded(tracer, tmp_path):
    """Live probe, not a synthetic event. The traced subset recorded zero globs, and this is
    what proves that zero was the suite's shape rather than a recorder that never fires."""
    sys.addaudithook(tracer.audit)
    glob.glob(str(tmp_path / "apps" / "*.py"))
    assert tracer.records["module:tests/test_a.py"].globs == {"apps/*.py"}


def test_a_real_subprocess_records_the_program_it_ran(tracer):
    """Also live: `subprocess.Popen` is the event name, but the argv shape is CPython's."""
    sys.addaudithook(tracer.audit)
    subprocess.run([sys.executable, "-c", ""], check=True, capture_output=True)
    assert os.path.basename(sys.executable) in tracer.records["module:tests/test_a.py"].spawns


@pytest.mark.parametrize(
    ("event", "args", "expected"),
    [
        ("subprocess.Popen", ("/usr/bin/git", ["git", "status"], None, None), "git"),
        ("subprocess.Popen", (None, ["/opt/homebrew/bin/gh", "pr"], None, None), "gh"),
        ("os.posix_spawn", ("/usr/bin/ffmpeg", ["ffmpeg"]), "ffmpeg"),
        ("os.system", (b"lsof -i :8000",), "lsof"),
    ],
)
def test_the_spawned_program_is_named_by_its_basename(tracer, event, args, expected):
    tracer.audit(event, args)
    assert tracer.records["module:tests/test_a.py"].spawns == {expected}


@pytest.mark.parametrize(
    ("event", "args"),
    [("subprocess.Popen", (None, None, None, None)), ("os.system", (b"",)), ("os.exec", ())],
)
def test_a_spawn_whose_argv_says_nothing_is_recorded_as_the_event(tracer, event, args):
    """UNKNOWN beats a guess: the module still counts as spawning, so it is never ruled out."""
    assert _program(event, args) == event
    tracer.audit(event, args)
    assert tracer.records["module:tests/test_a.py"].spawns == {event}


# ----- the plugin end to end -----


def test_a_real_session_writes_a_module_record(pytester, tmp_path, monkeypatch):
    """The hook signatures are pytest's, declared as a subset of each hookspec. A wrong name
    or an extra argument makes pytest refuse the plugin, and this is what catches that."""
    pytester.makepyfile(
        test_traced="""
        def test_reads_a_file(tmp_path):
            (tmp_path / 'ignored-outside-repo.txt').write_text('x')
        """
    )
    out = tmp_path / "impact"
    monkeypatch.setenv("TEST_IMPACT_OUT", str(out))
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    result = pytester.runpytest_subprocess("-p", "scripts.impact_trace_plugin", "-q")
    result.assert_outcomes(passed=1)
    written = list(out.glob("impact-*.json"))
    assert written, f"the plugin wrote no trace into {out}"
    records = json.loads(written[0].read_text(encoding="utf-8"))["records"]
    assert any(key.startswith("module:") and key.endswith("test_traced.py") for key in records)


def test_the_plugin_refuses_to_run_without_an_output_directory(pytester, monkeypatch):
    """A tracer that records into nowhere is worse than no tracer: it reports an empty map."""
    pytester.makepyfile(test_x="def test_x(): pass")
    monkeypatch.delenv("TEST_IMPACT_OUT", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    result = pytester.runpytest_subprocess("-p", "scripts.impact_trace_plugin", "-q")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*TEST_IMPACT_OUT*"])
