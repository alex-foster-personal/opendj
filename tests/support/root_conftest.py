"""The repo-root ``conftest.py`` as an importable module.

A bare ``from conftest import X`` resolves to whichever module pytest cached
under the name ``conftest`` first. Run alone, that is the root conftest; in a
whole-suite run it is often a sub-directory conftest (``tests/<dir>/conftest.py``
in a directory without ``__init__.py``), and the import fails at collection.
This module resolves the root conftest by path instead: the copy pytest already
loaded when there is one (so its import-time setup never runs twice), else a
fresh load under a unique module name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT_CONFTEST_PATH: Path = Path(__file__).resolve().parents[2] / "conftest.py"
_UNIQUE_MODULE_NAME = "_mdt_root_conftest"


def _already_loaded_root_conftest() -> ModuleType | None:
    for module in list(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if module_file and Path(module_file).resolve() == ROOT_CONFTEST_PATH:
            return module
    return None


def load_root_conftest() -> ModuleType:
    """Return the repo-root conftest module, loading it by path if pytest has not."""
    loaded = _already_loaded_root_conftest()
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_UNIQUE_MODULE_NAME, ROOT_CONFTEST_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {ROOT_CONFTEST_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_UNIQUE_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_root = load_root_conftest()

RB_FIXTURE_MANIFEST: Path = _root.RB_FIXTURE_MANIFEST
hydrate_mdt_data_dir_from_rb_ingest = _root.hydrate_mdt_data_dir_from_rb_ingest
_fail_closed_missing_fixture = _root._fail_closed_missing_fixture
