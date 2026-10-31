"""The Cmd K cue-preview CI gate helper resolves its programs by path and says what is missing."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "ci_cmdk_preview_cue_gate", REPOSITORY_ROOT / "scripts" / "ci_cmdk_preview_cue_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHILD = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("gate", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.resolve(sys.argv[2]))
"""


def _resolve_in_child(name: str, path_value: str) -> subprocess.CompletedProcess[str]:
    # A real child process whose environment holds only the PATH under test, so the lookup is the
    # interpreter's own rather than a monkeypatched one. SYSTEMROOT is kept on Windows, where Python
    # will not start without it.
    gate_path = REPOSITORY_ROOT / "scripts" / "ci_cmdk_preview_cue_gate.py"
    env = {key: os.environ[key] for key in ("SYSTEMROOT",) if key in os.environ}
    env["PATH"] = path_value
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(gate_path), name],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolve_names_the_missing_program(tmp_path: Path) -> None:
    # An empty PATH: nothing on it is called gcc, so the lookup must refuse and name it.
    result = _resolve_in_child("gcc", str(tmp_path))
    assert result.returncode != 0
    assert "gcc is not on PATH" in result.stderr


def test_resolve_returns_the_path_it_found() -> None:
    # A real executable: this interpreter, found through a PATH that holds only its directory.
    interpreter = Path(sys.executable)
    result = _resolve_in_child(interpreter.name, str(interpreter.parent))
    assert result.returncode == 0, result.stderr
    assert os.path.samefile(result.stdout.strip(), interpreter)


def test_a_failing_step_stops_the_gate_with_its_exit_code() -> None:
    gate = _load()
    # A real child process that exits 3, not a stand-in for subprocess.run.
    with pytest.raises(SystemExit, match="exited 3"):
        gate.run([sys.executable, "-c", "raise SystemExit(3)"], REPOSITORY_ROOT, dict(os.environ))


@pytest.mark.skipif(os.name == "nt", reason="the stand-in bash is a POSIX shell script; Windows has no bash to stand in for")
def test_wrappers_are_launched_through_a_resolved_bash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _load()
    # A real executable named bash on a PATH that holds only its directory, run for real.
    bash = tmp_path / "bash"
    bash.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bash.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    argv = gate.bash_run(Path("scripts/wrapper.sh"), "arg")
    assert os.path.samefile(argv[0], bash)
    assert argv[1:] == ["scripts/wrapper.sh", "arg"]
    gate.run(argv, REPOSITORY_ROOT, dict(os.environ))


def test_a_missing_bash_names_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _load()
    # An empty PATH: the lookup is the real one, and no bash is on it.
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SystemExit, match="bash is not on PATH"):
        gate.bash_run(Path("scripts/wrapper.sh"))
