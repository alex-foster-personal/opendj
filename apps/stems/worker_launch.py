"""How a stems job finds and starts its worker, in a checkout AND in the app.

WHY THIS EXISTS (issue #3421). Every stems producer is a script under
``scripts/`` started as a subprocess. In a repo checkout that works by
construction: ``scripts/`` is there and ``uv`` is on PATH. In the installed
app neither held. The payload shipped ``apps/`` only, so every worker path
named a file that was not in the bundle, and two of the three routes also
needed a ``uv`` binary the app does not ship. A user who installed the dmg
could not produce stems by any route, and no test noticed, because every
stems test runs from a checkout.

So the two questions a spawn has to answer live here, once:

* WHERE is the worker: an ABSOLUTE path under ``PROJECT_ROOT / scripts``.
  ``PROJECT_ROOT`` is ``payload/app`` in the installed app, and the payload
  build stages :data:`PAYLOAD_WORKER_SCRIPTS` there.
* WHAT starts it: the payload's own interpreter when the launcher names one
  (the same relocatable runtime, with the payload's ``pylib`` on PYTHONPATH,
  that local vocals already use), otherwise ``uv run`` as in a checkout.

Requirements (mini-PRD, STEM-39):
  [if] the launcher names a packaged interpreter [then] every stems argv
    starts with it and never names uv [else ⛔️ no route runs in the app]
  [if] no packaged interpreter and no uv [then ⛔️] refuse with a sentence,
    before a job is reported as started
  [if] a worker script is not on disk [then ⛔️] refuse naming the path
  [if] the installed app is asked for the direct Modal transport [then ⛔️]
    refuse: modal is not shipped, and a tester holds no Modal token anyway

-Claude
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# ----- CFG -------------------------------------------------------------------
PACKAGED_PYTHON_ENVS: tuple[str, ...] = (
    "MDT_STEM_WORKER_PYTHON",
    "MDT_VOCAL_WORKER_PYTHON",
)
"""The interpreter the payload launcher exports, first one set wins.

``MDT_VOCAL_WORKER_PYTHON`` is the payload's ``runtime/bin/python3``, already
exported for local vocals; the stems workers need the same interpreter with
the same ``pylib`` (demucs and torch ride in the ``vocals`` extra), so they
reuse it rather than ask the launcher for a second copy of one path. The
``MDT_STEM_WORKER_PYTHON`` override is the one ``apps/stems/cli.py`` already
honors.
"""

UV_BIN_ENV: str = "MDT_UV_BIN"

PAYLOAD_WORKER_SCRIPTS: tuple[str, ...] = (
    "scripts/__init__.py",
    "scripts/stems_modal_worker.py",
    "scripts/stems_local_worker.py",
    "scripts/stems_r2_first_worker.py",
    "scripts/stems_hydrate_worker.py",
    "scripts/stem_bundle_worker.py",
    "scripts/cloud_hydrate_worker.py",
)
"""Worker scripts the payload build copies to ``payload/app/scripts/``.

``scripts/modal_vocal_farm.py`` is deliberately NOT here: it imports modal at
module scope, and modal is not shipped. The relay path writes bundles through
:mod:`apps.stems.bundle_publish` instead, so the app never imports it.
"""


class WorkerLaunchError(RuntimeError):
    """This install cannot start the worker; the message says why."""


# ----- where -----------------------------------------------------------------


def worker_path(relative: str) -> Path:
    """``relative`` (``scripts/<name>.py``) as an absolute path in this install.

    Read at CALL time so a test that repoints ``PROJECT_ROOT`` at a staged
    payload measures the payload, not the checkout it runs from.
    """
    from apps.shared import platform_paths

    return Path(platform_paths.PROJECT_ROOT) / relative


def missing_worker(relative: str) -> str | None:
    """Why the worker named by ``relative`` cannot be spawned, or None."""
    path = worker_path(relative)
    if path.is_file():
        return None
    return f"{relative} is not present at {path}, so this build cannot run it"


# ----- what ------------------------------------------------------------------


def packaged_python() -> str | None:
    """The payload interpreter the launcher named, or None in a checkout."""
    for name in PACKAGED_PYTHON_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def uv_bin() -> str:
    return os.environ.get(UV_BIN_ENV, "uv")


def python_prefix(*, with_modal: bool = False) -> list[str]:
    """The argv prefix that runs a worker script, ending in an interpreter.

    ``with_modal`` asks for the ``--with modal`` overlay, which only the
    direct Modal transport needs. The installed app has no overlay to offer,
    so that request is refused there rather than started and left to die on
    ``import modal`` in a subprocess nobody is watching.
    """
    packaged = packaged_python()
    if packaged is not None:
        if with_modal:
            raise WorkerLaunchError(
                "the direct Modal transport needs the modal package, which the "
                "installed app does not ship; the app separates through the relay"
            )
        if not Path(packaged).is_file():
            raise WorkerLaunchError(
                f"the packaged stems interpreter {packaged} does not exist"
            )
        return [packaged]
    uv = uv_bin()
    if shutil.which(uv) is None:
        raise WorkerLaunchError(
            f"{uv!r} is not on PATH, and the stems worker needs uv in a "
            f"checkout. Install uv, or set {UV_BIN_ENV}."
        )
    prefix = [uv, "run", "--no-sync"]
    if with_modal:
        prefix += ["--with", "modal"]
    return [*prefix, "python"]


def script_argv(relative: str, *, with_modal: bool = False) -> list[str]:
    """Interpreter plus absolute worker path, or :class:`WorkerLaunchError`."""
    missing = missing_worker(relative)
    if missing is not None:
        raise WorkerLaunchError(missing)
    return [*python_prefix(with_modal=with_modal), str(worker_path(relative))]


__all__ = [
    "PACKAGED_PYTHON_ENVS",
    "PAYLOAD_WORKER_SCRIPTS",
    "WorkerLaunchError",
    "missing_worker",
    "packaged_python",
    "python_prefix",
    "script_argv",
    "uv_bin",
    "worker_path",
]
