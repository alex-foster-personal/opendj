"""Tree-sitter JS/TS child_process reader for runner-toolset scan (CI-12661).

- [if] CI package scripts spawn binaries through JS [then] literals scan and variables stay UNKNOWN, [else stop].
"""

from __future__ import annotations

import pytest

from scripts import runner_toolset_js as reader
from scripts import runner_toolset_shell_lex as lex
from scripts import runner_toolset_scan as scan

pytestmark = pytest.mark.requirement("CI-12661")

FIXTURE_SPAWN = """
import { spawn } from 'node:child_process';
spawn('uv', ['run', '--no-sync', 'python', '-m', 'scripts.testing.midi_maps_test_server']);
"""


def test_literal_spawn_argv_is_a_shell_line() -> None:
    """[if] spawn uses literal uv argv [then] shell line names midi server module, [else stop]."""
    hits = reader.commands(FIXTURE_SPAWN, path="fixture.mjs")
    assert any(hit.kind == "shell" and "scripts.testing.midi_maps_test_server" in hit.text for hit in hits)


def test_exec_sync_shell_string_is_reported() -> None:
    """[if] execSync uses a shell string [then] ffmpeg -version is reported, [else stop]."""
    source = "import { execSync } from 'node:child_process';\nexecSync('ffmpeg -version');\n"
    hits = reader.commands(source, path="fixture.mjs")
    assert any(hit.kind == "shell" and hit.text == "ffmpeg -version" for hit in hits)


def test_variable_spawn_argv_is_unknown() -> None:
    """[if] spawn argv is variable [then] UNKNOWN is reported, [else stop]."""
    source = "import { spawn } from 'node:child_process';\nspawn(cmd, args);\n"
    hits = reader.commands(source, path="fixture.mjs")
    assert any(hit.kind == "unresolved-argv" and hit.text == lex.UNKNOWN for hit in hits)


def test_child_process_mention_without_call_is_silent() -> None:
    """[if] child_process appears only in a string [then] no commands are reported, [else stop]."""
    source = "// talks about child_process\nconst note = 'node:child_process';\n"
    assert reader.commands(source, path="fixture.mjs") == []


def test_literal_shell_line_reaches_shell_scanner() -> None:
    """[if] a literal shell line is reported [then] scan_shell sees ffmpeg, [else stop]."""
    source = "import { execSync } from 'node:child_process';\nexecSync('ffmpeg -version');\n"
    command = next(hit for hit in reader.commands(source, path="fixture.mjs") if hit.kind == "shell")
    usage = scan.Usage()
    scan.scan_shell(command.text, "fixture.mjs", command.line - 1, scan._Ctx(usage=usage))
    assert "ffmpeg" in usage.executables
