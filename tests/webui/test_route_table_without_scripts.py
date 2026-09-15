"""The engine's route table must build without ``scripts`` importable.

The packaged engine (``scripts/build_engine_payload.py``) ships ``apps`` and
its wheels, never ``scripts/``. A route module that imports ``scripts.*`` at
module level therefore passes every in-repo test and breaks only inside the
payload, whose own route-table check is the first thing to notice (it did on
Tue 15 Sep 2026: ``routes/stems_assets.py`` imported
``scripts.stem_inventory`` and ``just dmg`` failed at verify).

This test reproduces the payload's boot under the repo interpreter with a
meta-path blocker that refuses ``scripts`` and every submodule, so the class
of break fails here first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_BOOT_WITHOUT_SCRIPTS = r"""
import importlib.abc
import sys
import tempfile


class _RefuseScripts(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "scripts" or fullname.startswith("scripts."):
            raise ModuleNotFoundError(
                f"{fullname} is not shipped in the engine payload", name=fullname
            )
        return None


sys.modules.pop("scripts", None)
sys.meta_path.insert(0, _RefuseScripts())

from apps.engine_core.config import apply_env_contract, build_config, prepare_layout

data_dir = tempfile.mkdtemp(prefix="opendj-route-table-verify-")
cfg = build_config(data_dir, "127.0.0.1", 1)
apply_env_contract(cfg)
prepare_layout(cfg)
from apps.engine_core.app import create_app

app = create_app(cfg)
print(len({getattr(r, "path", "") for r in app.routes}))
"""


def _boot_route_table(block_scripts: bool) -> subprocess.CompletedProcess[str]:
    code = _BOOT_WITHOUT_SCRIPTS
    if not block_scripts:
        code = code.replace("sys.meta_path.insert(0, _RefuseScripts())", "pass")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_route_table_builds_without_scripts_package() -> None:
    """if any route module imports scripts.* at import time then the payload cannot boot."""
    result = _boot_route_table(block_scripts=True)
    assert result.returncode == 0, (
        "the route table imports from scripts/, which the payload does not ship:\n"
        + result.stderr[-4000:]
    )
    assert int(result.stdout.strip().splitlines()[-1]) > 100, result.stdout


def test_scripts_blocker_refuses_a_scripts_import() -> None:
    """if the blocker lets `import scripts.stem_inventory` through then the guard above is inert."""
    probe = _BOOT_WITHOUT_SCRIPTS.split("from apps.engine_core.config")[0] + (
        "import scripts.stem_inventory\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0
    assert "is not shipped in the engine payload" in result.stderr
