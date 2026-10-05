"""tests/conftest.py must load with and without starlette installed.

CI runs the frontend typing gate (and other quality suites) in an isolated
environment holding pytest alone, and tests/conftest.py loads for them too.
When that conftest imported the TestClient helper unconditionally, every such
step failed at conftest load with ``No module named 'starlette'``. The fix
patches the TestClient default only when starlette is importable, so both
directions are pinned here:

- if tests/conftest.py fails to load when starlette is absent, then isolated
  CI suites are broken
- if the TestClient default is not loopback when starlette is present, then
  the host allowlist rejects every default TestClient with 403
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# sys.modules[name] = None makes both `import starlette` and
# importlib.util.find_spec("starlette") report it absent, which is the state of
# an isolated pytest-only environment.
_COLLECT_WITHOUT_STARLETTE = """
import sys
sys.modules["starlette"] = None
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", sys.argv[1]]))
"""


def test_conftest_loads_without_starlette() -> None:
    target = "tests/quality/test_frontend_typing_directives.py"
    proc = subprocess.run(
        [sys.executable, "-c", _COLLECT_WITHOUT_STARLETTE, target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if tests/conftest.py fails to load when starlette is absent, then isolated "
        f"CI suites are broken (rc={proc.returncode}):\n{output}"
    )
    assert "test_frontend_typing_directives.py::" in output, (
        f"collection ran but collected nothing from {target}:\n{output}"
    )


def test_testclient_default_is_loopback_when_starlette_present() -> None:
    from starlette.testclient import TestClient

    defaults = TestClient.__init__.__defaults__
    assert defaults is not None
    base_url_default = defaults[0]
    assert base_url_default == "http://127.0.0.1", (
        "if the TestClient default is not loopback when starlette is present, then "
        f"the host allowlist rejects every default TestClient (got {base_url_default!r})"
    )
