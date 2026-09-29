"""A job interpreter that can import music-dj-tools from OUTSIDE the checkout fails
the runner preflight (stale toolcache wheel on agentbox, Mon 28 Sep 2026; ADR PR #4214).

REAL INTERPRETERS, REAL SITE-PACKAGES. Nothing is mocked: every case builds a real
virtualenv (``python -m venv --without-pip``), plants real files in ITS
site-packages, and runs the real guard with that venv's own interpreter, the same
way ci_runner_preflight.sh runs it with setup-python's interpreter. A venv's
site-packages stands in for the toolcache's: both are the interpreter's own
purelib, which is what the guard inspects.

Presence, not absence: the positive controls prove the guard FIRES on a planted
dist-info (and on bare top-level packages), the negative controls prove a clean
interpreter and checkout-internal installs PASS.

Regression lines:
  - if a planted music_dj_tools-0.0.0.dist-info exits 0 then broken
  - if the failure does not name the planted dist-info path then broken
  - if an un-normalized ``Name: Music_DJ.Tools`` is not flagged then broken
  - if a bare apps/ or _rb_waveform_native in site-packages exits 0 then broken
  - if a clean interpreter exits nonzero then broken
  - if a venv inside the workspace holding the project exits nonzero then broken
  - if an editable install pointing into the workspace exits nonzero then broken
  - if an editable install pointing outside the workspace exits 0 then broken
  - if TOP_LEVEL_IMPORTS drifts from pyproject.toml then broken
  - if ci_runner_preflight.sh does not fail on a shadowed python3 on PATH then broken
  - if the Makefile reinstalls the wheel into a non-venv PY then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import ci_stale_install_guard as guard

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "ci_stale_install_guard.py"
PREFLIGHT = REPO / "scripts" / "ci_runner_preflight.sh"


# ---------------------------------------------------------------------------
# helpers


def _make_venv(where: Path) -> tuple[Path, Path]:
    """A real, empty virtualenv; returns (its python, its site-packages)."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(where)], check=True)
    python = where / "bin" / "python"
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return python, Path(purelib)


def _plant_dist_info(
    site: Path, name: str = "music-dj-tools", direct_url: dict | None = None
) -> Path:
    dist_info = site / "music_dj_tools-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.0\n")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url))
    return dist_info


