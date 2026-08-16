"""apps.vocals must import on a BASE install, with no ``analysis`` extra.

Regression guard for a real defect: ``apps/vocals/from_stems.py`` imported
soundfile at module level, and ``apps/vocals/cli.py`` imports from_stems at
module level, so a base install could not run ``python -m apps.vocals`` at all
and could not even COLLECT tests/vocals/. soundfile ships only in the
``analysis`` optional extra (pyproject.toml), so a module-level import silently
promoted that extra to a hard base requirement.

soundfile is lazy-imported inside the one function that needs it, matching
scripts/vocal_region_worker.py and scripts/stem_bundle_worker.py.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.requirement("CAT-05")]

# Modules that must survive an import with soundfile unavailable.
_BASE_IMPORTABLE: tuple[str, ...] = ("apps.vocals.from_stems", "apps.vocals.cli")

_BLOCKER = """
import sys

class _Block:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        if name == "soundfile" or name.startswith("soundfile."):
            raise ImportError("soundfile blocked: simulating a base install")
        return None

sys.meta_path.insert(0, _Block())
sys.modules.pop("soundfile", None)
import {module}
print("OK")
"""


@pytest.mark.parametrize("module", _BASE_IMPORTABLE)
def test_imports_without_soundfile(module: str) -> None:
    """[if] apps.vocals needs soundfile to import [then] base install is broken."""
    proc = subprocess.run(
        [sys.executable, "-c", _BLOCKER.format(module=module)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"{module} cannot import without soundfile.\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "OK" in proc.stdout


def test_soundfile_not_imported_at_module_level() -> None:
    """[if] from_stems binds sf at import time [then] the lazy import regressed."""
    mod = importlib.import_module("apps.vocals.from_stems")
    assert not hasattr(mod, "sf"), (
        "apps.vocals.from_stems binds a module-level `sf`; soundfile is only in "
        "the `analysis` extra, so the import must stay inside _read_mono()"
    )