def _run_guard(python: Path, workspace: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    return subprocess.run(
        [str(python), str(GUARD), "--workspace", str(workspace)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "checkout"
    ws.mkdir()
    return ws


@pytest.fixture
def outside_venv(tmp_path: Path) -> tuple[Path, Path]:
    return _make_venv(tmp_path / "toolcache")


# ---------------------------------------------------------------------------
# positive controls: the guard fires


def test_planted_dist_info_fails_naming_its_path(
    outside_venv: tuple[Path, Path], workspace: Path
) -> None:
    python, site = outside_venv
    dist_info = _plant_dist_info(site)
    result = _run_guard(python, workspace)
    assert result.returncode == 1, result.stdout + result.stderr
    assert str(dist_info.resolve()) in result.stderr or str(dist_info) in result.stderr
    assert "outside the checkout" in result.stderr


def test_unnormalized_project_name_still_fires(
    outside_venv: tuple[Path, Path], workspace: Path
) -> None:
    python, site = outside_venv
    _plant_dist_info(site, name="Music_DJ.Tools")
    assert _run_guard(python, workspace).returncode == 1


@pytest.mark.parametrize("planted", ["apps/__init__.py", "_rb_waveform_native.py"])
def test_bare_top_level_import_outside_workspace_fails(
    outside_venv: tuple[Path, Path], workspace: Path, planted: str
) -> None:
    python, site = outside_venv
    target = site / planted
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")
    result = _run_guard(python, workspace)
    assert result.returncode == 1, result.stdout + result.stderr
    assert planted.split("/", maxsplit=1)[0] in result.stderr


def test_editable_install_pointing_outside_workspace_fails(
    outside_venv: tuple[Path, Path], workspace: Path, tmp_path: Path
) -> None:
    python, site = outside_venv
    elsewhere = tmp_path / "old-checkout"
    elsewhere.mkdir()
    _plant_dist_info(site, direct_url={"url": elsewhere.as_uri(), "dir_info": {"editable": True}})
    assert _run_guard(python, workspace).returncode == 1


# ---------------------------------------------------------------------------
# negative controls: the guard passes what imports the checkout itself


def test_clean_interpreter_passes(outside_venv: tuple[Path, Path], workspace: Path) -> None:
    python, _ = outside_venv
    result = _run_guard(python, workspace)
    assert result.returncode == 0, result.stderr
    assert "[stale-install-guard] ok:" in result.stdout


def test_venv_inside_workspace_holding_the_project_passes(workspace: Path) -> None:
    python, site = _make_venv(workspace / ".venv")
    _plant_dist_info(site)
    (site / "apps").mkdir()
    (site / "apps" / "__init__.py").write_text("")
    result = _run_guard(python, workspace)
    assert result.returncode == 0, result.stderr


def test_editable_install_pointing_into_workspace_passes(
    outside_venv: tuple[Path, Path], workspace: Path
) -> None:
    python, site = outside_venv
    _plant_dist_info(site, direct_url={"url": workspace.as_uri(), "dir_info": {"editable": True}})
    result = _run_guard(python, workspace)
    assert result.returncode == 0, result.stderr


def test_non_editable_direct_url_into_workspace_still_fails(
    outside_venv: tuple[Path, Path], workspace: Path
) -> None:
    """The observed shape: a wheel built in the checkout's dist/ and installed
    into the shared interpreter. Its direct_url points INTO the workspace, but
    it is an archive copy, not an editable link, so it must still fire."""
    python, site = outside_venv
    wheel = workspace / "dist" / "music_dj_tools-1.0.1-cp311-abi3-linux_x86_64.whl"
    _plant_dist_info(site, direct_url={"url": wheel.as_uri(), "archive_info": {}})
    assert _run_guard(python, workspace).returncode == 1


# ---------------------------------------------------------------------------
# pins


def test_top_level_imports_match_pyproject() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    tool = pyproject["tool"]
    packages = {
        pattern.split(".")[0].rstrip("*")
        for pattern in tool["setuptools"]["packages"]["find"]["include"]
    }
    ext_modules = {ext["target"].split(".")[0] for ext in tool["setuptools-rust"]["ext-modules"]}
    assert set(guard.TOP_LEVEL_IMPORTS) == packages | ext_modules
    assert pyproject["project"]["name"] == guard.PROJECT_DIST_NAME


def test_runner_preflight_fails_when_python3_on_path_is_shadowed(
    outside_venv: tuple[Path, Path], workspace: Path
) -> None:
    python, site = outside_venv
    dist_info = _plant_dist_info(site)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env |= {
        "RUNNER_NAME": "agbox3-test",
        "GITHUB_WORKSPACE": str(workspace),
        "PATH": f"{python.parent}{os.pathsep}{env['PATH']}",
    }
    result = subprocess.run(
        [str(PREFLIGHT), "sh"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "agbox3-test" in result.stderr
    assert "music_dj_tools-0.0.0.dist-info" in result.stderr
    assert dist_info.name in result.stderr


def test_runner_preflight_passes_when_python_on_path_is_clean(
    outside_venv: tuple[Path, Path], workspace: Path
) -> None:
    python, _ = outside_venv
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env |= {
        "RUNNER_NAME": "agbox3-test",
        "GITHUB_WORKSPACE": str(workspace),
        "PATH": f"{python.parent}{os.pathsep}{env['PATH']}",
    }
    result = subprocess.run(
        [str(PREFLIGHT), "sh"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[stale-install-guard] ok: {python}" in result.stdout


def _make_require_venv_py(py: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-C", str(REPO), "require-venv-py", f"PY={py}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_makefile_refuses_to_install_into_a_non_venv_python(tmp_path: Path) -> None:
    base_python = Path(sys.base_prefix) / "bin" / "python3"
    result = _make_require_venv_py(base_python)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "is not a virtualenv interpreter" in result.stderr


def test_makefile_accepts_a_venv_python(outside_venv: tuple[Path, Path]) -> None:
    python, _ = outside_venv
    result = _make_require_venv_py(python)
    assert result.returncode == 0, result.stdout + result.stderr
